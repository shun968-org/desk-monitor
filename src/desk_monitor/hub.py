"""ハブ: 映像の物体検出と、ダッシュボードへの情報の集約(開発 PC で動かす。role hub)。

  カメラ配信サーバ(camera_server.py)── H.264 640x480 ──▶ ハブ ── 検出結果 ──▶ 表示プロセス(control.py)
                                                          │
                                                          └──▶ ダッシュボード(http://127.0.0.1:8090/)

- カメラ配信サーバから全視野の H.264 を受けて復号し(PyAV)、YOLO で毎フレーム推論する
- 検出結果(全視野に対する 0〜1 の座標)を表示プロセスに送り、円形 LCD に枠を重ねさせる
- 表示プロセスに操作(モード切替・テスト通知)を送り、状態と吹き出しの通知を受け取る
- ダッシュボードに、検出枠つきの映像・各機器の状態・検出と通知の履歴・操作をまとめる
- 履歴は SQLite(既定 ~/.local/share/desk-monitor/hub.sqlite3)に残す

ダッシュボードは 127.0.0.1 だけで待ち受ける(開発 PC 上のブラウザからのみ見る)。
操作の API は、他のサイトから送らせないよう Host ヘッダと独自ヘッダを確かめる。

環境変数(~/.config/desk-monitor/desk-monitor.env。scripts/setup-secrets.sh で作る):
  DESK_MONITOR_TOKEN, DESK_MONITOR_CAMERA_ADDR(例 192.168.10.111:8082),
  DESK_MONITOR_DISPLAY_ADDR(例 192.168.10.113:8083)

使い方(リポジトリ直下で。通常は deploy/hub の Docker で動かす):
    PYTHONPATH=src python3 -m desk_monitor.hub [--port 8090] [--model models/yolo11m.onnx]
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

from desk_monitor import wire
from desk_monitor.detector import (COCO_LABELS_JA, Detection, DetectionWorker, YoloDetector,
                                   draw_detections)
from desk_monitor.fonts import get_font
from desk_monitor.paths import MODELS_DIR

log = logging.getLogger(__name__)

DEFAULT_MODEL = MODELS_DIR / 'yolo11m.onnx'
# 機器の状態の推移を残す長さ(状態は約 2 秒ごとに届く)
METRICS_SEC = 600
DEFAULT_DB = Path.home() / '.local' / 'share' / 'desk-monitor' / 'hub.sqlite3'
RETRY_SEC = 5.0
PING_SEC = 2.0
# この秒数見えなくなったら、その種類の検出を「終わった」とみなす(一瞬の見落としで記録を分けない)
PRESENCE_GAP_SEC = 3.0
STREAM_FPS = 10.0


def _label(class_id: int) -> str:
    return COCO_LABELS_JA[class_id] if 0 <= class_id < len(COCO_LABELS_JA) else f'#{class_id}'


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

    def add_detection(self, class_id: int, started_at: float, ended_at: float,
                      max_count: int, max_score: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                'INSERT INTO detections (class_id, label, started_at, ended_at, max_count, max_score)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (class_id, _label(class_id), started_at, ended_at, max_count, max_score))

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


class PresenceTracker:
    """フレームごとの検出結果から「何がいつからいつまで映っていたか」をまとめる。"""

    def __init__(self, on_end, gap: float = PRESENCE_GAP_SEC) -> None:
        self._on_end = on_end
        self._gap = gap
        self._active: dict[int, dict] = {}

    def update(self, detections: list[Detection], now: float) -> None:
        counts: dict[int, tuple[int, float]] = {}
        for d in detections:
            n, best = counts.get(d.class_id, (0, 0.0))
            counts[d.class_id] = (n + 1, max(best, d.score))
        for class_id, (n, best) in counts.items():
            item = self._active.setdefault(
                class_id, {'start': now, 'last': now, 'count': 0, 'score': 0.0})
            item['last'] = now
            item['count'] = max(item['count'], n)
            item['score'] = max(item['score'], best)
        for class_id in [c for c, item in self._active.items() if now - item['last'] > self._gap]:
            self._finish(class_id)

    def flush(self) -> None:
        for class_id in list(self._active):
            self._finish(class_id)

    def active(self) -> list[dict]:
        return [{'label': _label(c), 'since': item['start'], 'count': item['count']}
                for c, item in sorted(self._active.items(), key=lambda kv: kv[1]['start'])]

    def _finish(self, class_id: int) -> None:
        item = self._active.pop(class_id)
        self._on_end(class_id, item['start'], item['last'], item['count'], item['score'])


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
        self.record('hub', {'fps': fps, 'ms': round(ms, 1) if ms is not None else None}, now)

    def annotated_jpeg(self, font) -> bytes | None:
        with self._lock:
            frame = self.frame
            detections = self.detections if time.time() - self.detections_at < 1.0 else []
        if frame is None:
            return None
        if detections:
            frame = draw_detections(frame, detections, font, circular=False)
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
                  'y2': d.y2 / height, 'score': round(d.score, 3), 'cls': d.class_id}
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
        elif kind == 'bubble':
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
        url = urlsplit(self.path)
        link = self.server.display
        if url.path == '/api/mode' and body.get('mode') in ('face', 'camera'):
            ok = link.send({'type': 'mode', 'mode': body['mode']})
        elif url.path == '/api/bubble' and isinstance(body.get('text'), str) and body['text'].strip():
            ok = link.send({'type': 'bubble', 'text': body['text'].strip()[:200],
                            'source': 'test', 'duration': 10})
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

    def __init__(self, addr: tuple[str, int], state: HubState, display: DisplayLink) -> None:
        self.state = state
        self.display = display
        self.font = get_font(16)
        super().__init__(addr, _DashboardHandler)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>desk-monitor</title>
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
  /* 右の列の残りの高さを 4 つのグラフで分ける */
  .charts { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .charts canvas { flex: 1; min-height: 80px; width: 100%; background: #161616;
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
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(440px, 92vw); z-index: 10;
            background: var(--panel); border-left: 1px solid var(--line); box-shadow: -8px 0 24px #0008;
            padding: 12px; display: flex; flex-direction: column; gap: 6px;
            transform: translateX(100%); transition: transform .2s ease; visibility: hidden; }
  .drawer.open { transform: none; visibility: visible; }
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
    .charts canvas { flex: none; height: 120px; }
  }
</style>
</head>
<body>
<header>
  <h1>desk-monitor</h1>
  <span class="kv" id="clock"></span>
  <button id="drawerOpen" style="margin-left:auto" aria-controls="drawer" aria-expanded="false">検出履歴</button>
</header>
<main>
  <section class="camera">
    <h2>カメラ(全視野・検出枠つき)</h2>
    <div class="video">
      <img id="video" alt="カメラ映像">
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
      <form id="bubbleForm">
        <input id="bubbleText" maxlength="200" placeholder="テスト通知の文言" autocomplete="off">
        <button>送信</button>
      </form>
      <div class="kv" id="opMsg"></div>
    </section>
    <section class="charts">
      <canvas id="chartTemp" aria-label="温度の推移"></canvas>
      <canvas id="chartCpu" aria-label="CPU 使用率の推移"></canvas>
      <canvas id="chartMem" aria-label="メモリ使用率の推移"></canvas>
      <canvas id="chartFps" aria-label="fps の推移"></canvas>
    </section>
  </div>
</main>
<!-- 通知のトースト。読み上げはするがフォーカスは移さない(フォーカスを受け取る要素を置かない) -->
<div class="toasts" id="toasts" role="status" aria-live="polite"></div>
<!-- サイドパネル(通常は閉じておき、ヘッダーのボタンで開く) -->
<aside class="drawer" id="drawer" aria-hidden="true" aria-labelledby="drawerTitle">
  <div class="drawer-head">
    <h2 id="drawerTitle">検出履歴</h2>
    <button id="drawerClose" aria-label="閉じる">×</button>
  </div>
  <div class="kv" id="counts"></div>
  <div class="scroll">
    <table id="detections"><thead><tr><th>種類</th><th>時刻</th><th>長さ</th><th>数</th><th>信頼度</th></tr></thead><tbody></tbody></table>
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
const SERIES = {
  // 右の列は狭いので短い名前にする(pi_a: 円形 LCD、pi_b: カメラ、ハブ: 推論)
  camera: { label: 'pi_b', color: '#42a5f5' },
  display: { label: 'pi_a', color: '#ffa726' },
  hub: { label: 'ハブ', color: '#66bb6a' },
};
const CHARTS = [
  { id: 'chartTemp', title: '温度 °C', key: 'temp', devices: ['display', 'camera'], min: 40, max: 90, guide: 80 },
  { id: 'chartCpu', title: 'CPU %', key: 'cpu', devices: ['display', 'camera'], min: 0, max: 100 },
  { id: 'chartMem', title: 'メモリ %', key: 'memory', devices: ['display', 'camera'], min: 0, max: 100 },
  { id: 'chartFps', title: 'fps', key: 'fps', devices: ['display', 'camera', 'hub'], min: 0, max: 35 },
];

function drawChart(chart, now) {
  const canvas = $(chart.id);
  if (!canvas) return;
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
  // 値が範囲を超えたら上限を広げる
  let max = chart.max;
  for (const d of chart.devices) for (const p of metrics[d] ?? []) if (p[chart.key] != null) max = Math.max(max, p[chart.key]);
  const x = (t) => left + (1 - (now - t) / WINDOW_SEC) * pw;
  const y = (v) => top + ph - (Math.min(max, Math.max(chart.min, v)) - chart.min) / (max - chart.min) * ph;

  ctx.font = '10px system-ui, sans-serif';
  ctx.fillStyle = '#8a8a8a'; ctx.strokeStyle = '#2a2a2a'; ctx.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const v = chart.min + (max - chart.min) * i / 2, yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(left + pw, yy); ctx.stroke();
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle'; ctx.fillText(String(Math.round(v)), left - 4, yy);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const m of [10, 5, 0]) ctx.fillText(m ? `-${m}分` : '今', x(now - m * 60), top + ph + 2);
  if (chart.guide != null) {
    ctx.setLineDash([3, 3]); ctx.strokeStyle = '#ef535099';
    ctx.beginPath(); ctx.moveTo(left, y(chart.guide)); ctx.lineTo(left + pw, y(chart.guide)); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  ctx.fillStyle = '#ddd'; ctx.font = '600 11px system-ui, sans-serif';
  ctx.fillText(chart.title, 4, 3);
  let legendX = 4 + ctx.measureText(chart.title).width + 10;
  ctx.font = '10px system-ui, sans-serif';
  for (const d of chart.devices) {
    const series = (metrics[d] ?? []).filter((p) => p[chart.key] != null && now - p.t <= WINDOW_SEC + 5);
    const last = series.at(-1);
    const label = `${SERIES[d].label} ${last ? Number(last[chart.key]).toFixed(chart.key === 'temp' ? 1 : 0) : '–'}`;
    ctx.fillStyle = SERIES[d].color;
    ctx.fillRect(legendX, 7, 8, 2);
    ctx.fillText(label, legendX + 11, 3);
    legendX += 11 + ctx.measureText(label).width + 10;

    ctx.save();
    ctx.beginPath(); ctx.rect(left, top, pw, ph); ctx.clip();
    ctx.strokeStyle = SERIES[d].color; ctx.lineWidth = 1.5; ctx.beginPath();
    let prev = null;
    for (const p of series) {
      // 6 秒以上途切れたら(切断など)線をつながない
      if (prev && p.t - prev.t > 6) ctx.moveTo(x(p.t), y(p[chart.key]));
      else if (prev) ctx.lineTo(x(p.t), y(p[chart.key]));
      else ctx.moveTo(x(p.t), y(p[chart.key]));
      prev = p;
    }
    ctx.stroke();
    ctx.restore();
  }
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
$('bubbleForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('bubbleText').value.trim();
  if (text && await post('/api/bubble', { text })) $('bubbleText').value = '';
});

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

function setDrawer(open) {
  $('drawer').classList.toggle('open', open);
  $('drawer').setAttribute('aria-hidden', String(!open));
  $('drawerOpen').setAttribute('aria-expanded', String(open));
  if (open) refreshHistory();
}
$('drawerOpen').addEventListener('click', () => setDrawer(!$('drawer').classList.contains('open')));
$('drawerClose').addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setDrawer(false); });

async function refreshMetrics() {
  try { metrics = await (await fetch('/api/metrics', { cache: 'no-store' })).json(); } catch (e) { /* 次の周期で */ }
}

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
    parser.add_argument('--conf', type=float, default=0.4, help='信頼度しきい値 (既定: 0.4)')
    parser.add_argument('--threads', type=int, default=4, help='推論のスレッド数 (既定: 4)')
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

    def on_result(frame: np.ndarray, detections: list[Detection]) -> None:
        height, width = frame.shape[:2]
        state.set_detections(detections, time.time(), worker.inference_sec * 1000)
        display.send_detections(detections, width, height)

    worker = None
    if args.model.is_file():
        worker = DetectionWorker(YoloDetector(args.model, conf_threshold=args.conf,
                                              num_threads=args.threads), on_result=on_result)
    else:
        log.warning('モデルが見つからないため物体検出をしません: %s', args.model)

    for target, name in ((CameraLink(camera_addr, token, state, worker).run_forever, 'camera'),
                         (display.run_forever, 'display')):
        threading.Thread(target=target, daemon=True, name=name).start()
    def housekeeping() -> None:
        while True:
            time.sleep(2.0)
            state.expire(time.time())

    threading.Thread(target=housekeeping, daemon=True, name='housekeeping').start()
    server = DashboardServer((args.bind, args.port), state, display)
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
