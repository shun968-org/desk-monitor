from __future__ import annotations

import http.client
import json
import threading
import time

import numpy as np
import pytest

from desk_monitor import hub
from desk_monitor.detector import Detection


def test_history_records_and_summarizes():
    h = hub.History(':memory:')
    now = time.time()
    h.add_detection(0, '人', now - 10, now - 5, 2, 0.91)
    h.add_detection(15, '猫', now - 3, now - 1, 1, 0.6)
    h.add_detection(0, '人', now - 1, now, 1, 0.7)
    h.add_notification(now, 'bluesky', '「地震」が多数投稿されています')
    recent = h.recent()
    assert [d['label'] for d in recent['detections']] == ['人', '猫', '人']
    assert recent['notifications'][0]['source'] == 'bluesky'
    assert recent['counts_24h'] == [{'label': '人', 'count': 2}, {'label': '猫', 'count': 1}]
    assert len(h.recent(limit=1)['detections']) == 1


def test_history_file(tmp_path):
    path = tmp_path / 'nested' / 'hub.sqlite3'
    hub.History(path).add_notification(1.0, 'test', 'x')
    assert hub.History(path).recent()['notifications'][0]['text'] == 'x'


def test_presence_tracker_merges_short_gaps():
    ended = []
    tracker = hub.PresenceTracker(lambda *args: ended.append(args), gap=3.0)
    person = Detection(0, 0, 1, 1, 0.8, 0)
    tracker.update([person, Detection(0, 0, 1, 1, 0.9, 0)], 100.0)
    tracker.update([], 101.0)
    tracker.update([person], 102.0)  # 3 秒以内に再び見えたら同じ記録
    assert tracker.active() == [{'label': '人', 'since': 100.0, 'count': 2}]
    tracker.update([], 105.5)
    assert ended == [(0, '人', 100.0, 102.0, 2, 0.9)]
    tracker.update([Detection(0, 0, 1, 1, 0.5, 54, 'ペン')], 106.0)
    tracker.flush()
    assert ended[-1][:2] == (54, 'ペン') and not tracker.active()


def test_state_snapshot_and_jpeg():
    state = hub.HubState(hub.History(':memory:'))
    snap = state.snapshot()
    assert snap['hub']['live'] is False and snap['detections'] == []
    assert state.annotated_jpeg(None) is None

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    state.set_frame(frame, time.time())
    state.set_detections([Detection(10, 10, 100, 100, 0.8, 0)], time.time(), 25.0)
    snap = state.snapshot()
    assert snap['hub']['live'] and snap['hub']['inference_ms'] == 25.0
    assert snap['detections'] == [{'label': '人', 'score': 0.8}]
    assert snap['present'][0]['label'] == '人'
    from desk_monitor.fonts import get_font
    assert state.annotated_jpeg(lambda width: get_font(16))[:2] == b'\xff\xd8'

    state.expire(time.time() + 10)  # 推論結果が途絶えたら記録を締める
    assert state.snapshot()['present'] == []
    assert state.history.recent()['detections'][0]['label'] == '人'


def test_display_link_sends_normalized_detections():
    state = hub.HubState(hub.History(':memory:'))
    link = hub.DisplayLink(('127.0.0.1', 1), 't', state)
    assert link.send({'type': 'ping'}) is False  # 未接続なら捨てる
    state.display = {'connected': True}
    link.send_detections([Detection(64, 48, 320, 240, 0.87654, 3)], 640, 480)
    msg = link._outbox.get_nowait()
    assert msg == {'type': 'detections', 'items': [
        {'x1': 0.1, 'y1': 0.1, 'x2': 0.5, 'y2': 0.5, 'score': 0.877, 'cls': 3, 'label': 'バイク'}]}

    link._handle({'type': 'status', 'display': {'mode': 'face'}, 'system': {'temp': 50}})
    assert state.display['display'] == {'mode': 'face'}
    link._handle({'type': 'notification', 'text': '台風', 'source': 'trends_rss', 'at': 10.0})
    assert state.history.recent()['notifications'][0]['text'] == '台風'


