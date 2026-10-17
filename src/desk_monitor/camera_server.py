"""カメラ映像を LAN 内に配信するサーバ(カメラを付けた Pi で動かす。role camera)。

Pi 3 のハードウェアエンコーダで 2 本のストリームを作り、接続してきた相手に送る。CPU はほとんど使わない。

  h264  : 全視野 1280x960 の H.264。ハブ(開発 PC)が受けて物体検出とダッシュボードに使う
  mjpeg : 全視野 320x240 の MJPEG。表示用の Pi が受けて、中央の正方形を円形 LCD に出す

どちらも同じ全視野(4:3)なので、ハブが返す正規化座標(0〜1)の検出枠をそのまま重ねられる。

通信の形式は wire.py。接続した側は hello で欲しいストリームを指定する:
  → {"type": "hello", "token": ..., "stream": "h264" | "mjpeg"}
  ← {"type": "frame", "key": bool, "ts": マイクロ秒} + 符号化済みの 1 フレーム
  ← {"type": "status", "camera": {...}, "system": {...}}  約 2 秒ごと

H.264 は途中から受けても復号できるよう、キーフレームから送り始める(キーフレームごとに SPS/PPS も付ける)。
送信が追いつかない相手には、溜まったフレームを捨ててキーフレームから送り直す。

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.camera_server [--port 8082] [--fps 20] [--no-vflip] [--hflip]

依存パッケージ:
    sudo apt install --no-install-recommends python3-picamera2
"""
from __future__ import annotations

import argparse
import collections
import logging
import os
import select
import socket
import socketserver
import sys
import threading
import time

from desk_monitor import sysstat, wire

log = logging.getLogger(__name__)

MAIN_SIZE = (1280, 960)
LORES_SIZE = (320, 240)
# ov5647 の 1296x972 モード(全視野を 2x2 ビニング)。640x480 モードは画素を間引くため細部がつぶれ、検出精度が落ちる
SENSOR_SIZE = (1296, 972)
STREAMS = ('h264', 'mjpeg')
STATUS_SEC = 2.0
# 送信待ちがこれを超えた相手は、溜まった分を捨てる(遅い相手のために遅延を溜めない)
MAX_QUEUE = 30


class Client:
    """1 つの接続相手への送信待ちの列。送信はこの相手専用のスレッドで行う。"""

    def __init__(self, needs_keyframe: bool) -> None:
        self._cond = threading.Condition()
        self._queue: collections.deque[bytes] = collections.deque()
        self._needs_keyframe = needs_keyframe
        self._waiting_key = needs_keyframe
        self.closed = False
        self.dropped = 0

    def offer_frame(self, message: bytes, keyframe: bool) -> None:
        with self._cond:
            if self.closed:
                return
            if self._waiting_key and not keyframe:
                return
            self._waiting_key = False
            if len(self._queue) >= MAX_QUEUE:
                self.dropped += len(self._queue)
                self._queue.clear()
                if self._needs_keyframe and not keyframe:
                    self._waiting_key = True
                    return
            self._queue.append(message)
            self._cond.notify()

    def offer_status(self, message: bytes) -> None:
        with self._cond:
            if not self.closed:
                self._queue.append(message)
                self._cond.notify()

    def next_message(self, timeout: float) -> bytes | None:
        with self._cond:
            self._cond.wait_for(lambda: self._queue or self.closed, timeout=timeout)
            return self._queue.popleft() if self._queue else None

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify()


class Broadcaster:
    """エンコーダの出力を、同じストリームを受けている全員に配る。"""

    def __init__(self, stream: str) -> None:
        self.stream = stream
        self._lock = threading.Lock()
        self._clients: set[Client] = set()
        self.frames = 0

    def add(self) -> Client:
        client = Client(needs_keyframe=self.stream == 'h264')
        with self._lock:
            self._clients.add(client)
        return client

    def remove(self, client: Client) -> None:
        client.close()
        with self._lock:
            self._clients.discard(client)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def frame(self, data: bytes, keyframe: bool, timestamp: int | None) -> None:
        self.frames += 1
        with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        message = wire.encode({'type': 'frame', 'key': bool(keyframe), 'ts': timestamp}, data)
        for client in clients:
            client.offer_frame(message, bool(keyframe))

    def status(self, header: dict) -> None:
        message = wire.encode(header)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.offer_status(message)


def make_output(broadcaster: Broadcaster):
    """picamera2 のエンコーダの出力先(Output)を作る。picamera2 はここで初めて import する。"""
    from picamera2.outputs import Output

    class _BroadcastOutput(Output):
        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            if not audio:
                broadcaster.frame(bytes(frame), keyframe, timestamp)

    return _BroadcastOutput()


