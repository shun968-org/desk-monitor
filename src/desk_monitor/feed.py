"""表示中のフレームをダッシュボード(pi_b の dashboard.py)に送る。

表示プロセスから外向きに接続するだけで、pi_a に待ち受けポートは開かない。
ダッシュボードに閲覧者がいるときだけ JPEG を作って送り(上限 fps あり)、
いないときは約 1 秒ごとに表示モードを知らせるだけにして、負荷と発熱を増やさない。
通信の形式は dashboard.py を参照。

接続先とトークンは環境変数で渡す(systemd の EnvironmentFile。リポジトリには置かない):
  DESK_MONITOR_FEED_ADDR  例: 192.168.10.111:8081
  DESK_MONITOR_FEED_TOKEN
"""
from __future__ import annotations

import io
import json
import socket
import sys
import threading
import time

import numpy as np
from PIL import Image

JPEG_QUALITY = 80


def parse_addr(addr: str) -> tuple[str, int]:
    host, sep, port = addr.rpartition(':')
    if not sep or not host:
        raise ValueError(f'host:port の形式で指定してください: {addr!r}')
    return host, int(port)


def encode_jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format='JPEG', quality=JPEG_QUALITY)
    return buf.getvalue()


class DashboardFeed:
    """ダッシュボードへの接続を別スレッドで保ち、最新フレームを送る。"""

    STATUS_SEC = 1.0    # 表示モードを知らせる間隔
    RETRY_SEC = 10.0    # 接続できなかったときに再接続するまでの秒数
    CONNECT_TIMEOUT = 5.0

    def __init__(self, addr: tuple[str, int], token: str, fps: float = 5.0,
                 start: bool = True) -> None:
        self._addr = addr
        self._token = token
        self._interval = 1.0 / fps
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._published_at = 0.0
        self._mode = 'face'
        self._viewers = 0
        self._connected = False
        if start:
            threading.Thread(target=self._run, daemon=True, name='dashboard-feed').start()

    @property
    def viewers(self) -> int:
        return self._viewers

    def wanted(self, t: float) -> bool:
        """このフレームを送るべきか(閲覧者がいて、前回から送信間隔が空いている)。"""
        return self._viewers > 0 and t - self._published_at >= self._interval

    def publish(self, arr: np.ndarray, t: float) -> None:
        """表示したフレームを渡す。JPEG への変換と送信は送信スレッドで行う。"""
        with self._cond:
            self._frame = arr
            self._published_at = t
            self._cond.notify()

    def set_mode(self, mode: str) -> None:
        """LCD に実際に出している表示('camera' / 'face')を知らせる。"""
        self._mode = mode

    # ─── 送信スレッド ───

    def _run(self) -> None:
        while True:
            try:
                with socket.create_connection(self._addr, timeout=self.CONNECT_TIMEOUT) as sock:
                    self._serve(sock)
            except OSError as exc:
                if self._connected:
                    print(f'ダッシュボードとの接続が切れました: {exc!r}', file=sys.stderr, flush=True)
                self._connected = False
                self._viewers = 0
            time.sleep(self.RETRY_SEC)

    def _serve(self, sock: socket.socket) -> None:
        sock.settimeout(10.0)
        self._send(sock, {'type': 'hello', 'token': self._token})
        self._connected = True
        print(f'ダッシュボードに接続しました: {self._addr[0]}:{self._addr[1]}', flush=True)
        reader = threading.Thread(target=self._read_viewers, args=(sock,), daemon=True,
                                  name='dashboard-feed-reader')
        reader.start()
        next_status = 0.0
        while reader.is_alive():
            with self._cond:
                self._cond.wait_for(lambda: self._frame is not None, timeout=self.STATUS_SEC)
                frame, self._frame = self._frame, None
            if frame is not None and self._viewers > 0:
                self._send(sock, {'type': 'frame'}, encode_jpeg(frame))
            now = time.time()
            if now >= next_status:
                self._send(sock, {'type': 'status', 'mode': self._mode})
                next_status = now + self.STATUS_SEC
        raise ConnectionError('ダッシュボード側から切断されました')

    def _read_viewers(self, sock: socket.socket) -> None:
        """ダッシュボードから届く閲覧者数を反映する。接続が切れたら終わる。"""
        buf = b''
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue  # 閲覧者数が変わらない間は何も届かない
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                try:
                    self._viewers = int(json.loads(line).get('viewers', 0))
                except (ValueError, AttributeError):
                    continue
        self._viewers = 0

    @staticmethod
    def _send(sock: socket.socket, header: dict, payload: bytes = b'') -> None:
        header = {**header, 'size': len(payload)}
        sock.sendall(json.dumps(header).encode() + b'\n' + payload)
