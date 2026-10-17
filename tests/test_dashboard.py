from __future__ import annotations

import base64
import http.client
import json
import threading
import time

import numpy as np
import pytest

from desk_monitor import dashboard, feed


@pytest.mark.parametrize('host, expected', [
    ('192.168.10.113', True),
    ('10.0.0.5', True),
    ('172.16.1.1', True),
    ('127.0.0.1', True),
    ('8.8.8.8', False),
    ('2404:7a81::1', False),
    ('not-an-ip', False),
])
def test_is_lan_address(host, expected):
    assert dashboard.is_lan_address(host) is expected


def test_store_notifies_producer_of_viewers():
    store = dashboard.FrameStore()
    seen = []
    store.add_viewer()
    pid = store.attach_producer(seen.append)
    store.add_viewer()
    store.remove_viewer()
    assert seen == [1, 2, 1]
    store.detach_producer(pid)
    store.remove_viewer()  # 切断後は通知しない
    assert seen == [1, 2, 1]


def test_store_limits_viewers():
    store = dashboard.FrameStore()
    assert all(store.add_viewer() for _ in range(dashboard.MAX_VIEWERS))
    assert not store.add_viewer()


def test_store_status_and_frames():
    store = dashboard.FrameStore()
    assert store.status() == {'connected': False, 'mode': None, 'live': False}
    pid = store.attach_producer(lambda n: None)
    store.put_mode('face')
    assert store.status() == {'connected': True, 'mode': 'face', 'live': False}

    assert store.next_frame(0, timeout=0.01) == (0, None)
    store.put_frame(b'jpeg', now=100.0)
    assert store.next_frame(0, timeout=0.01) == (1, b'jpeg')
    assert store.status(now=101.0)['live'] is True
    assert store.status(now=100.0 + dashboard.LIVE_SEC)['live'] is False

    # 古い接続を外しても、新しい接続は残る
    pid2 = store.attach_producer(lambda n: None)
    store.detach_producer(pid)
    assert store.status()['connected'] is True
    store.detach_producer(pid2)
    assert store.status(now=101.0) == {'connected': False, 'mode': None, 'live': True}


# ─── pi_a(feed)→ pi_b(dashboard)→ ブラウザを 127.0.0.1 上でつなぐ ───

TOKEN = 'test-token'
USER, PASSWORD = 'desk', 'p@ss:word'


def _wait_until(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def servers(monkeypatch):
    monkeypatch.setattr(feed.DashboardFeed, 'RETRY_SEC', 0.1)
    monkeypatch.setattr(feed.DashboardFeed, 'STATUS_SEC', 0.1)
    store = dashboard.FrameStore()
    feed_srv = dashboard.FeedServer(('127.0.0.1', 0), store, TOKEN)
    http_srv = dashboard.DashboardHttpServer(('127.0.0.1', 0), store, USER, PASSWORD)
    for srv in (feed_srv, http_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield store, feed_srv.server_address, http_srv.server_address
    for srv in (feed_srv, http_srv):
        srv.shutdown()
        srv.server_close()


def _auth(user=USER, password=PASSWORD) -> dict:
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    return {'Authorization': f'Basic {token}'}


def _get(addr, path, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request('GET', path, headers=headers or {})
    return conn, conn.getresponse()


@pytest.mark.parametrize('headers', [None, _auth(password='wrong'), _auth(user='other'),
                                     {'Authorization': 'Basic !!!'}])
def test_http_requires_auth(servers, headers):
    _, _, http_addr = servers
    _, res = _get(http_addr, '/', headers)
    assert res.status == 401
    assert 'Basic' in res.getheader('WWW-Authenticate')


def test_http_serves_page_and_status(servers):
    _, _, http_addr = servers
    _, res = _get(http_addr, '/', _auth())
    assert res.status == 200 and b'/stream.mjpg' in res.read()
    _, res = _get(http_addr, '/status', _auth())
    assert json.loads(res.read()) == {'connected': False, 'mode': None, 'live': False}
    _, res = _get(http_addr, '/nothing', _auth())
    assert res.status == 404


def test_http_rejects_non_lan_clients(servers, monkeypatch):
    _, _, http_addr = servers
    monkeypatch.setattr(dashboard, 'is_lan_address', lambda host: False)
    _, res = _get(http_addr, '/', _auth())
    assert res.status == 403


def test_feed_with_wrong_token_is_rejected(servers):
    store, feed_addr, _ = servers
    sender = feed.DashboardFeed(feed_addr, 'wrong', fps=100)
    time.sleep(0.3)
    assert store.status()['connected'] is False
    assert sender.viewers == 0


def test_frames_flow_from_display_to_browser(servers):
    store, feed_addr, http_addr = servers
    sender = feed.DashboardFeed(feed_addr, TOKEN, fps=100)
    assert _wait_until(lambda: store.status()['connected'])

    sender.set_mode('face')
    assert _wait_until(lambda: store.status()['mode'] == 'face')
    assert not sender.wanted(time.time())  # 閲覧者がいなければ送らない

    conn, res = _get(http_addr, '/stream.mjpg', _auth())
    assert res.status == 200
    assert res.getheader('Content-Type').startswith('multipart/x-mixed-replace')
    assert _wait_until(lambda: sender.viewers == 1)

    sender.set_mode('camera')
    t = time.time()
    assert sender.wanted(t)
    sender.publish(np.zeros((240, 240, 3), dtype=np.uint8), t)
    assert not sender.wanted(t)  # 上限 fps の間隔が空くまでは送らない

    assert res.fp.readline() == b'--frame\r\n'
    headers = {}
    while (line := res.fp.readline()) != b'\r\n':
        key, _, value = line.decode().partition(':')
        headers[key.strip().lower()] = value.strip()
    assert headers['content-type'] == 'image/jpeg'
    jpeg = res.fp.read(int(headers['content-length']))
    assert jpeg[:2] == b'\xff\xd8'
    assert store.status()['live'] is True

    res.close()  # ブラウザを閉じる(レスポンスも閉じないとソケットが残る)
    conn.close()
    assert _wait_until(lambda: sender.viewers == 0, timeout=3.0)


def test_feed_reconnects_after_dashboard_restart(servers, monkeypatch):
    store, feed_addr, _ = servers
    sender = feed.DashboardFeed(feed_addr, TOKEN, fps=100)
    assert _wait_until(lambda: store.status()['connected'])
    assert sender._connected


def test_viewer_limit_returns_503(servers, monkeypatch):
    store, _, http_addr = servers
    monkeypatch.setattr(dashboard, 'MAX_VIEWERS', 0)
    _, res = _get(http_addr, '/stream.mjpg', _auth())
    assert res.status == 503


def test_main_requires_secrets(monkeypatch):
    monkeypatch.delenv('DESK_MONITOR_DASHBOARD_PASSWORD', raising=False)
    monkeypatch.setenv('DESK_MONITOR_FEED_TOKEN', 'x')
    monkeypatch.setattr('sys.argv', ['dashboard'])
    with pytest.raises(SystemExit) as exc:
        dashboard.main()
    assert exc.value.code == 2