class CameraRunner:
    """カメラと 2 つのエンコーダを動かし続ける。失敗したら RETRY_SEC 後にやり直す。"""

    RETRY_SEC = 30.0

    def __init__(self, broadcasters: dict[str, Broadcaster], fps: float,
                 vflip: bool, hflip: bool) -> None:
        self._broadcasters = broadcasters
        self._fps = fps
        self._vflip = vflip
        self._hflip = hflip
        self.running = False
        self.error: str | None = None

    def run_forever(self) -> None:
        while True:
            try:
                self._run_once()
            except Exception as exc:  # カメラの不調でサーバごと落とさない
                self.running = False
                self.error = repr(exc)
                log.warning('カメラを動かせません(%.0f 秒後に再試行): %r', self.RETRY_SEC, exc)
            time.sleep(self.RETRY_SEC)

    def _run_once(self) -> None:
        from libcamera import Transform
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder, MJPEGEncoder

        cam = Picamera2()
        try:
            config = cam.create_video_configuration(
                main={'size': MAIN_SIZE, 'format': 'YUV420'},
                sensor={'output_size': SENSOR_SIZE, 'bit_depth': 10},
                lores={'size': LORES_SIZE, 'format': 'YUV420'},
                transform=Transform(vflip=self._vflip, hflip=self._hflip),
                controls={'FrameRate': self._fps},
            )
            cam.configure(config)
            iperiod = max(1, round(self._fps))  # 途中から受けた相手も約 1 秒で映り始める
            cam.start_encoder(H264Encoder(bitrate=5_000_000, repeat=True, iperiod=iperiod),
                              make_output(self._broadcasters['h264']), name='main')
            cam.start_encoder(MJPEGEncoder(bitrate=4_000_000),
                              make_output(self._broadcasters['mjpeg']), name='lores')
            cam.start()
            self.running, self.error = True, None
            log.info('カメラを開始しました(%dx%d H.264 / %dx%d MJPEG, %.0f fps)',
                     *MAIN_SIZE, *LORES_SIZE, self._fps)
            # エンコーダが止まったら(カメラが外れたなど)やり直す
            last = sum(b.frames for b in self._broadcasters.values())
            while True:
                time.sleep(5.0)
                now = sum(b.frames for b in self._broadcasters.values())
                if now == last:
                    raise RuntimeError('5 秒間フレームが出ていません')
                last = now
        finally:
            self.running = False
            try:
                cam.stop_encoder()
                cam.stop()
            finally:
                cam.close()


class _Handler(socketserver.BaseRequestHandler):
    server: CameraServer

    def handle(self) -> None:
        host = self.client_address[0]
        accepted = wire.accept_hello(self.request, host, self.server.token)
        if accepted is None:
            log.warning('接続を拒否しました: %s', host)
            return
        _, hello = accepted
        broadcaster = self.server.broadcasters.get(hello.get('stream'))
        if broadcaster is None:
            log.warning('不明なストリームの要求: %s %r', host, hello.get('stream'))
            return
        client = broadcaster.add()
        client.offer_status(wire.encode(self.server.status()))
        log.info('%s に %s の配信を開始しました', host, broadcaster.stream)
        try:
            while True:
                message = client.next_message(timeout=1.0)
                if message is not None:
                    self.request.sendall(message)
                elif self._peer_closed():
                    break
        except OSError:
            pass
        finally:
            broadcaster.remove(client)
            log.info('%s への %s の配信を終えました', host, broadcaster.stream)


    def _peer_closed(self) -> bool:
        """相手が接続を閉じたか(送るものがない間も切断に気づくため)。"""
        try:
            readable, _, _ = select.select([self.request], [], [], 0)
            return bool(readable) and self.request.recv(1, socket.MSG_PEEK) == b''
        except OSError:
            return True


class CameraServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], token: str, runner: CameraRunner | None,
                 broadcasters: dict[str, Broadcaster]) -> None:
        self.token = token
        self.runner = runner
        self.broadcasters = broadcasters
        self._meter = sysstat.CpuMeter()
        self._fps: dict[str, float] = {}
        self._counted: dict[str, tuple[int, float]] = {}
        super().__init__(addr, _Handler)

    def status(self) -> dict:
        return {
            'type': 'status',
            'camera': {
                'running': bool(self.runner and self.runner.running),
                'error': self.runner.error if self.runner else None,
                'fps': dict(self._fps),
                'clients': {name: b.client_count for name, b in self.broadcasters.items()},
            },
            'system': sysstat.snapshot(self._meter),
        }

    def publish_status_forever(self) -> None:
        while True:
            now = time.time()
            for name, b in self.broadcasters.items():
                frames, at = self._counted.get(name, (b.frames, now))
                if now > at:
                    self._fps[name] = round((b.frames - frames) / (now - at), 1)
                self._counted[name] = (b.frames, now)
            status = self.status()
            for b in self.broadcasters.values():
                b.status(status)
            time.sleep(STATUS_SEC)


def main() -> None:
    parser = argparse.ArgumentParser(description='カメラ映像を LAN 内に配信する')
    parser.add_argument('--bind', default='0.0.0.0', help='待ち受けるアドレス (既定: 0.0.0.0。IPv4 のみ)')
    parser.add_argument('--port', type=int, default=8082, help='待ち受けるポート (既定: 8082)')
    parser.add_argument('--fps', type=float, default=20.0, help='フレームレート (既定: 20)')
    parser.add_argument('--vflip', action=argparse.BooleanOptionalAction, default=True,
                        help='上下反転 (既定: 有効。鏡像を 180° 回転した向きにする)')
    parser.add_argument('--hflip', action=argparse.BooleanOptionalAction, default=False,
                        help='左右反転 (既定: 無効)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    token = os.environ.get('DESK_MONITOR_TOKEN', '')
    if not token:
        print('DESK_MONITOR_TOKEN を設定してください(scripts/setup-secrets.sh)', file=sys.stderr)
        sys.exit(2)

    broadcasters = {name: Broadcaster(name) for name in STREAMS}
    runner = CameraRunner(broadcasters, args.fps, args.vflip, args.hflip)
    server = CameraServer((args.bind, args.port), token, runner, broadcasters)
    threading.Thread(target=runner.run_forever, daemon=True, name='camera').start()
    threading.Thread(target=server.publish_status_forever, daemon=True, name='status').start()
    log.info('カメラ配信を待ち受けます: port %d', args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
