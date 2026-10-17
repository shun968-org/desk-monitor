from __future__ import annotations

import sys
import types

import pytest

from desk_monitor import camera_server as cs
from desk_monitor import wire
from helpers import wait_until

TOKEN = 'token'


def _frames(client: cs.Client) -> list[dict]:
    out = []
    while (msg := client.next_message(timeout=0)) is not None:
        header = msg.split(b'\n', 1)[0]
        out.append(__import__('json').loads(header))
    return out


def test_h264_client_starts_at_keyframe():
    b = cs.Broadcaster('h264')
    client = b.add()
    b.frame(b'p1', keyframe=False, timestamp=1)
    b.frame(b'k2', keyframe=True, timestamp=2)
    b.frame(b'p3', keyframe=False, timestamp=3)
    assert [h['ts'] for h in _frames(client)] == [2, 3]
    assert b.frames == 3


def test_mjpeg_client_gets_every_frame():
    b = cs.Broadcaster('mjpeg')
    client = b.add()
    b.frame(b'j1', keyframe=False, timestamp=1)  # MJPEG はキーフレームを待たない
    assert [h['ts'] for h in _frames(client)] == [1]


def test_slow_h264_client_drops_until_next_keyframe(monkeypatch):
    monkeypatch.setattr(cs, 'MAX_QUEUE', 3)
    b = cs.Broadcaster('h264')
    client = b.add()
    b.frame(b'k', True, 0)
    for ts in range(1, 6):
        b.frame(b'p', False, ts)
    # 溢れたら捨てて、次のキーフレームまで送らない
    assert client.dropped == 3
    b.frame(b'k', True, 10)
    b.frame(b'p', False, 11)
    assert [h['ts'] for h in _frames(client)] == [10, 11]


def test_status_and_close():
    b = cs.Broadcaster('mjpeg')
    client = b.add()
    b.status({'type': 'status', 'x': 1})
    assert _frames(client) == [{'type': 'status', 'x': 1, 'size': 0}]
    assert b.client_count == 1
    b.remove(client)
    assert b.client_count == 0 and client.closed
    b.frame(b'j', True, 1)
    client.offer_status(b'x')
    assert client.next_message(timeout=0) is None


@pytest.fixture
def server():
    broadcasters = {name: cs.Broadcaster(name) for name in cs.STREAMS}
    srv = cs.CameraServer(('127.0.0.1', 0), TOKEN, None, broadcasters)
    import threading
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, broadcasters
    srv.shutdown()
    srv.server_close()


def test_client_receives_status_then_frames(server):
    srv, broadcasters = server
    sock, reader = wire.connect(srv.server_address, TOKEN, timeout=5, stream='mjpeg')
    with sock:
        header, _ = reader.read()
        assert header['type'] == 'status'
        assert header['camera']['running'] is False
        assert wait_until(lambda: broadcasters['mjpeg'].client_count == 1)
        broadcasters['mjpeg'].frame(b'jpeg-bytes', True, 123)
        header, payload = reader.read()
        assert header['type'] == 'frame' and header['ts'] == 123 and payload == b'jpeg-bytes'
    assert wait_until(lambda: broadcasters['mjpeg'].client_count == 0, timeout=10)


@pytest.mark.parametrize('token, stream', [('wrong', 'mjpeg'), (TOKEN, 'raw')])
def test_rejects_bad_hello(server, token, stream):
    srv, _ = server
    sock, reader = wire.connect(srv.server_address, token, timeout=5, stream=stream)
    with sock, pytest.raises((EOFError, OSError)):
        reader.read()


def test_status_counts_fps(server, monkeypatch):
    srv, broadcasters = server
    monkeypatch.setattr(cs.time, 'sleep', lambda s: (_ for _ in ()).throw(StopIteration))
    with pytest.raises(StopIteration):
        srv.publish_status_forever()
    assert srv.status()['camera']['clients'] == {'h264': 0, 'mjpeg': 0}


def test_output_forwards_encoder_frames(monkeypatch):
    class Output:
        pass
    monkeypatch.setitem(sys.modules, 'picamera2', types.ModuleType('picamera2'))
    monkeypatch.setitem(sys.modules, 'picamera2.outputs', types.SimpleNamespace(Output=Output))
    b = cs.Broadcaster('mjpeg')
    client = b.add()
    out = cs.make_output(b)
    out.outputframe(memoryview(b'abc'), keyframe=True, timestamp=5)
    out.outputframe(b'audio', audio=True)
    assert [h['ts'] for h in _frames(client)] == [5]


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv('DESK_MONITOR_TOKEN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['camera_server'])
    with pytest.raises(SystemExit) as exc:
        cs.main()
    assert exc.value.code == 2