def test_camera_link_decodes_h264():
    av = pytest.importorskip('av')
    try:
        encoder = av.CodecContext.create('libx264', 'w')
    except Exception:
        pytest.skip('この環境の PyAV には H.264 エンコーダがない')
    encoder.width, encoder.height, encoder.pix_fmt = 64, 48, 'yuv420p'
    encoder.time_base = __import__('fractions').Fraction(1, 20)
    encoder.options = {'tune': 'zerolatency'}  # B フレームなし(カメラのエンコーダと同じ)
    packets = []
    for i in range(3):
        img = np.full((48, 64, 3), 40 * i, dtype=np.uint8)
        packets += encoder.encode(av.VideoFrame.from_ndarray(img, format='rgb24'))
    packets += encoder.encode(None)

    state = hub.HubState(hub.History(':memory:'))
    submitted = []
    worker = type('W', (), {'submit': lambda self, f: submitted.append(f)})()
    link = hub.CameraLink(('127.0.0.1', 1), 't', state, worker)
    decoder = hub._decoder()
    for p in packets:
        link._decode(decoder, bytes(p))
    assert state.frame is not None and state.frame.shape == (48, 64, 3)
    assert len(submitted) == 3
    link._decode(decoder, b'\x00\x00\x00\x01garbage')  # 壊れたデータでも落ちない


@pytest.fixture
def dashboard():
    state = hub.HubState(hub.History(':memory:'))
    link = hub.DisplayLink(('127.0.0.1', 1), 't', state)
    srv = hub.DashboardServer(('127.0.0.1', 0), state, link)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield state, link, srv.server_address
    srv.shutdown()
    srv.server_close()


def _request(addr, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers={'Content-Type': 'application/json', **(headers or {})})
    res = conn.getresponse()
    return res.status, res.read()


def test_dashboard_pages(dashboard):
    state, _, addr = dashboard
    status, body = _request(addr, 'GET', '/')
    assert status == 200 and b'/api/state' in body
    status, body = _request(addr, 'GET', '/api/state')
    assert json.loads(body)['camera'] == {'connected': False}
    state.history.add_notification(1.0, 'test', 'x')
    status, body = _request(addr, 'GET', '/api/history?limit=abc')
    assert json.loads(body)['notifications'][0]['text'] == 'x'
    assert _request(addr, 'GET', '/nothing')[0] == 404


def test_dashboard_rejects_foreign_host(dashboard):
    _, _, addr = dashboard
    # DNS リバインディング: 他のドメイン名でアクセスされたら応じない
    assert _request(addr, 'GET', '/api/state', headers={'Host': 'evil.example:8090'})[0] == 403


def test_dashboard_post_requires_custom_header(dashboard):
    state, link, addr = dashboard
    assert _request(addr, 'POST', '/api/mode', {'mode': 'face'})[0] == 403
    hdr = {'X-Desk-Monitor': '1'}
    # 表示プロセスにつながっていなければ 503
    assert _request(addr, 'POST', '/api/mode', {'mode': 'face'}, hdr)[0] == 503
    state.display = {'connected': True}
    assert _request(addr, 'POST', '/api/mode', {'mode': 'face'}, hdr)[0] == 200
    assert _request(addr, 'POST', '/api/mode', {'mode': 'x'}, hdr)[0] == 400
    # テスト通知の API はない
    assert _request(addr, 'POST', '/api/bubble', {'text': 'テスト'}, hdr)[0] == 400
    assert link._outbox.get_nowait() == {'type': 'mode', 'mode': 'face'}
    assert link._outbox.empty()


def test_dashboard_stream(dashboard):
    state, _, addr = dashboard
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request('GET', '/stream.mjpg')
    res = conn.getresponse()
    assert res.getheader('Content-Type').startswith('multipart/x-mixed-replace')
    state.set_frame(np.zeros((48, 64, 3), dtype=np.uint8), time.time())
    assert res.fp.readline() == b'--frame\r\n'
    res.close()
    conn.close()


def test_main_requires_settings(monkeypatch):
    monkeypatch.delenv('DESK_MONITOR_CAMERA_ADDR', raising=False)
    monkeypatch.setattr('sys.argv', ['hub'])
    with pytest.raises(SystemExit) as exc:
        hub.main()
    assert exc.value.code == 2


