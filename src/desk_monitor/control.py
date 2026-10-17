"""表示プロセスの制御口(表示用の Pi で、表示プロセスの中で動かす)。

ハブ(開発 PC)が接続してきて、検出結果・操作を送り、表示側の状態と通知を受け取る。
通信の形式は wire.py。

  ハブ → 表示
    {"type": "detections", "items": [{"x1", "y1", "x2", "y2", "score", "cls"}, ...]}
        座標は全視野に対する 0〜1。DETECTION_STALE_SEC 届かなければ枠を消す
    {"type": "mode", "mode": "face" | "camera"}
  表示 → ハブ
    {"type": "status", "display": {...}, "system": {...}}   約 2 秒ごと
    {"type": "notification", "text", "source", "at"}        トレンド監視の通知を受けたとき(notification.py)
"""
from __future__ import annotations

import logging
import socketserver
import threading
import time
from typing import Callable

from desk_monitor import sysstat, wire
from desk_monitor.detector import Detection

log = logging.getLogger(__name__)

STATUS_SEC = 2.0
DETECTION_STALE_SEC = 1.5


class DetectionOverlay:
    """ハブから届いた最新の検出結果(全視野に対する 0〜1 の座標)を持つ。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._at = 0.0

    def update(self, items: list, now: float | None = None) -> None:
        valid = []
        for item in items if isinstance(items, list) else []:
            try:
                valid.append({k: float(item[k]) for k in ('x1', 'y1', 'x2', 'y2', 'score')}
                             | {'cls': int(item['cls'])})
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            self._items = valid
            self._at = time.time() if now is None else now

    def latest(self, width: int, height: int, now: float | None = None) -> list[Detection]:
        """width x height の全視野の画の画素座標で返す。古ければ空。"""
        now = time.time() if now is None else now
        with self._lock:
            if now - self._at > DETECTION_STALE_SEC:
                return []
            items = list(self._items)
        return [Detection(i['x1'] * width, i['y1'] * height, i['x2'] * width, i['y2'] * height,
                          i['score'], i['cls']) for i in items]


class DisplayControl:
    """制御口が表示プロセスを操作するための窓口。"""

    def __init__(self, get_status: Callable[[], dict], set_mode: Callable[[str], None],
                 overlay: DetectionOverlay) -> None:
        self.get_status = get_status
        self.set_mode = set_mode
        self.overlay = overlay
        self._meter = sysstat.CpuMeter()
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[bytes], None]] = []

    def status_message(self) -> bytes:
        return wire.encode({'type': 'status', 'display': self.get_status(),
                            'system': sysstat.snapshot(self._meter)})

    def subscribe(self, send: Callable[[bytes], None]) -> None:
        with self._lock:
            self._subscribers.append(send)

    def unsubscribe(self, send: Callable[[bytes], None]) -> None:
        with self._lock:
            if send in self._subscribers:
                self._subscribers.remove(send)

    def notify(self, text: str, source: str) -> None:
        """トレンド監視の通知を、接続中のハブに中継する。"""
        message = wire.encode({'type': 'notification', 'text': text, 'source': source,
                               'at': time.time()})
        with self._lock:
            subscribers = list(self._subscribers)
        for send in subscribers:
            send(message)

    def handle(self, header: dict) -> None:
        kind = header.get('type')
        if kind == 'detections':
            self.overlay.update(header.get('items'))
        elif kind == 'mode' and header.get('mode') in ('face', 'camera'):
            self.set_mode(header['mode'])


class _Handler(socketserver.BaseRequestHandler):
    server: ControlServer

    def handle(self) -> None:
        host = self.client_address[0]
        accepted = wire.accept_hello(self.request, host, self.server.token)
        if accepted is None:
            log.warning('制御口への接続を拒否しました: %s', host)
            return
        reader, _ = accepted
        control = self.server.control
        lock = threading.Lock()
        closed = threading.Event()

        def send(message: bytes) -> None:
            try:
                with lock:
                    self.request.sendall(message)
            except OSError:
                closed.set()

        def push_status() -> None:
            while not closed.is_set():
                send(control.status_message())
                closed.wait(STATUS_SEC)

        control.subscribe(send)
        threading.Thread(target=push_status, daemon=True, name='control-status').start()
        log.info('ハブが接続しました: %s', host)
        try:
            # ハブは少なくとも STATUS_SEC ごとに何か送ってくるので、長く途切れたら切断とみなす
            self.request.settimeout(STATUS_SEC * 5)
            while not closed.is_set():
                header, _ = reader.read()
                control.handle(header)
        except (OSError, EOFError, wire.ProtocolError):
            pass
        finally:
            closed.set()
            control.unsubscribe(send)
            log.info('ハブとの接続が切れました: %s', host)


class ControlServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], token: str, control: DisplayControl) -> None:
        self.token = token
        self.control = control
        super().__init__(addr, _Handler)

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True, name='control').start()
