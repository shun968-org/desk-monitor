"""camera 映像(検出枠つき)を LAN 内のブラウザに配信するダッシュボード(pi_b で動かす)。

表示プロセス(pi_a の display.py)が feed.py で JPEG フレームを送ってきて、それを
ブラウザに MJPEG で中継する。pi_a は外向きに接続するだけで、待ち受けポートを持たない。

  feed ポート(既定 8081): 表示プロセスからの接続。最初の行の共有トークンで認証する
      pi_a → pi_b: 1 行の JSON ヘッダ + size バイトのペイロード
          {"type": "hello", "token": "..."}           接続直後に 1 回
          {"type": "frame", "size": N} + JPEG         camera 表示中、閲覧者がいるときだけ
          {"type": "status", "mode": "face"|"camera"} 約 1 秒ごと
      pi_b → pi_a: {"viewers": N} の行(閲覧者数が変わるたび)
  HTTP ポート(既定 8080): ブラウザ向け。Basic 認証
      /             ダッシュボードのページ
      /stream.mjpg  MJPEG ストリーム
      /status       {"connected", "mode", "live"} の JSON

どちらのポートも、接続元がプライベート IP(LAN)でなければ拒否する。
パスワードとトークンは環境変数で渡す(systemd の EnvironmentFile。リポジトリには置かない):
  DESK_MONITOR_DASHBOARD_PASSWORD, DESK_MONITOR_DASHBOARD_USER(既定 desk), DESK_MONITOR_FEED_TOKEN

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.dashboard [--http-port 8080] [--feed-port 8081]

標準ライブラリだけで動く(pi_b には venv も追加パッケージも要らない)。
"""
from __future__ import annotations

import argparse
import base64
import hmac
import ipaddress
import json
import logging
import os
import select
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# 最後のフレームからこの秒数が過ぎたら「映像なし」とみなす
LIVE_SEC = 2.0
# 同時に見られるブラウザの数(Pi 3 の負荷と帯域を抑える)
MAX_VIEWERS = 4
# 表示プロセスから受け取るヘッダ行・ペイロードの上限
_MAX_HEADER = 4096
_MAX_PAYLOAD = 1 << 20

_BOUNDARY = 'frame'


def is_lan_address(host: str) -> bool:
    """LAN(プライベート IP・ループバック)からの接続か。"""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


