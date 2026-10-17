from __future__ import annotations

import socket

import pytest

from desk_monitor import control, wire
from helpers import wait_until

TOKEN = 'token'


def test_overlay_converts_to_pixels_and_expires():
    overlay = control.DetectionOverlay()
    overlay.update([{'x1': 0.25, 'y1': 0.5, 'x2': 0.5, 'y2': 1.0, 'score': 0.8, 'cls': 15},
                    {'x1': 0, 'y1': 0, 'x2': 1, 'y2': 1, 'score': 0.7, 'cls': 54, 'label': 'ペン'},
                    {'x1': 'bad'}, 'not a dict'], now=100.0)
    det, pen = overlay.latest(640, 480, now=100.5)
    assert (det.x1, det.y1, det.x2, det.y2, det.class_id, det.label) == (160, 240, 320, 480, 15, '猫')
    assert pen.label == 'ペン'  # ハブが付けた種類名(Objects365 など)をそのまま使う
    assert overlay.latest(640, 480, now=100.0 + control.DETECTION_STALE_SEC + 0.1) == []
    overlay.update('not a list')
    assert overlay.latest(640, 480) == []


class Recorder:
    def __init__(self) -> None:
        self.modes = []
        self.overlay = control.DetectionOverlay()

    def control(self) -> control.DisplayControl:
        return control.DisplayControl(
            lambda: {'mode': 'face', 'showing': 'face'}, self.modes.append, self.overlay)


def test_handle_commands():
    rec = Recorder()
    ctl = rec.control()
    ctl.handle({'type': 'mode', 'mode': 'camera'})
    ctl.handle({'type': 'mode', 'mode': 'other'})
    ctl.handle({'type': 'bubble', 'text': 'テスト通知は受け付けない'})
    ctl.handle({'type': 'detections', 'items': [
        {'x1': 0, 'y1': 0, 'x2': 1, 'y2': 1, 'score': 0.5, 'cls': 0}]})
    ctl.handle({'type': 'unknown'})
    assert rec.modes == ['camera']
    assert len(rec.overlay.latest(10, 10)) == 1


@pytest.fixture
def server():
    rec = Recorder()
    ctl = rec.control()
    srv = control.ControlServer(('127.0.0.1', 0), TOKEN, ctl)
    srv.start()
    yield rec, ctl, srv.server_address
    srv.shutdown()
    srv.server_close()


def test_hub_session(server, monkeypatch):
    rec, ctl, addr = server
    sock, reader = wire.connect(addr, TOKEN, timeout=5)
    with sock:
        header, _ = reader.read()
        assert header['type'] == 'status'
        assert header['display'] == {'mode': 'face', 'showing': 'face'}
        assert set(header['system']) == {'host', 'temp', 'cpu', 'throttled', 'memory', 'uptime'}

        wire.send(sock, {'type': 'mode', 'mode': 'camera'})
        assert wait_until(lambda: rec.modes == ['camera'])

        ctl.notify('地震', 'bluesky')
        while (msg := reader.read()[0])['type'] != 'notification':
            pass
        assert (msg['text'], msg['source']) == ('地震', 'bluesky')


def test_rejects_wrong_token(server):
    _, ctl, addr = server
    sock, reader = wire.connect(addr, 'wrong', timeout=5)
    with sock, pytest.raises((EOFError, OSError)):
        reader.read()


def test_subscriber_removed_after_disconnect(server):
    _, ctl, addr = server
    sock, reader = wire.connect(addr, TOKEN, timeout=5)
    reader.read()
    assert wait_until(lambda: len(ctl._subscribers) == 1)
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    assert wait_until(lambda: not ctl._subscribers)
