from __future__ import annotations

import io
import socket
import threading

import numpy as np
from PIL import Image

from desk_monitor import remote_camera, wire
from helpers import wait_until


def _jpeg(color=(200, 10, 10), size=(320, 240)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, format='JPEG')
    return buf.getvalue()


def test_frames_decoded_only_while_wanted():
    cam = remote_camera.RemoteCamera(('127.0.0.1', 1), 't', start=False)
    cam.handle({'type': 'frame'}, _jpeg(), now=100.0)
    assert cam._frame is None  # frame() が呼ばれていない(face 表示中)なら展開しない

    cam._wanted_at = 100.0
    cam.handle({'type': 'frame'}, _jpeg(), now=100.5)
    assert cam._frame.shape == (240, 320, 3)
    cam.handle({'type': 'frame'}, b'broken', now=100.6)  # 壊れた JPEG は無視
    cam.handle({'type': 'status', 'camera': {'running': True}}, b'')
    assert cam.camera_status['camera'] == {'running': True}


def test_frame_returns_none_when_stale(monkeypatch):
    cam = remote_camera.RemoteCamera(('127.0.0.1', 1), 't', start=False)
    now = {'t': 100.0}
    monkeypatch.setattr(remote_camera.time, 'time', lambda: now['t'])
    assert cam.frame() is None
    cam.handle({'type': 'frame'}, _jpeg(), now=100.0)
    frame = cam.frame()
    assert frame is not None and frame[0, 0, 0] > 150
    now['t'] += remote_camera.RemoteCamera.STALE_SEC + 0.1
    assert cam.frame() is None
    cam.close()


def test_connects_and_receives(monkeypatch):
    monkeypatch.setattr(remote_camera.RemoteCamera, 'RETRY_SEC', 0.05)
    srv = socket.create_server(('127.0.0.1', 0))
    hellos = []

    def serve():
        conn, _ = srv.accept()
        with conn:
            hellos.append(wire.Reader(conn).read()[0])
            for _ in range(50):
                wire.send(conn, {'type': 'frame', 'key': True}, _jpeg())
                if stop.wait(0.05):
                    break

    stop = threading.Event()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    cam = remote_camera.RemoteCamera(srv.getsockname(), 'secret')
    try:
        assert wait_until(lambda: cam.frame() is not None)
        assert cam.connected
        assert hellos[0]['stream'] == 'mjpeg' and hellos[0]['token'] == 'secret'
    finally:
        stop.set()
        srv.close()
    assert isinstance(cam.frame(), (np.ndarray, type(None)))