class FrameStore:
    """表示プロセスから届いた最新フレームと状態を持ち、閲覧者に渡す。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._frame_at = 0.0
        self._mode: str | None = None
        self._viewers = 0
        self._producer: Callable[[int], None] | None = None
        self._producer_id = 0

    # ─── 表示プロセス(feed)側 ───

    def attach_producer(self, notify: Callable[[int], None]) -> int:
        """表示プロセスの接続を登録し、現在の閲覧者数を通知する。古い接続は置き換える。"""
        with self._cond:
            self._producer_id += 1
            self._producer = notify
            viewers = self._viewers
            pid = self._producer_id
        notify(viewers)
        return pid

    def detach_producer(self, pid: int) -> None:
        with self._cond:
            if pid == self._producer_id:
                self._producer = None
                self._mode = None

    def put_frame(self, jpeg: bytes, now: float | None = None) -> None:
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._frame_at = time.time() if now is None else now
            self._mode = 'camera'
            self._cond.notify_all()

    def put_mode(self, mode: str) -> None:
        with self._cond:
            self._mode = mode

    # ─── ブラウザ側 ───

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._cond:
            return {
                'connected': self._producer is not None,
                'mode': self._mode,
                'live': self._jpeg is not None and now - self._frame_at < LIVE_SEC,
            }

    def add_viewer(self) -> bool:
        """閲覧者を 1 人増やす。上限を超えるなら False。"""
        with self._cond:
            if self._viewers >= MAX_VIEWERS:
                return False
            self._viewers += 1
            notify, viewers = self._producer, self._viewers
        if notify is not None:
            notify(viewers)
        return True

    def remove_viewer(self) -> None:
        with self._cond:
            self._viewers -= 1
            notify, viewers = self._producer, self._viewers
        if notify is not None:
            notify(viewers)

    def next_frame(self, last_seq: int, timeout: float) -> tuple[int, bytes | None]:
        """last_seq より新しいフレームを待って返す。timeout までに来なければ (last_seq, None)。"""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout)
            if self._seq == last_seq:
                return last_seq, None
            return self._seq, self._jpeg


# ─── feed ポート(表示プロセスからの接続) ───────────────────────────────────

class _FeedHandler(socketserver.StreamRequestHandler):
    server: FeedServer

    def handle(self) -> None:
        host = self.client_address[0]
        if not is_lan_address(host):
            log.warning('LAN 外からの feed 接続を拒否しました: %s', host)
            return
        self.request.settimeout(10.0)
        try:
            hello = self._read_header()
        except (OSError, ValueError):
            return
        if hello.get('type') != 'hello' or not hmac.compare_digest(
                str(hello.get('token', '')).encode(), self.server.token.encode()):
            log.warning('feed の認証に失敗しました: %s', host)
            return

        lock = threading.Lock()

        def notify(viewers: int) -> None:
            try:
                with lock:
                    self.wfile.write(json.dumps({'viewers': viewers}).encode() + b'\n')
                    self.wfile.flush()
            except OSError:
                pass

        store = self.server.store
        pid = store.attach_producer(notify)
        log.info('表示プロセスが接続しました: %s', host)
        # 表示プロセスは 1 秒ごとに status を送るので、それより十分長く途切れたら切断とみなす
        self.request.settimeout(10.0)
        try:
            while True:
                header = self._read_header()
                kind = header.get('type')
                size = int(header.get('size', 0))
                if not 0 <= size <= _MAX_PAYLOAD:
                    raise ValueError(f'payload too large: {size}')
                payload = self.rfile.read(size) if size else b''
                if len(payload) != size:
                    raise EOFError
                if kind == 'frame':
                    store.put_frame(payload)
                elif kind == 'status' and header.get('mode') in ('face', 'camera'):
                    store.put_mode(header['mode'])
        except (OSError, ValueError, EOFError):
            pass
        finally:
            store.detach_producer(pid)
            log.info('表示プロセスとの接続が切れました: %s', host)

    def _read_header(self) -> dict:
        line = self.rfile.readline(_MAX_HEADER)
        if not line.endswith(b'\n'):
            raise EOFError
        header = json.loads(line)
        if not isinstance(header, dict):
            raise ValueError('header is not an object')
        return header


class FeedServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], store: FrameStore, token: str) -> None:
        self.store = store
        self.token = token
        super().__init__(addr, _FeedHandler)


# ─── HTTP ポート(ブラウザ) ──────────────────────────────────────────────────

class _HttpHandler(BaseHTTPRequestHandler):
    server: DashboardHttpServer
    server_version = 'desk-monitor'
    sys_version = ''

    # MJPEG を送り続けている間に表示プロセス側が止まっても、ブラウザの接続を保つための間隔
    KEEPALIVE_SEC = 5.0

    def do_GET(self) -> None:  # noqa: N802 (http.server の命名)
        if not is_lan_address(self.client_address[0]):
            self.send_error(403)
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="desk-monitor", charset="UTF-8"')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        path = urlsplit(self.path).path
        if path == '/':
            self._send(200, 'text/html; charset=utf-8', INDEX_HTML.encode())
        elif path == '/status':
            self._send(200, 'application/json', json.dumps(self.server.store.status()).encode())
        elif path == '/stream.mjpg':
            self._stream()
        else:
            self.send_error(404)

    def _authorized(self) -> bool:
        header = self.headers.get('Authorization', '')
        scheme, _, encoded = header.partition(' ')
        if scheme.lower() != 'basic':
            return False
        try:
            user, _, password = base64.b64decode(encoded, validate=True).decode().partition(':')
        except (ValueError, UnicodeDecodeError):
            return False
        # 両方を必ず比較し、どちらが違ったかを応答時間から推測させない
        ok_user = hmac.compare_digest(user.encode(), self.server.user.encode())
        ok_password = hmac.compare_digest(password.encode(), self.server.password.encode())
        return ok_user and ok_password

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        store = self.server.store
        if not store.add_viewer():
            self.send_error(503, 'Too many viewers')
            return
        try:
            self.send_response(200)
            self.send_header('Content-Type', f'multipart/x-mixed-replace; boundary={_BOUNDARY}')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            seq, last, sent_at = 0, None, 0.0
            while not self._client_gone():
                seq, jpeg = store.next_frame(seq, timeout=1.0)
                if jpeg is None:
                    # 新しいフレームがない間も、ときどき最後のフレームを送り直して接続を保つ
                    if last is None or time.time() - sent_at < self.KEEPALIVE_SEC:
                        continue
                    jpeg = last
                last, sent_at = jpeg, time.time()
                self.wfile.write(
                    f'--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n'
                    f'Content-Length: {len(jpeg)}\r\n\r\n'.encode() + jpeg + b'\r\n')
                self.wfile.flush()
        except OSError:
            pass  # ブラウザが閉じられた
        finally:
            store.remove_viewer()

    def _client_gone(self) -> bool:
        """ブラウザが接続を閉じたか。閲覧者数をすぐ減らし、pi_a に無駄な JPEG を作らせないため。"""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except OSError:
            return True

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # アクセスごとのログは journald に出さない


class DashboardHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], store: FrameStore,
                 user: str, password: str) -> None:
        self.store = store
        self.user = user
        self.password = password
        super().__init__(addr, _HttpHandler)


INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>desk-monitor</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #111; color: #ddd; font-family: system-ui, sans-serif; }
  main { display: grid; gap: 16px; justify-items: center; padding: 16px; }
  .screen { position: relative; width: min(90vw, 480px); aspect-ratio: 1;
            border-radius: 50%; overflow: hidden; background: #000;
            box-shadow: 0 0 0 6px #222; }
  .screen img { width: 100%; height: 100%; display: block; image-rendering: auto; }
  .screen .msg { position: absolute; inset: 0; display: grid; place-items: center;
                 text-align: center; padding: 24px; background: #000c; }
  .screen .msg[hidden] { display: none; }
  #state { font-size: 14px; color: #999; }
</style>
</head>
<body>
<main>
  <div class="screen">
    <img id="video" alt="カメラ映像">
    <div class="msg" id="msg">接続中…</div>
  </div>
  <div id="state"></div>
</main>
<script>
const video = document.getElementById('video');
const msg = document.getElementById('msg');
const state = document.getElementById('state');
let streaming = false;

function show(text) { msg.textContent = text; msg.hidden = !text; }

async function poll() {
  try {
    const res = await fetch('/status', { cache: 'no-store' });
    const s = await res.json();
    if (s.live) {
      show('');
      state.textContent = 'camera 表示中(検出枠つき)';
    } else if (!s.connected) {
      show('表示プロセスと接続していません');
      state.textContent = '';
    } else if (s.mode === 'face') {
      show('LCD は face 表示中のため映像はありません');
      state.textContent = 'face 表示中';
    } else {
      show('映像を待っています…');
      state.textContent = '';
    }
    // 接続が切れていたら張り直す
    if (!streaming) {
      streaming = true;
      video.src = '/stream.mjpg?t=' + Date.now();
    }
  } catch (e) {
    show('ダッシュボードに接続できません');
  }
}
video.addEventListener('error', () => { streaming = false; });
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description='camera 映像を LAN 内のブラウザに配信する')
    parser.add_argument('--bind', default='0.0.0.0',
                        help='待ち受けるアドレス (既定: 0.0.0.0。IPv4 のみ)')
    parser.add_argument('--http-port', type=int, default=8080,
                        help='ブラウザ向けの HTTP ポート (既定: 8080)')
    parser.add_argument('--feed-port', type=int, default=8081,
                        help='表示プロセスからの接続を受けるポート (既定: 8081)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    password = os.environ.get('DESK_MONITOR_DASHBOARD_PASSWORD', '')
    token = os.environ.get('DESK_MONITOR_FEED_TOKEN', '')
    user = os.environ.get('DESK_MONITOR_DASHBOARD_USER', 'desk')
    if not password or not token:
        print('DESK_MONITOR_DASHBOARD_PASSWORD と DESK_MONITOR_FEED_TOKEN を設定してください'
              '(scripts/setup-dashboard.sh)', file=sys.stderr)
        sys.exit(2)

    store = FrameStore()
    feed = FeedServer((args.bind, args.feed_port), store, token)
    http = DashboardHttpServer((args.bind, args.http_port), store, user, password)
    threading.Thread(target=feed.serve_forever, daemon=True, name='feed').start()
    log.info('ダッシュボードを開始しました: http://%s:%d/ (feed: %d)',
             socket.gethostname(), args.http_port, args.feed_port)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
        feed.shutdown()
        feed.server_close()


if __name__ == '__main__':
    main()