def test_metrics_record_device_samples():
    state = hub.HubState(hub.History(':memory:'))
    state.record_camera({'system': {'cpu': 8.0, 'temp': 64.5, 'memory': 20.0},
                         'camera': {'fps': {'h264': 20.5}}}, 100.0)
    state.record_display({'system': {'cpu': 15.0, 'temp': 67.0}, 'display': {'fps': 24.0}}, 101.0)
    state.record_camera({'system': 'broken', 'camera': None}, 102.0)  # 壊れた状態でも落ちない
    state.expire(103.0)
    m = state.metrics_snapshot()
    assert m['camera'][0] == {'t': 100.0, 'cpu': 8.0, 'temp': 64.5, 'memory': 20.0, 'fps': 20.5}
    assert m['camera'][1]['cpu'] is None
    assert m['display'][0]['fps'] == 24.0
    assert {k: m['hub'][0][k] for k in ('t', 'fps', 'ms')} == {'t': 103.0, 'fps': 0.0, 'ms': None}
    assert {'cpu', 'temp', 'memory'} <= set(m['hub'][0])  # 開発 PC 自身の状態も残す
    # 古いものから捨てる
    state.record('hub', {'fps': 1}, 103.0 + hub.METRICS_SEC + 1)
    assert [p['t'] for p in state.metrics_snapshot()['hub']] == [103.0 + hub.METRICS_SEC + 1]


def test_dashboard_metrics_api(dashboard):
    state, _, addr = dashboard
    state.record('display', {'cpu': 1.0}, time.time())
    status, body = _request(addr, 'GET', '/api/metrics')
    assert status == 200 and json.loads(body)['display'][0]['cpu'] == 1.0


def test_notifications_since(dashboard):
    state, _, addr = dashboard
    state.history.add_notification(10.0, 'bluesky', '古い')
    state.history.add_notification(20.0, 'test', '新しい')
    state.history.add_notification(30.0, 'trends_rss', 'もっと新しい')
    status, body = _request(addr, 'GET', '/api/notifications?since=10')
    assert [x['text'] for x in json.loads(body)['notifications']] == ['新しい', 'もっと新しい']
    status, body = _request(addr, 'GET', '/api/notifications?since=bad')
    assert len(json.loads(body)['notifications']) == 3


def test_stabilizer_requires_consecutive_detections():
    stab = hub.DetectionStabilizer(confirm=2)
    cat = Detection(10, 10, 50, 50, 0.6, 15)
    assert stab.update([cat]) == []                      # 1 回目はまだ出さない
    moved = Detection(12, 12, 52, 52, 0.7, 15)
    assert stab.update([moved]) == [moved]               # ほぼ同じ位置に続けて出たら出す
    assert stab.update([Detection(12, 12, 52, 52, 0.7, 16)]) == []  # 種類が変わったら出さない
    assert stab.update([Detection(200, 200, 240, 240, 0.7, 16)]) == []  # 位置が離れていたら出さない
    assert hub.DetectionStabilizer(confirm=1).update([cat]) == [cat]   # 1 ならそのまま


def test_dashboard_ar_api(dashboard, tmp_path):
    state, link, addr = dashboard
    status, body = _request(addr, 'GET', '/api/ar')
    assert json.loads(body) == {'camera': None, 'pois': []}  # 設定がなければ空
    hdr = {'X-Desk-Monitor': '1'}
    assert _request(addr, 'POST', '/api/ar/view', {'heading': 10}, hdr)[0] == 400  # AR なし


@pytest.fixture
def ar_dashboard(tmp_path):
    from desk_monitor import ar
    state = hub.HubState(hub.History(':memory:'))
    store = ar.PoiStore(ar.CameraConfig(lat=35.0, lon=139.0), tmp_path / 'pois.json',
                        fetch=lambda q: {}, view_path=tmp_path / 'view.json')
    srv = hub.DashboardServer(('127.0.0.1', 0), state, hub.DisplayLink(('127.0.0.1', 1), 't', state), store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield store, srv.server_address
    srv.shutdown()
    srv.server_close()


def test_dashboard_ar_view_update(ar_dashboard):
    store, addr = ar_dashboard
    hdr = {'X-Desk-Monitor': '1'}
    status, body = _request(addr, 'POST', '/api/ar/view', {'heading': 90, 'max_distance_km': 30}, hdr)
    assert status == 200 and json.loads(body)['camera']['heading'] == 90
    assert store.config.max_distance_km == 30
    assert _request(addr, 'POST', '/api/ar/view', {'pitch': 200}, hdr)[0] == 400
    assert _request(addr, 'POST', '/api/ar/view', {'heading': 0})[0] == 403  # 独自ヘッダがない
    assert _request(addr, 'POST', '/api/ar/view', [1, 2], hdr)[0] == 400
