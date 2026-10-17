"""ハブ: 映像の物体検出と、ダッシュボードへの情報の集約(開発 PC で動かす。role hub)。

  カメラ配信サーバ(camera_server.py)── H.264 640x480 ──▶ ハブ ── 検出結果 ──▶ 表示プロセス(control.py)
                                                          │
                                                          └──▶ ダッシュボード(http://127.0.0.1:8090/)

- カメラ配信サーバから全視野の H.264 を受けて復号し(PyAV)、物体検出(既定 D-FINE-L の Objects365 版、365 種類)を繰り返す。
  一瞬だけ出る誤検出は DetectionStabilizer で抑える
- 検出結果(全視野に対する 0〜1 の座標)を表示プロセスに送り、円形 LCD に枠を重ねさせる
- 表示プロセスに操作(モード切替)を送り、状態とトレンド監視の通知を受け取る
- ダッシュボードに、検出枠つきの映像・各機器の状態・検出と通知の履歴・操作をまとめる
- 設置場所とカメラの向き(ar.json)があれば、画角内の観光地を映像に重ね、地図に範囲を示す(ar.py)
- 履歴は SQLite(既定 ~/.local/share/desk-monitor/hub.sqlite3)に残す

ダッシュボードは 127.0.0.1 だけで待ち受ける(開発 PC 上のブラウザからのみ見る)。
操作の API は、他のサイトから送らせないよう Host ヘッダと独自ヘッダを確かめる。

環境変数(~/.config/desk-monitor/desk-monitor.env。scripts/setup-secrets.sh で作る):
  DESK_MONITOR_TOKEN, DESK_MONITOR_CAMERA_ADDR(例 192.168.10.111:8082),
  DESK_MONITOR_DISPLAY_ADDR(例 192.168.10.113:8083)

使い方(リポジトリ直下で。通常は deploy/hub の Docker で動かす):
    PYTHONPATH=src python3 -m desk_monitor.hub [--port 8090] [--model models/dfine_l_obj365.onnx]
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
from PIL import Image

from desk_monitor import sysstat, wire
from desk_monitor.ar import CameraConfig, PoiStore
from desk_monitor.detector import Detection, DetectionWorker, draw_detections, load_detector
from desk_monitor.fonts import get_font
from desk_monitor.paths import MODELS_DIR

log = logging.getLogger(__name__)

# D-FINE-L の Objects365 版(365 種類)。COCO 版(80 種類)では眼鏡やイヤホンが「ネクタイ」「花瓶」に
# 誤分類されていたが、Objects365 版は正しい種類名で出た(速さはほぼ同じ)
DEFAULT_MODEL = MODELS_DIR / 'dfine_l_obj365.onnx'
# 機器の状態の推移を残す長さ(状態は約 2 秒ごとに届く)
METRICS_SEC = 600
DEFAULT_DB = Path.home() / '.local' / 'share' / 'desk-monitor' / 'hub.sqlite3'
DEFAULT_AR_CONFIG = Path.home() / '.config' / 'desk-monitor' / 'ar.json'
RETRY_SEC = 5.0
PING_SEC = 2.0
# この秒数見えなくなったら、その種類の検出を「終わった」とみなす(一瞬の見落としで記録を分けない)
PRESENCE_GAP_SEC = 3.0
STREAM_FPS = 10.0


# ─── 履歴 ────────────────────────────────────────────────────────────────

class History:
    """検出と通知の履歴(SQLite)。複数スレッドから使うのでロックで直列化する。"""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY, class_id INTEGER NOT NULL, label TEXT NOT NULL,
                    started_at REAL NOT NULL, ended_at REAL NOT NULL,
                    max_count INTEGER NOT NULL, max_score REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS detections_started ON detections(started_at);
                CREATE INDEX IF NOT EXISTS notifications_at ON notifications(at);
            ''')

    def add_detection(self, class_id: int, label: str, started_at: float, ended_at: float,
                      max_count: int, max_score: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO detections (class_id, label, started_at, ended_at, max_count, max_score)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (class_id, label, started_at, ended_at, max_count, max_score))

    def add_notification(self, at: float, source: str, text: str) -> None:
        with self._lock, self._db:
            self._db.execute('INSERT INTO notifications (at, source, text) VALUES (?, ?, ?)',
                             (at, source, text))

    def notifications_since(self, since: float, limit: int = 20) -> list[dict]:
        """since より後の通知を古い順に返す(ダッシュボードのトースト表示用)。"""
        with self._lock:
            rows = self._db.execute(
                'SELECT at, source, text FROM notifications WHERE at > ? ORDER BY at DESC LIMIT ?',
                (since, limit)).fetchall()
        return [dict(zip(('at', 'source', 'text'), r)) for r in reversed(rows)]

    def recent(self, limit: int = 50) -> dict:
        with self._lock:
            detections = self._db.execute(
                'SELECT label, started_at, ended_at, max_count, max_score FROM detections'
                ' ORDER BY started_at DESC LIMIT ?', (limit,)).fetchall()
            notifications = self._db.execute(
                'SELECT at, source, text FROM notifications ORDER BY at DESC LIMIT ?',
                (limit,)).fetchall()
            counts = self._db.execute(
                'SELECT label, COUNT(*) FROM detections WHERE started_at >= ?'
                ' GROUP BY label ORDER BY COUNT(*) DESC', (time.time() - 86400,)).fetchall()
        return {
            'detections': [dict(zip(('label', 'started_at', 'ended_at', 'max_count', 'max_score'), r))
                           for r in detections],
            'notifications': [dict(zip(('at', 'source', 'text'), r)) for r in notifications],
            'counts_24h': [{'label': label, 'count': n} for label, n in counts],
        }


def _iou(a: Detection, b: Detection) -> float:
    ix = max(0.0, min(a.x2, b.x2) - max(a.x1, b.x1))
    iy = max(0.0, min(a.y2, b.y2) - max(a.y1, b.y1))
    inter = ix * iy
    union = (a.x2 - a.x1) * (a.y2 - a.y1) + (b.x2 - b.x1) * (b.y2 - b.y1) - inter
    return inter / union if union > 0 else 0.0


class DetectionStabilizer:
    """一瞬だけ出る誤検出を抑える。同じ種類がほぼ同じ位置に confirm 回続けて出たものだけを通す。"""

    def __init__(self, confirm: int = 2, iou: float = 0.3) -> None:
        self._confirm = max(1, confirm)
        self._iou = iou
        self._history: collections.deque[list[Detection]] = collections.deque(maxlen=self._confirm - 1)

    def update(self, detections: list[Detection]) -> list[Detection]:
        stable = [d for d in detections
                  if all(any(p.class_id == d.class_id and _iou(p, d) >= self._iou for p in frame)
                         for frame in self._history)
                  and len(self._history) == self._confirm - 1]
        self._history.append(detections)
        return stable


class PresenceTracker:
    """フレームごとの検出結果から「何がいつからいつまで映っていたか」を種類名ごとにまとめる。"""

    def __init__(self, on_end, gap: float = PRESENCE_GAP_SEC) -> None:
        self._on_end = on_end  # on_end(class_id, label, 開始, 終了, 最大数, 最大信頼度)
        self._gap = gap
        self._active: dict[str, dict] = {}

    def update(self, detections: list[Detection], now: float) -> None:
        counts: dict[str, tuple[int, int, float]] = {}
        for d in detections:
            _, n, best = counts.get(d.label, (d.class_id, 0, 0.0))
            counts[d.label] = (d.class_id, n + 1, max(best, d.score))
        for label, (class_id, n, best) in counts.items():
            item = self._active.setdefault(
                label, {'class_id': class_id, 'start': now, 'last': now, 'count': 0, 'score': 0.0})
            item['last'] = now
            item['count'] = max(item['count'], n)
            item['score'] = max(item['score'], best)
        for label in [k for k, item in self._active.items() if now - item['last'] > self._gap]:
            self._finish(label)

    def flush(self) -> None:
        for label in list(self._active):
            self._finish(label)

    def active(self) -> list[dict]:
        return [{'label': label, 'since': item['start'], 'count': item['count']}
                for label, item in sorted(self._active.items(), key=lambda kv: kv[1]['start'])]

    def _finish(self, label: str) -> None:
        item = self._active.pop(label)
        self._on_end(item['class_id'], label, item['start'], item['last'], item['count'], item['score'])


# ─── 状態 ────────────────────────────────────────────────────────────────

class HubState:
    """ハブが集めた最新の情報。ダッシュボードはここから読む。"""

    def __init__(self, history: History) -> None:
        self.history = history
        self._lock = threading.Condition()
        self.frame: np.ndarray | None = None
        self.frame_at = 0.0
        self.frame_seq = 0
        self.detections: list[Detection] = []
        self.detections_at = 0.0
        self.inference_ms: float | None = None
        self.inference_fps = 0.0
        self._inferences: list[float] = []
        self.camera: dict = {'connected': False}
        # 機器ごとの状態の推移。{'t': 時刻, 'cpu': .., 'temp': .., ...} の並び
        self.metrics: dict[str, collections.deque] = {
            name: collections.deque(maxlen=METRICS_SEC) for name in ('camera', 'display', 'hub')}
        self.display: dict = {'connected': False}
        self.tracker = PresenceTracker(history.add_detection)
        self._meter = sysstat.CpuMeter()

    def record(self, device: str, values: dict, now: float) -> None:
        """状態を推移として残す(ダッシュボードのグラフに使う)。"""
        series = self.metrics[device]
        series.append({'t': round(now, 1), **values})
        while series and now - series[0]['t'] > METRICS_SEC:
            series.popleft()

    def record_camera(self, header: dict, now: float) -> None:
        system = header.get('system') if isinstance(header.get('system'), dict) else {}
        fps = ((header.get('camera') or {}).get('fps') or {}) if isinstance(header.get('camera'), dict) else {}
        self.record('camera', {'cpu': system.get('cpu'), 'temp': system.get('temp'),
                               'memory': system.get('memory'), 'fps': fps.get('h264')}, now)

    def record_display(self, header: dict, now: float) -> None:
        system = header.get('system') if isinstance(header.get('system'), dict) else {}
        display = header.get('display') if isinstance(header.get('display'), dict) else {}
        self.record('display', {'cpu': system.get('cpu'), 'temp': system.get('temp'),
                                'memory': system.get('memory'), 'fps': display.get('fps')}, now)

    def metrics_snapshot(self) -> dict:
        return {name: list(series) for name, series in self.metrics.items()}

    def set_frame(self, frame: np.ndarray, now: float) -> None:
        with self._lock:
            self.frame, self.frame_at = frame, now
            self.frame_seq += 1
            self._lock.notify_all()

    def wait_frame(self, last_seq: int, timeout: float) -> int:
        with self._lock:
            self._lock.wait_for(lambda: self.frame_seq != last_seq, timeout=timeout)
            return self.frame_seq

    def set_detections(self, detections: list[Detection], now: float, inference_ms: float) -> None:
        with self._lock:
            self.detections, self.detections_at = detections, now
            self.inference_ms = inference_ms
            self._inferences = [t for t in self._inferences if now - t < 5.0] + [now]
            self.inference_fps = round(len(self._inferences) / 5.0, 1)
            self.tracker.update(detections, now)

    def expire(self, now: float) -> None:
        """推論結果が届かなくなったら(カメラ停止など)、映っていた物の記録を締める。"""
        with self._lock:
            if now - self.detections_at > 1.0:
                self.tracker.update([], now)
                self.inference_fps = round(len([t for t in self._inferences if now - t < 5.0]) / 5.0, 1)
            fps, ms = self.inference_fps, self.inference_ms
        # ハブ自身(開発 PC)の状態。コンテナ内でも /proc と /sys はホストの値を返す
        system = sysstat.snapshot(self._meter)
        self.record('hub', {'cpu': system['cpu'], 'temp': system['temp'], 'memory': system['memory'],
                            'fps': fps, 'ms': round(ms, 1) if ms is not None else None}, now)

    def annotated_jpeg(self, font) -> bytes | None:
        with self._lock:
            frame = self.frame
            detections = self.detections if time.time() - self.detections_at < 1.0 else []
        if frame is None:
            return None
        if detections:
            frame = draw_detections(frame, detections, font(frame.shape[1]), circular=False)
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format='JPEG', quality=80)
        return buf.getvalue()

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            live = self.frame is not None and now - self.frame_at < 2.0
            detections = self.detections if now - self.detections_at < 1.0 else []
            return {
                'now': now,
                'camera': self.camera,
                'display': self.display,
                'hub': {
                    'live': live,
                    'inference_ms': self.inference_ms,
                    'inference_fps': self.inference_fps,
                },
                'detections': [{'label': d.label, 'score': round(d.score, 2)} for d in detections],
                'present': self.tracker.active(),
            }


# ─── 機器との接続 ──────────────────────────────────────────────────────────

def _decoder():
    import av  # ハブでだけ使う
    ctx = av.CodecContext.create('h264', 'r')
    # フレーム単位のマルチスレッド復号は数フレーム遅れるので、遅延の少ないスライス単位にする
    ctx.thread_type = 'SLICE'
    return ctx


class CameraLink:
    """カメラ配信サーバから H.264 を受けて復号し、推論に回す。"""

    def __init__(self, addr: tuple[str, int], token: str, state: HubState,
                 worker: DetectionWorker | None) -> None:
        self._addr, self._token = addr, token
        self._state, self._worker = state, worker

    def run_forever(self) -> None:
        while True:
            try:
                sock, reader = wire.connect(self._addr, self._token, stream='h264')
                with sock:
                    decoder = _decoder()
                    log.info('カメラ配信に接続しました: %s:%d', *self._addr)
                    while True:
                        header, payload = reader.read()
                        self._state.camera = {**self._state.camera, 'connected': True}
                        if header.get('type') == 'status':
                            self._state.camera = {'connected': True,
                                                  **{k: header.get(k) for k in ('camera', 'system')}}
                            self._state.record_camera(header, time.time())
                        elif header.get('type') == 'frame':
                            self._decode(decoder, payload)
            except (OSError, EOFError, wire.ProtocolError) as exc:
                if self._state.camera.get('connected'):
                    log.warning('カメラ配信との接続が切れました: %r', exc)
                self._state.camera = {'connected': False, 'error': repr(exc)}
            time.sleep(RETRY_SEC)

    def _decode(self, decoder, payload: bytes) -> None:
        import av
        try:
            # カメラのエンコーダは 1 フレーム分をまとめて出すので、そのまま 1 パケットとして復号する
            # (parse() は次のフレームが届くまで今のフレームを出さず、1 フレーム遅れる)
            frames = decoder.decode(av.Packet(payload))
        except Exception as exc:  # 壊れたパケットは捨てて次のキーフレームを待つ
            log.warning('H.264 を復号できません: %r', exc)
            return
        for frame in frames:
            rgb = frame.to_ndarray(format='rgb24')
            self._state.set_frame(rgb, time.time())
            if self._worker is not None:
                self._worker.submit(rgb)


class DisplayLink:
    """表示プロセスの制御口に接続し、検出結果と操作を送り、状態と通知を受け取る。"""

    def __init__(self, addr: tuple[str, int], token: str, state: HubState) -> None:
        self._addr, self._token = addr, token
        self._state = state
        self._outbox: queue.Queue[dict] = queue.Queue(maxsize=100)

    def send(self, header: dict) -> bool:
        """表示プロセスに送る。つながっていなければ捨てて False。"""
        if not self._state.display.get('connected'):
            return False
        try:
            self._outbox.put_nowait(header)
        except queue.Full:
            return False
        return True

    def send_detections(self, detections: list[Detection], width: int, height: int) -> None:
        items = [{'x1': d.x1 / width, 'y1': d.y1 / height, 'x2': d.x2 / width,
                  'y2': d.y2 / height, 'score': round(d.score, 3), 'cls': d.class_id,
                  'label': d.label}
                 for d in detections]
        self.send({'type': 'detections', 'items': items})

    def run_forever(self) -> None:
        while True:
            try:
                sock, reader = wire.connect(self._addr, self._token, timeout=10.0)
                with sock:
                    self._state.display = {'connected': True}
                    while not self._outbox.empty():  # 前の接続の残りは古いので捨てる
                        self._outbox.get_nowait()
                    log.info('表示プロセスに接続しました: %s:%d', *self._addr)
                    sender = threading.Thread(target=self._send_loop, args=(sock,), daemon=True)
                    sender.start()
                    while True:
                        header, _ = reader.read()
                        self._handle(header)
            except (OSError, EOFError, wire.ProtocolError) as exc:
                if self._state.display.get('connected'):
                    log.warning('表示プロセスとの接続が切れました: %r', exc)
                self._state.display = {'connected': False, 'error': repr(exc)}
            time.sleep(RETRY_SEC)

    def _handle(self, header: dict) -> None:
        kind = header.get('type')
        if kind == 'status':
            self._state.display = {'connected': True,
                                   **{k: header.get(k) for k in ('display', 'system')}}
            self._state.record_display(header, time.time())
        elif kind == 'notification':
            self._state.history.add_notification(
                float(header.get('at') or time.time()), str(header.get('source', '')),
                str(header.get('text', '')))

    def _send_loop(self, sock) -> None:
        try:
            while self._state.display.get('connected'):
                try:
                    header = self._outbox.get(timeout=PING_SEC)
                except queue.Empty:
                    header = {'type': 'ping'}
                wire.send(sock, header)
        except OSError:
            pass


# ─── ダッシュボード ────────────────────────────────────────────────────────

_ALLOWED_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}


class _DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = 'desk-monitor-hub'
    sys_version = ''

    def _host_ok(self) -> bool:
        # DNS リバインディング対策: ブラウザが送る Host がローカルのときだけ応じる
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0]
        return host in _ALLOWED_HOSTS

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self.send_error(403)
            return
        url = urlsplit(self.path)
        if url.path == '/':
            self._send(200, 'text/html; charset=utf-8', DASHBOARD_HTML.encode())
        elif url.path == '/api/state':
            self._json(self.server.state.snapshot())
        elif url.path == '/api/notifications':
            try:
                since = float(parse_qs(url.query).get('since', ['0'])[0])
            except ValueError:
                since = 0.0
            self._json({'notifications': self.server.state.history.notifications_since(since)})
        elif url.path == '/api/ar':
            ar = self.server.ar
            self._json(ar.snapshot() if ar is not None else {'camera': None, 'pois': []})
        elif url.path == '/api/metrics':
            self._json(self.server.state.metrics_snapshot())
        elif url.path == '/api/history':
            limit = parse_qs(url.query).get('limit', ['50'])[0]
            self._json(self.server.state.history.recent(max(1, min(500, int(limit)))
                                                         if limit.isdigit() else 50))
        elif url.path == '/stream.mjpg':
            self._stream()
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        # 他のサイトのページから送らせない: 独自ヘッダ付きのリクエストはブラウザが事前確認(CORS)
        # を挟み、このサーバは許可しないので、ダッシュボード自身からしか送れない
        if not self._host_ok() or self.headers.get('X-Desk-Monitor') != '1':
            self.send_error(403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(min(length, 4096)) or b'{}')
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        url = urlsplit(self.path)
        link = self.server.display
        if url.path == '/api/ar/view' and self.server.ar is not None:
            try:
                self.server.ar.set_view(body)
            except (ValueError, TypeError) as exc:
                self._json({'ok': False, 'error': str(exc)}, 400)
                return
            self._json(self.server.ar.snapshot())
            return
        if url.path == '/api/mode' and body.get('mode') in ('face', 'camera'):
            ok = link.send({'type': 'mode', 'mode': body['mode']})
        else:
            self.send_error(400)
            return
        self._json({'ok': ok}, 200 if ok else 503)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, 'application/json', json.dumps(data, ensure_ascii=False).encode())

    def _stream(self) -> None:
        state = self.server.state
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        seq, sent_at = 0, 0.0
        try:
            while True:
                seq = state.wait_frame(seq, timeout=5.0)
                # 送る頻度を抑える(復号は 20fps でも、ブラウザには STREAM_FPS まで)
                wait = sent_at + 1.0 / STREAM_FPS - time.time()
                if wait > 0:
                    time.sleep(wait)
                jpeg = state.annotated_jpeg(self.server.font)
                if jpeg is None:
                    continue
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n'
                                 + f'Content-Length: {len(jpeg)}\r\n\r\n'.encode() + jpeg + b'\r\n')
                self.wfile.flush()
                sent_at = time.time()
        except OSError:
            pass

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], state: HubState, display: DisplayLink,
                 ar: PoiStore | None = None) -> None:
        self.state = state
        self.display = display
        self.ar = ar
        self._fonts: dict[int, object] = {}
        super().__init__(addr, _DashboardHandler)

    def font(self, width: int):
        """映像の幅に合わせたラベルの文字(640 幅で 16px)。"""
        size = max(12, round(16 * width / 640))
        if size not in self._fonts:
            self._fonts[size] = get_font(size)
        return self._fonts[size]


DASHBOARD_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>desk-monitor</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
  :root { color-scheme: dark; --bg: #111; --panel: #1b1b1b; --line: #2a2a2a; --text: #ddd;
          --muted: #8a8a8a; --ok: #4caf50; --warn: #ffb300; --ng: #ef5350; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 system-ui, sans-serif; display: flex; flex-direction: column; }
  header { padding: 8px 16px; border-bottom: 1px solid var(--line); display: flex;
           align-items: center; gap: 12px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; }
  /* 1 画面に収める: 映像は残りの高さいっぱいに、右の列は中身だけスクロールする */
  main { flex: 1; min-height: 0; display: grid; gap: 12px; padding: 12px;
         grid-template-columns: minmax(0, 1fr) minmax(300px, 380px); }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px;
            min-height: 0; }
  h2 { font-size: 13px; color: var(--muted); margin: 0 0 6px; font-weight: 600; }
  .camera { display: flex; flex-direction: column; }
  .video { position: relative; flex: 1; min-height: 0; background: #000; border-radius: 6px; overflow: hidden; }
  .video img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; }
  .video .msg { position: absolute; inset: 0; display: grid; place-items: center; background: #000c; }
  .video .msg[hidden] { display: none; }
  .side { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  .kv { color: var(--muted); font-size: 12px; }
  .warn { color: var(--warn); }
  /* 右の列の残りの高さを、選んだ機器の 3 つのグラフで分ける */
  .charts { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .map-section { flex: 1 1 0; min-height: 220px; display: flex; flex-direction: column; }
  #map { flex: 1; min-height: 160px; border-radius: 6px; background: #222; }
  #arInfo { margin-top: 4px; }
  #arOverlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  .gizmo { position: absolute; top: 8px; right: 8px; display: flex; flex-direction: column; align-items: center;
           gap: 2px; z-index: 2; }
  .gizmo[hidden] { display: none; }
  /* 3D ツールの視点ギズモ風: すりガラスの円に、方位の目盛りリングと 3 軸 */
  #gizmo { width: 112px; height: 112px; border-radius: 50%; cursor: grab; touch-action: none;
           background: radial-gradient(circle at 35% 30%, #ffffff1f, #ffffff08 60%, #00000033);
           backdrop-filter: blur(8px) saturate(140%); -webkit-backdrop-filter: blur(8px) saturate(140%);
           border: 1px solid #ffffff26; box-shadow: 0 6px 20px #00000080, inset 0 1px 0 #ffffff1f;
           transition: box-shadow .15s ease, border-color .15s ease; }
  #gizmo:hover, #gizmo.dragging { border-color: #64b5f6aa;
           box-shadow: 0 6px 24px #00000090, 0 0 0 3px #42a5f533, inset 0 1px 0 #ffffff26; }
  #gizmo.dragging { cursor: grabbing; }
  #gizmo:focus-visible { outline: 2px solid #42a5f5; outline-offset: 3px; }
  .gizmo-text { font: 500 11px/1.6 system-ui, sans-serif; color: #e0e0e0; padding: 0 10px; border-radius: 999px;
                background: #14161ab3; border: 1px solid #ffffff1a; backdrop-filter: blur(8px);
                -webkit-backdrop-filter: blur(8px); font-variant-numeric: tabular-nums; letter-spacing: .02em; }
  .gizmo-text b { color: #fff; font-weight: 600; }
  .range-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
  .range { position: relative; flex: 1; height: 22px; }
  .range-track { position: absolute; left: 0; right: 0; top: 9px; height: 4px; border-radius: 2px; background: #333; }
  .range-fill { position: absolute; top: 0; bottom: 0; background: #ffa726; border-radius: 2px; }
  /* 2 つの range を重ね、つまみだけ操作できるようにする(1 本のバーで開始〜終了を選ぶ) */
  .range input { position: absolute; left: 0; width: 100%; top: 0; height: 22px; margin: 0; background: none;
                 pointer-events: none; -webkit-appearance: none; appearance: none; }
  .range input::-webkit-slider-thumb { pointer-events: auto; -webkit-appearance: none; width: 14px; height: 14px;
                 border-radius: 50%; background: #fff; border: 2px solid #ffa726; cursor: pointer; }
  .range input::-moz-range-thumb { pointer-events: auto; width: 12px; height: 12px; border-radius: 50%;
                 background: #fff; border: 2px solid #ffa726; cursor: pointer; }
  .range input::-moz-range-track { background: none; }
  .range input:focus-visible::-webkit-slider-thumb { outline: 2px solid #42a5f5; }
  #rangeText { min-width: 92px; text-align: right; font-variant-numeric: tabular-nums; }
  .device-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin-bottom: 8px; }
  .device-tabs [role="tab"] { background: none; border: 0; border-bottom: 2px solid transparent;
                              border-radius: 0; padding: 4px 10px; color: var(--muted); }
  .device-tabs [role="tab"][aria-selected="true"] { color: var(--text); border-bottom-color: #42a5f5; }
  .chart-panel { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 8px; }
  .chart-panel canvas { flex: 1; min-height: 80px; width: 100%; background: #161616;
                        border: 1px solid var(--line); border-radius: 6px; display: block; }
  button { background: #333; color: var(--text); border: 1px solid #444; border-radius: 6px;
           padding: 4px 12px; cursor: pointer; }
  button[aria-pressed="true"] { background: #1565c0; border-color: #1e88e5; }
  form { display: flex; gap: 6px; margin-top: 6px; }
  input { flex: 1; min-width: 0; background: #222; color: var(--text); border: 1px solid #444;
          border-radius: 6px; padding: 4px 6px; }
  .toasts { position: fixed; right: 16px; bottom: 16px; z-index: 20; display: flex; flex-direction: column;
            gap: 8px; width: min(360px, calc(100vw - 32px)); pointer-events: none; }
  .toast { pointer-events: auto; cursor: pointer; background: #263238; border: 1px solid #37474f;
           border-left: 4px solid var(--src, #90a4ae); border-radius: 6px; padding: 8px 10px;
           box-shadow: 0 4px 16px #0008; animation: toast-in .2s ease; }
  .toast .kv { display: block; }
  .toast.leaving { opacity: 0; transition: opacity .3s ease; }
  @keyframes toast-in { from { opacity: 0; transform: translateY(8px); } }
  @media (prefers-reduced-motion: reduce) { .toast { animation: none; } }
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(440px, 88vw); z-index: 10;
            background: var(--panel); border-left: 1px solid var(--line);
            transform: translateX(100%); transition: transform .2s ease; }
  .drawer.open { transform: none; box-shadow: -8px 0 24px #0008; }
  .drawer-body { height: 100%; padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .drawer-tabs { position: absolute; top: 50%; right: 100%; transform: translateY(-50%);
                 display: flex; flex-direction: column; gap: 6px; }
  .drawer-tab { writing-mode: vertical-rl; letter-spacing: .1em; padding: 14px 6px;
                background: var(--panel); color: var(--text); border: 1px solid var(--line); border-right: 0;
                border-radius: 8px 0 0 8px; box-shadow: -2px 0 8px #0006; }
  .drawer-tab:hover { background: #263238; }
  .drawer-tab[aria-expanded="true"] { background: #263238; border-left: 3px solid #42a5f5; }
  .drawer-panel { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 6px; }
  .drawer-panel[hidden] { display: none; }
  @media (prefers-reduced-motion: reduce) { .drawer { transition: none; } }
  .drawer-head { display: flex; align-items: center; justify-content: space-between; }
  .drawer-head h2 { margin: 0; }
  .scroll { flex: 1; min-height: 0; overflow: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  td, th { text-align: left; padding: 3px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; position: sticky; top: 0; background: var(--panel); }
  /* 狭い画面では縦に並べ、ページ全体をスクロールする(映像は画面の 70% まで) */
  @media (max-width: 900px) {
    html, body { height: auto; }
    main { grid-template-columns: 1fr; }
    .video { flex: none; aspect-ratio: 4 / 3; max-height: 70vh; }
    .scroll { max-height: 50vh; }
    .chart-panel canvas { flex: none; height: 120px; }
  }
</style>
</head>
<body>
<header>
  <h1>desk-monitor</h1>
  <span class="kv" id="clock"></span>
</header>
<main>
  <section class="camera">
    <h2>カメラ(全視野・検出枠つき)</h2>
    <div class="video">
      <img id="video" alt="カメラ映像">
      <canvas id="arOverlay" aria-hidden="true"></canvas>
      <!-- カメラの向きを変える 3 軸のアイコン(ドラッグ: 方位と傾き、Shift+ドラッグかホイール: 回転、矢印キーでも可) -->
      <div class="gizmo" id="gizmoBox">
        <canvas id="gizmo" tabindex="0" role="slider" title="ドラッグ: 方位と傾き / Shift+ドラッグ・ホイール: 回転 / ダブルクリック: 傾きと回転を 0 に"
                aria-label="カメラの向き(左右の矢印で方位、上下で傾き、Shift+左右で回転)"
                aria-valuemin="0" aria-valuemax="359" aria-valuenow="0"></canvas>
        <div class="gizmo-text" id="gizmoText"></div>
      </div>
      <div class="msg" id="videoMsg">接続中…</div>
    </div>
  </section>
  <div class="side">
    <section>
      <h2>操作</h2>
      <div style="display:flex; gap:6px">
        <button id="modeFace" data-mode="face">face</button>
        <button id="modeCamera" data-mode="camera">camera</button>
      </div>
      <label class="kv" style="display:block; margin-top:6px"><input type="checkbox" id="arToggle" checked> 観光地のラベルを映像に重ねる</label>
      <div class="kv" id="opMsg"></div>
    </section>
    <section class="map-section">
      <h2>観光地の範囲(扇形: カメラの画角と距離の上限)</h2>
      <div id="map" role="img" aria-label="設置場所とラベルを出す範囲の地図"></div>
      <div class="range-row">
        <span class="kv">距離</span>
        <div class="range" id="rangeBox">
          <div class="range-track"><div class="range-fill" id="rangeFill"></div></div>
          <input type="range" id="rangeMin" min="0" max="50" step="0.5" value="0" aria-label="ラベルを出す距離の開始(km)">
          <input type="range" id="rangeMax" min="0" max="50" step="0.5" value="20" aria-label="ラベルを出す距離の終了(km)">
        </div>
        <span class="kv" id="rangeText"></span>
      </div>
      <div class="kv" id="arInfo"></div>
    </section>
  </div>
</main>
<!-- 通知のトースト。読み上げはするがフォーカスは移さない(フォーカスを受け取る要素を置かない) -->
<div class="toasts" id="toasts" role="status" aria-live="polite"></div>
<!-- サイドパネル(通常は閉じておき、画面の右端のタブで開く。タブごとに中身を切り替える) -->
<aside class="drawer" id="drawer" aria-labelledby="drawerTitle">
  <!-- 画面の右端に出ているタブ。パネルの左端に付いていて、開くと一緒に動く -->
  <div class="drawer-tabs">
    <button class="drawer-tab" data-panel="historyPanel" aria-controls="drawerBody" aria-expanded="false">検出履歴</button>
    <button class="drawer-tab" data-panel="metricsPanel" aria-controls="drawerBody" aria-expanded="false">メトリクス</button>
  </div>
  <div class="drawer-body" id="drawerBody" inert>
    <div class="drawer-head">
      <h2 id="drawerTitle">検出履歴</h2>
      <button id="drawerClose" aria-label="閉じる">×</button>
    </div>
    <div class="drawer-panel" id="historyPanel" data-title="検出履歴">
      <div class="kv" id="counts"></div>
      <div class="scroll">
        <table id="detections"><thead><tr><th>種類</th><th>時刻</th><th>長さ</th><th>数</th><th>信頼度</th></tr></thead><tbody></tbody></table>
      </div>
    </div>
    <div class="drawer-panel" id="metricsPanel" data-title="メトリクス(直近 10 分)" hidden>
      <div class="charts">
        <div class="device-tabs" role="tablist" aria-label="機器">
          <button role="tab" id="tab-display" data-device="display" aria-selected="true" aria-controls="chartsPanel">pi_a(LCD)</button>
          <button role="tab" id="tab-camera" data-device="camera" aria-selected="false" aria-controls="chartsPanel" tabindex="-1">pi_b(カメラ)</button>
          <button role="tab" id="tab-hub" data-device="hub" aria-selected="false" aria-controls="chartsPanel" tabindex="-1">開発PC</button>
        </div>
        <div class="chart-panel" id="chartsPanel" role="tabpanel" aria-labelledby="tab-display">
          <canvas id="chartTemp" aria-label="温度の推移"></canvas>
          <canvas id="chartCpu" aria-label="CPU 使用率の推移"></canvas>
          <canvas id="chartMem" aria-label="メモリ使用率の推移"></canvas>
        </div>
      </div>
    </div>
  </div>
</aside>
<script>
const $ = (id) => document.getElementById(id);
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString('ja-JP');
const fmtDate = (t) => new Date(t * 1000).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtDur = (s) => s < 60 ? `${Math.round(s)}秒` : `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`;
const n = (v, d = 0, unit = '') => v == null ? '–' : `${Number(v).toFixed(d)}${unit}`;
function cell(text) { const td = document.createElement('td'); td.textContent = text; return td; }
function row(cells) { const tr = document.createElement('tr'); cells.forEach((c) => tr.append(cell(c))); return tr; }

let streaming = false;
const video = $('video');
video.addEventListener('error', () => { streaming = false; });

let metrics = { camera: [], display: [], hub: [] };

// ─── 時系列グラフ(直近 10 分。時間軸は毎秒流す) ───
const WINDOW_SEC = 600;
// 機器ごとの色と温度の目安(Pi はスロットリングが始まる 80°C、開発 PC は Ryzen の上限に近い 95°C)
const DEVICES = {
  display: { color: '#ffa726', tempMax: 90, tempGuide: 80 },
  camera: { color: '#42a5f5', tempMax: 90, tempGuide: 80 },
  hub: { color: '#66bb6a', tempMax: 100, tempGuide: 95 },
};
let selectedDevice = 'display';
const CHARTS = [
  { id: 'chartTemp', title: '温度 °C', key: 'temp', min: 40, digits: 1 },
  { id: 'chartCpu', title: 'CPU %', key: 'cpu', min: 0, max: 100, digits: 0 },
  { id: 'chartMem', title: 'メモリ %', key: 'memory', min: 0, max: 100, digits: 0 },
];

function drawChart(chart, now) {
  const canvas = $(chart.id);
  if (!canvas) return;
  const device = DEVICES[selectedDevice];
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const left = 30, right = 6, top = 18, bottom = 14;
  const pw = w - left - right, ph = h - top - bottom;
  const series = (metrics[selectedDevice] ?? []).filter((p) => p[chart.key] != null && now - p.t <= WINDOW_SEC + 5);
  const guide = chart.key === 'temp' ? device.tempGuide : null;
  // 値が範囲を超えたら上限を広げる
  let max = chart.key === 'temp' ? device.tempMax : chart.max;
  for (const p of series) max = Math.max(max, p[chart.key]);
  const min = Math.min(chart.min, ...series.map((p) => p[chart.key]));
  const x = (t) => left + (1 - (now - t) / WINDOW_SEC) * pw;
  const y = (v) => top + ph - (v - min) / (max - min) * ph;

  ctx.font = '10px system-ui, sans-serif';
  ctx.fillStyle = '#8a8a8a'; ctx.strokeStyle = '#2a2a2a'; ctx.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const v = min + (max - min) * i / 2, yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(left + pw, yy); ctx.stroke();
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(String(Math.round(v)), left - 4, yy);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const m of [10, 5, 0]) ctx.fillText(m ? `-${m}分` : '今', x(now - m * 60), top + ph + 2);
  if (guide != null) {
    ctx.setLineDash([3, 3]); ctx.strokeStyle = '#ef535099';
    ctx.beginPath(); ctx.moveTo(left, y(guide)); ctx.lineTo(left + pw, y(guide)); ctx.stroke();
    ctx.setLineDash([]);
  }

  // タイトルと最新値(目安を超えていたら赤)
  const last = series.at(-1)?.[chart.key];
  ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  ctx.fillStyle = '#ddd'; ctx.font = '600 11px system-ui, sans-serif';
  ctx.fillText(chart.title, 4, 3);
  const titleW = ctx.measureText(chart.title).width;
  ctx.fillStyle = last != null && guide != null && last >= guide ? '#ef5350' : device.color;
  ctx.fillText(last == null ? '–' : Number(last).toFixed(chart.digits), 4 + titleW + 8, 3);

  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, pw, ph); ctx.clip();
  ctx.strokeStyle = device.color; ctx.lineWidth = 1.5; ctx.beginPath();
  let prev = null;
  for (const p of series) {
    // 6 秒以上途切れたら(切断など)線をつながない
    if (!prev || p.t - prev.t > 6) ctx.moveTo(x(p.t), y(p[chart.key]));
    else ctx.lineTo(x(p.t), y(p[chart.key]));
    prev = p;
  }
  ctx.stroke();
  ctx.restore();
}

function drawCharts() {
  const now = Date.now() / 1000;
  for (const chart of CHARTS) drawChart(chart, now);
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = text;
  return e;
}

async function refreshState() {
  try {
    const s = await (await fetch('/api/state', { cache: 'no-store' })).json();
    $('clock').textContent = fmtTime(s.now);
    const showing = s.display?.display?.showing;
    if (s.hub.live) { $('videoMsg').hidden = true; }
    else { $('videoMsg').hidden = false; $('videoMsg').textContent = s.camera.connected ? '映像を待っています…' : 'カメラ配信に接続していません'; }
    if (!streaming) { streaming = true; video.src = '/stream.mjpg?t=' + Date.now(); }

    const disp = s.display.display ?? {};
    for (const b of document.querySelectorAll('[data-mode]')) b.setAttribute('aria-pressed', String(disp.mode === b.dataset.mode));
  } catch (e) {
    $('videoMsg').hidden = false; $('videoMsg').textContent = 'ハブに接続できません';
  }
}

async function refreshHistory() {
  try {
    const h = await (await fetch('/api/history?limit=100', { cache: 'no-store' })).json();
    $('counts').textContent = h.counts_24h.length ? '24 時間: ' + h.counts_24h.map((c) => `${c.label} ${c.count}`).join('、') : '';
    $('detections').tBodies[0].replaceChildren(...h.detections.map((d) => row(
      [d.label, fmtDate(d.started_at), fmtDur(d.ended_at - d.started_at), String(d.max_count), `${Math.round(d.max_score * 100)}%`])));
  } catch (e) { /* 次の周期で再試行 */ }
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desk-Monitor': '1' }, body: JSON.stringify(body) });
  $('opMsg').textContent = res.ok ? '' : '表示プロセスに接続していないため送れませんでした';
  return res.ok;
}
for (const b of document.querySelectorAll('[data-mode]')) b.addEventListener('click', () => post('/api/mode', { mode: b.dataset.mode }).then(refreshState));

// ─── 通知のトースト ───
const TOAST_SEC = 8, MAX_TOASTS = 4;
const SOURCE_LABEL = { bluesky: 'Bluesky', trends_rss: 'ニュース・トレンド', test: 'テスト' };
const SOURCE_COLOR = { bluesky: '#0085ff', trends_rss: '#ff6600', test: '#90a4ae' };
let notifiedUntil = Date.now() / 1000;  // 開く前の通知はトーストにしない

function dismiss(toast) {
  toast.classList.add('leaving');
  setTimeout(() => toast.remove(), 300);
}

function showToast(x) {
  const box = $('toasts');
  const toast = el('div', 'toast');
  toast.style.setProperty('--src', SOURCE_COLOR[x.source] ?? '#90a4ae');
  toast.append(el('span', 'kv', `${SOURCE_LABEL[x.source] ?? x.source}・${fmtTime(x.at)}`), el('div', '', x.text));
  toast.addEventListener('click', () => dismiss(toast));
  box.append(toast);
  while (box.children.length > MAX_TOASTS) box.firstElementChild.remove();
  setTimeout(() => dismiss(toast), TOAST_SEC * 1000);
}

async function pollNotifications() {
  try {
    const res = await (await fetch(`/api/notifications?since=${notifiedUntil}`, { cache: 'no-store' })).json();
    for (const x of res.notifications) {
      notifiedUntil = Math.max(notifiedUntil, x.at);
      showToast(x);
    }
  } catch (e) { /* 次の周期で */ }
}

// サイドパネル: タブごとに中身を切り替える。開いているタブをもう一度押すと閉じる
const drawerTabs = [...document.querySelectorAll('.drawer-tab')];
let drawerPanel = null;  // 開いているパネルの id(閉じていれば null)
let lastDrawerTab = drawerTabs[0];
function setDrawer(panelId) {
  drawerPanel = panelId;
  $('drawer').classList.toggle('open', !!panelId);
  $('drawerBody').inert = !panelId;  // 閉じている間は中身にフォーカスが入らないようにする
  for (const tab of drawerTabs) tab.setAttribute('aria-expanded', String(tab.dataset.panel === panelId));
  for (const panel of document.querySelectorAll('.drawer-panel')) panel.hidden = panel.id !== panelId;
  if (panelId) $('drawerTitle').textContent = $(panelId).dataset.title;
  if (panelId === 'historyPanel') refreshHistory();
  if (panelId === 'metricsPanel') drawCharts();
}
function closeDrawer() { setDrawer(null); lastDrawerTab.focus(); }
for (const tab of drawerTabs) tab.addEventListener('click', () => {
  lastDrawerTab = tab;
  setDrawer(drawerPanel === tab.dataset.panel ? null : tab.dataset.panel);
});
$('drawerClose').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && drawerPanel) closeDrawer(); });

// 機器のタブ(左右の矢印キーでも切り替える)
const deviceTabs = [...document.querySelectorAll('[role="tab"][data-device]')];
function selectDevice(tab) {
  for (const t of deviceTabs) {
    const selected = t === tab;
    t.setAttribute('aria-selected', String(selected));
    t.tabIndex = selected ? 0 : -1;
  }
  selectedDevice = tab.dataset.device;
  $('chartsPanel').setAttribute('aria-labelledby', tab.id);
  drawCharts();
}
for (const tab of deviceTabs) {
  tab.addEventListener('click', () => selectDevice(tab));
  tab.addEventListener('keydown', (e) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    const next = deviceTabs[(deviceTabs.indexOf(tab) + step + deviceTabs.length) % deviceTabs.length];
    next.focus(); selectDevice(next);
  });
}

async function refreshMetrics() {
  try { metrics = await (await fetch('/api/metrics', { cache: 'no-store' })).json(); } catch (e) { /* 次の周期で */ }
}

// ─── 観光地の AR と地図 ───
let ar = null;      // ハブから受けた設定と観光地(方位・距離)
let view = null;    // いまの向きと距離の範囲(ダッシュボードで動かす)
let map = null;
const rad = (d) => d * Math.PI / 180;
const deg = (r) => r * 180 / Math.PI;

// 映像(object-fit: contain)が枠の中で実際に描かれている範囲
function videoRect() {
  const box = video.getBoundingClientRect();
  const iw = video.naturalWidth || 4, ih = video.naturalHeight || 3;
  const scale = Math.min(box.width / iw, box.height / ih);
  const w = iw * scale, h = ih * scale;
  return { x: (box.width - w) / 2, y: (box.height - h) / 2, w, h, bw: box.width, bh: box.height };
}

function fmtKm(m) { return m >= 10000 ? `${Math.round(m / 1000)}km` : `${(m / 1000).toFixed(1)}km`; }

// カメラの座標軸(東・北・上の座標で、右・上・前)。方位→傾き→回転の順に回す
function cameraBasis(v) {
  const h = rad(v.heading), p = rad(v.pitch), r = rad(v.roll);
  const f = [Math.sin(h) * Math.cos(p), Math.cos(h) * Math.cos(p), Math.sin(p)];
  const r0 = [Math.cos(h), -Math.sin(h), 0];
  const u0 = [-Math.sin(h) * Math.sin(p), -Math.cos(h) * Math.sin(p), Math.cos(p)];
  const right = r0.map((x, i) => x * Math.cos(r) - u0[i] * Math.sin(r));
  const up = u0.map((x, i) => x * Math.cos(r) + r0[i] * Math.sin(r));
  return { f, right, up };
}
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

// 方位 az(度)・仰角 0 の方向が、映像のどこ(0〜1)に来るか。カメラの後ろなら null
function projectDir(az, basis, c) {
  const d = [Math.sin(rad(az)), Math.cos(rad(az)), 0];
  const z = dot(d, basis.f);
  if (z <= 1e-6) return null;
  let x = 0.5 + dot(d, basis.right) / z / (2 * Math.tan(rad(c.hfov / 2)));
  const y = 0.5 - dot(d, basis.up) / z / (2 * Math.tan(rad(c.vfov / 2)));
  if (c.mirror) x = 1 - x;
  return { x, y };
}

// 映像に出す観光地: 画面内かつ距離の範囲内で、近い順に max_labels 件
function visiblePois() {
  if (!ar?.camera || !view) return [];
  const basis = cameraBasis(view), out = [];
  for (const p of ar.pois) {
    if (p.distance_m < view.min_distance_km * 1000 || p.distance_m > view.max_distance_km * 1000) continue;
    const pos = projectDir(p.bearing, basis, ar.camera);
    if (pos && pos.x >= 0 && pos.x <= 1 && pos.y >= 0 && pos.y <= 1) out.push({ ...p, ...pos });
    if (out.length >= ar.camera.max_labels) break;
  }
  return out;
}

function drawAr() {
  const canvas = $('arOverlay');
  const r = videoRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(r.bw * ratio); canvas.height = Math.round(r.bh * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, r.bw, r.bh);
  $('gizmoBox').hidden = !ar?.camera;
  drawGizmo();
  if (!ar?.camera || !view || !$('arToggle').checked || !video.naturalWidth) return;
  const c = ar.camera, basis = cameraBasis(view);
  const toPx = (pos) => ({ x: r.x + pos.x * r.w, y: r.y + pos.y * r.h });
  ctx.save();
  ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
  // 地平線(仰角 0 の線。回転していれば傾く)
  const left = projectDir(view.heading - c.hfov * 0.75, basis, c), right = projectDir(view.heading + c.hfov * 0.75, basis, c);
  if (left && right) {
    const a = toPx(left), b = toPx(right);
    ctx.strokeStyle = '#ffffff55'; ctx.setLineDash([6, 6]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); ctx.setLineDash([]);
  }
  const fontSize = Math.max(11, Math.round(r.w / 70));
  ctx.font = `600 ${fontSize}px system-ui, sans-serif`;
  ctx.textBaseline = 'middle';
  // 左から順に、横に重ならない段へ並べる(重なるときは一段上に積む)
  const rowH = fontSize + 10, rows = [];
  const labels = visiblePois().sort((a, b) => a.x - b.x).map((p) => {
    const text = `${p.name} ${fmtKm(p.distance_m)}`;
    const w = ctx.measureText(text).width + 12;
    const pt = toPx(p);
    let row = 0;
    while (rows[row] !== undefined && rows[row] > pt.x - w / 2 - 4) row++;
    rows[row] = pt.x + w / 2;
    return { text, w, pt, row };
  });
  for (const { text, w, pt, row } of labels) {
    const cy = pt.y - 24 - row * rowH;
    ctx.strokeStyle = '#ffcc80'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(pt.x, cy + rowH / 2 - 4); ctx.lineTo(pt.x, pt.y); ctx.stroke();
    ctx.fillStyle = '#ffcc80';
    ctx.beginPath(); ctx.arc(pt.x, pt.y, 3, 0, Math.PI * 2); ctx.fill();
    const lx = Math.min(Math.max(pt.x - w / 2, r.x), r.x + r.w - w);
    ctx.fillStyle = '#000000b0';
    ctx.fillRect(lx, cy - rowH / 2 + 2, w, rowH - 4);
    ctx.fillStyle = '#fff';
    ctx.fillText(text, lx + 6, cy);
  }
  ctx.restore();
  ctx.font = '10px system-ui, sans-serif'; ctx.fillStyle = '#ffffffaa'; ctx.textBaseline = 'bottom';
  ctx.fillText(ar.attribution.pois, r.x + 6, r.y + r.h - 4);
}

// 3 軸のアイコン(視点ギズモ): 外周に方位の目盛り、中に北(赤)・東(緑)・上(青)の軸をカメラから見た向きで描く
function drawGizmo() {
  const canvas = $('gizmo');
  if (!view) return;
  const size = 112, ratio = window.devicePixelRatio || 1;
  if (canvas.width !== size * ratio) { canvas.width = size * ratio; canvas.height = size * ratio; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, size, size);
  const c = size / 2, ringR = c - 9;

  // 方位の目盛りリング(いまの方位が上に来るように回す)
  ctx.lineCap = 'round';
  for (let a = 0; a < 360; a += 15) {
    const t = rad(a - view.heading) - Math.PI / 2;
    const major = a % 90 === 0, mid = a % 45 === 0;
    const r1 = ringR - (major ? 0 : mid ? 2 : 3), r2 = ringR + (major ? 0 : 1);
    if (major) continue;  // 東西南北は文字で示す
    ctx.strokeStyle = mid ? '#ffffff59' : '#ffffff2e'; ctx.lineWidth = mid ? 1.5 : 1;
    ctx.beginPath(); ctx.moveTo(c + Math.cos(t) * r1, c + Math.sin(t) * r1);
    ctx.lineTo(c + Math.cos(t) * r2, c + Math.sin(t) * r2); ctx.stroke();
  }
  ctx.font = '600 9px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (const [a, label] of [[0, 'N'], [90, 'E'], [180, 'S'], [270, 'W']]) {
    const t = rad(a - view.heading) - Math.PI / 2;
    ctx.fillStyle = label === 'N' ? '#ff6b6b' : '#ffffffa0';
    ctx.fillText(label, c + Math.cos(t) * ringR, c + Math.sin(t) * ringR);
  }
  // 上端の目印(カメラが向いている方位)
  ctx.fillStyle = '#64b5f6';
  ctx.beginPath(); ctx.moveTo(c - 4, 1); ctx.lineTo(c + 4, 1); ctx.lineTo(c, 6); ctx.closePath(); ctx.fill();

  // 3 軸(正の向きは文字入りの丸、負の向きは白抜きの小さな丸)。奥にある軸から描く
  const basis = cameraBasis(view), len = 30;
  const axes = [
    { v: [0, 1, 0], color: '#ff5f5f', label: 'N' }, { v: [1, 0, 0], color: '#5fd38d', label: 'E' },
    { v: [0, 0, 1], color: '#5fa8ff', label: 'U' },
  ].flatMap((a) => [a, { ...a, v: a.v.map((x) => -x), negative: true }])
    .map((a) => ({ ...a, sx: dot(a.v, basis.right), sy: dot(a.v, basis.up), depth: dot(a.v, basis.f) }))
    .sort((a, b) => b.depth - a.depth);
  for (const a of axes) {
    const x = c + a.sx * len, y = c - a.sy * len;
    const near = (1 - a.depth) / 2;             // 0: 奥 〜 1: 手前
    const alpha = 0.45 + 0.55 * near;
    ctx.globalAlpha = alpha;
    if (!a.negative) {
      ctx.strokeStyle = a.color; ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.moveTo(c, c); ctx.lineTo(x, y); ctx.stroke();
      const r = 7 + 2 * near;
      const g = ctx.createRadialGradient(x - r / 3, y - r / 3, 1, x, y, r);
      g.addColorStop(0, '#ffffff'); g.addColorStop(0.25, a.color); g.addColorStop(1, a.color);
      ctx.shadowColor = '#00000080'; ctx.shadowBlur = 4;
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = '#0d0f12'; ctx.font = '700 9px system-ui, sans-serif';
      ctx.fillText(a.label, x, y + 0.5);
    } else {
      ctx.strokeStyle = a.color; ctx.lineWidth = 1.5; ctx.fillStyle = '#14161a99';
      ctx.beginPath(); ctx.arc(x, y, 4 + near, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  // 中心
  ctx.fillStyle = '#ffffffcc'; ctx.beginPath(); ctx.arc(c, c, 2, 0, Math.PI * 2); ctx.fill();

  const text = `方位 ${Math.round(view.heading)}° ・ 傾き ${Math.round(view.pitch)}° ・ 回転 ${Math.round(view.roll)}°`;
  $('gizmoText').innerHTML = `方位 <b>${Math.round(view.heading)}°</b> ・ 傾き <b>${Math.round(view.pitch)}°</b> ・ 回転 <b>${Math.round(view.roll)}°</b>`;
  canvas.setAttribute('aria-valuenow', String(Math.round(view.heading)));
  canvas.setAttribute('aria-valuetext', text);
}

// 地点から方位・距離だけ進んだ地点(扇形の弧を描くため)
function destination(lat, lon, bearingDeg, distM) {
  const R = 6371000, d = distM / R, b = rad(bearingDeg);
  const p1 = rad(lat), l1 = rad(lon);
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [deg(p2), deg(l2)];
}

function drawMap() {
  if (!ar?.camera || !view || typeof L === 'undefined') {
    $('arInfo').textContent = ar && !ar.camera ? '設置場所の設定(ar.json)がありません' : '';
    return;
  }
  const c = ar.camera;
  if (!map) {
    map = L.map('map', { zoomControl: true, attributionControl: true });
    L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: `<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">${ar.attribution.tiles}</a> | ${ar.attribution.pois}`,
    }).addTo(map);
    map.layers = L.layerGroup().addTo(map);
  }
  map.layers.clearLayers();
  // 扇形(内側を切り欠いた形): 画角の左端〜右端、開始〜終了の距離
  const inner = view.min_distance_km * 1000, outer = view.max_distance_km * 1000;
  const from = view.heading - c.hfov / 2, n = 32, outerArc = [], innerArc = [];
  for (let i = 0; i <= n; i++) {
    const b = from + c.hfov * i / n;
    outerArc.push(destination(c.lat, c.lon, b, outer));
    innerArc.push(inner > 0 ? destination(c.lat, c.lon, b, inner) : [c.lat, c.lon]);
  }
  const sector = L.polygon([...outerArc, ...innerArc.reverse()], { color: '#ffa726', weight: 1.5, fillOpacity: 0.12 });
  map.layers.addLayer(sector);
  L.circleMarker([c.lat, c.lon], { radius: 6, color: '#fff', weight: 2, fillColor: '#1e88e5', fillOpacity: 1 })
    .bindTooltip('設置場所').addTo(map.layers);
  const shown = new Set(visiblePois().map((p) => p.name));
  for (const p of ar.pois) {
    const isShown = shown.has(p.name);
    L.circleMarker([p.lat, p.lon], {
      radius: isShown ? 6 : 3, weight: 1, color: '#000',
      fillColor: isShown ? '#ff7043' : '#9e9e9e', fillOpacity: isShown ? 0.95 : 0.5,
    }).bindTooltip(`${p.name}(${p.kind}、${fmtKm(p.distance_m)})`).addTo(map.layers);
  }
  if (!map.fitted) { map.fitBounds(sector.getBounds(), { padding: [10, 10] }); map.fitted = true; }
  $('arInfo').textContent = `表示中 ${shown.size} 件 / 周辺 ${ar.pois.length} 件`
    + (ar.error ? '(データの取得に失敗、保存済みのデータを表示)' : '');
}

// 距離の範囲のバー(2 つのつまみ)
function syncRange() {
  const lo = $('rangeMin'), hi = $('rangeMax'), max = Number(hi.max);
  $('rangeFill').style.left = `${view.min_distance_km / max * 100}%`;
  $('rangeFill').style.right = `${100 - view.max_distance_km / max * 100}%`;
  lo.value = view.min_distance_km; hi.value = view.max_distance_km;
  $('rangeText').textContent = `${view.min_distance_km}〜${view.max_distance_km} km`;
}
for (const id of ['rangeMin', 'rangeMax']) {
  $(id).addEventListener('input', () => {
    if (!view) return;
    let lo = Number($('rangeMin').value), hi = Number($('rangeMax').value);
    // つまみが交差しないようにする(最小 0.5km の幅)
    if (lo > hi - 0.5) { if (id === 'rangeMin') lo = hi - 0.5; else hi = lo + 0.5; }
    view.min_distance_km = Math.max(0, lo); view.max_distance_km = Math.min(Number($('rangeMax').max), hi);
    changed();
  });
}

let saveTimer = null;
function changed() {
  syncRange(); drawAr(); drawMap();
  // 動かし終わってから保存する
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const res = await fetch('/api/ar/view', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desk-Monitor': '1' },
      body: JSON.stringify(view),
    });
    saveTimer = null;
    if (!res.ok) $('arInfo').textContent = '向きを保存できませんでした';
  }, 800);
}

function rotate(dh, dp, dr) {
  if (!view) return;
  view.heading = ((view.heading + dh) % 360 + 360) % 360;
  view.pitch = Math.max(-45, Math.min(45, view.pitch + dp));
  view.roll = Math.max(-45, Math.min(45, view.roll + dr));
  changed();
}

// アイコンのドラッグ: 左右で方位、上下で傾き、Shift を押しながら左右で回転
let drag = null;
$('gizmo').addEventListener('pointerdown', (e) => {
  drag = { x: e.clientX, y: e.clientY, roll: e.shiftKey };
  $('gizmo').setPointerCapture(e.pointerId);
  $('gizmo').classList.add('dragging');
});
$('gizmo').addEventListener('pointermove', (e) => {
  if (!drag) return;
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  drag.x = e.clientX; drag.y = e.clientY;
  if (drag.roll) rotate(0, 0, dx * 0.3); else rotate(-dx * 0.5, dy * 0.3, 0);
});
$('gizmo').addEventListener('pointerup', () => { drag = null; $('gizmo').classList.remove('dragging'); });
// ダブルクリックで傾きと回転を 0 に戻す(方位はそのまま)
$('gizmo').addEventListener('dblclick', () => { if (view) { view.pitch = 0; view.roll = 0; changed(); } });
$('gizmo').addEventListener('wheel', (e) => { e.preventDefault(); rotate(0, 0, Math.sign(e.deltaY)); }, { passive: false });
$('gizmo').addEventListener('keydown', (e) => {
  const step = e.altKey ? 0.2 : 1;
  const keys = e.shiftKey
    ? { ArrowLeft: [0, 0, -step], ArrowRight: [0, 0, step] }
    : { ArrowLeft: [-step, 0, 0], ArrowRight: [step, 0, 0], ArrowUp: [0, step, 0], ArrowDown: [0, -step, 0] };
  if (!keys[e.key]) return;
  e.preventDefault(); rotate(...keys[e.key]);
});

async function refreshAr() {
  try {
    ar = await (await fetch('/api/ar', { cache: 'no-store' })).json();
    if (ar.camera) {
      $('rangeMin').max = $('rangeMax').max = ar.camera.fetch_radius_km;
      // 操作中でなければ、保存されている向きに合わせる
      if (!view || !saveTimer) view = Object.fromEntries(['heading', 'pitch', 'roll', 'min_distance_km', 'max_distance_km'].map((k) => [k, ar.camera[k]]));
      syncRange();
    }
    drawMap(); drawAr();
  } catch (e) { /* 次の周期で */ }
}
$('arToggle').addEventListener('change', drawAr);
video.addEventListener('load', drawAr);
new ResizeObserver(() => { drawAr(); map?.invalidateSize(); }).observe($('video'));

refreshAr();
setInterval(refreshAr, 60000);
// MJPEG はブラウザによって読み込みイベントが出ないので、映像の大きさが分かるまで描き直す
setInterval(drawAr, 2000);
refreshMetrics().then(drawCharts); refreshState(); refreshHistory();
setInterval(refreshMetrics, 2000);
setInterval(pollNotifications, 2000);
setInterval(drawCharts, 1000);
window.addEventListener('resize', drawCharts);
setInterval(refreshState, 1000);
setInterval(refreshHistory, 5000);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description='物体検出とダッシュボード(開発 PC で動かす)')
    parser.add_argument('--bind', default='127.0.0.1', help='ダッシュボードの待ち受けアドレス (既定: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8090, help='ダッシュボードのポート (既定: 8090)')
    parser.add_argument('--model', type=Path,
                        default=Path(os.environ.get('DESK_MONITOR_MODEL', DEFAULT_MODEL)),
                        help=f'ONNX モデル (既定: 環境変数 DESK_MONITOR_MODEL、なければ {DEFAULT_MODEL})')
    parser.add_argument('--conf', type=float, default=0.5, help='信頼度しきい値 (既定: 0.5)')
    parser.add_argument('--threads', type=int, default=4, help='推論のスレッド数 (既定: 4)')
    parser.add_argument('--confirm', type=int, default=2,
                        help='同じ物が何回続けて検出されたら表示するか。誤検出のちらつきを抑える (既定: 2)')
    parser.add_argument('--ar-config', type=Path,
                        default=Path(os.environ.get('DESK_MONITOR_AR_CONFIG', DEFAULT_AR_CONFIG)),
                        help=f'設置場所とカメラの向き(AR)。なければ AR を出さない (既定: {DEFAULT_AR_CONFIG})')
    parser.add_argument('--db', type=Path, default=Path(os.environ.get('DESK_MONITOR_DB', DEFAULT_DB)),
                        help=f'履歴の SQLite (既定: {DEFAULT_DB})')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    token = os.environ.get('DESK_MONITOR_TOKEN', '')
    try:
        camera_addr = wire.parse_addr(os.environ.get('DESK_MONITOR_CAMERA_ADDR', ''))
        display_addr = wire.parse_addr(os.environ.get('DESK_MONITOR_DISPLAY_ADDR', ''))
    except ValueError as exc:
        print(f'DESK_MONITOR_CAMERA_ADDR / DESK_MONITOR_DISPLAY_ADDR を設定してください: {exc}',
              file=sys.stderr)
        sys.exit(2)
    if not token:
        print('DESK_MONITOR_TOKEN を設定してください(scripts/setup-secrets.sh)', file=sys.stderr)
        sys.exit(2)

    state = HubState(History(args.db))
    display = DisplayLink(display_addr, token, state)

    stabilizer = DetectionStabilizer(args.confirm)

    def on_result(frame: np.ndarray, detections: list[Detection]) -> None:
        detections = stabilizer.update(detections)
        height, width = frame.shape[:2]
        state.set_detections(detections, time.time(), worker.inference_sec * 1000)
        display.send_detections(detections, width, height)

    worker = None
    if args.model.is_file():
        worker = DetectionWorker(load_detector(args.model, args.conf, args.threads),
                                 on_result=on_result)
        log.info('物体検出のモデル: %s', args.model.name)
    else:
        log.warning('モデルが見つからないため物体検出をしません: %s', args.model)

    for target, name in ((CameraLink(camera_addr, token, state, worker).run_forever, 'camera'),
                         (display.run_forever, 'display')):
        threading.Thread(target=target, daemon=True, name=name).start()

    ar = None
    try:
        config = CameraConfig.load(args.ar_config)
    except (ValueError, TypeError) as exc:
        log.warning('AR の設定が不正なため使いません: %s', exc)
        config = None
    if config is not None:
        ar = PoiStore(config, args.db.parent / 'pois.json', view_path=args.db.parent / 'ar_view.json')

        def keep_pois() -> None:
            # 取得に失敗していれば 10 分後、成功していれば 1 時間後に確かめ直す(新しければ取りに行かない)
            while True:
                ar.load()
                time.sleep(600 if ar.error or not ar.pois else 3600)

        threading.Thread(target=keep_pois, daemon=True, name='pois').start()
        log.info('AR: 方位 %.0f°、画角 %.1f°、%.0f〜%.0f km', ar.config.heading, ar.config.hfov,
                 ar.config.min_distance_km, ar.config.max_distance_km)

    def housekeeping() -> None:
        while True:
            time.sleep(2.0)
            state.expire(time.time())

    threading.Thread(target=housekeeping, daemon=True, name='housekeeping').start()
    server = DashboardServer((args.bind, args.port), state, display, ar)
    log.info('ダッシュボード: http://%s:%d/', args.bind, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.tracker.flush()
        server.server_close()


if __name__ == '__main__':
    main()
