from __future__ import annotations

import os
import signal
import sys

import numpy as np
import pytest

from desk_monitor import display
from desk_monitor.remote_camera import RemoteCamera


@pytest.fixture
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGUSR1, signal.SIGUSR2)}
    yield
    for s, handler in saved.items():
        signal.signal(s, handler)


def test_mode_controller_switches_on_signals(restore_signals):
    controller = display.ModeController('camera')
    os.kill(os.getpid(), signal.SIGUSR1)
    assert controller.mode == 'face'
    os.kill(os.getpid(), signal.SIGUSR2)
    assert controller.mode == 'camera'


def test_camera_source_selection(monkeypatch):
    monkeypatch.setattr(RemoteCamera, '_run', lambda self: None)
    monkeypatch.setenv('DESK_MONITOR_CAMERA_ADDR', '192.168.10.111:8082')
    assert isinstance(display._create_camera('token'), RemoteCamera)
    # トークンがない・アドレスが不正なら、この Pi のカメラを使う
    assert isinstance(display._create_camera(''), display.CameraSource)
    monkeypatch.setenv('DESK_MONITOR_CAMERA_ADDR', 'no-port')
    assert isinstance(display._create_camera('token'), display.CameraSource)
    monkeypatch.delenv('DESK_MONITOR_CAMERA_ADDR')
    assert isinstance(display._create_camera('token'), display.CameraSource)


class _Stop(Exception):
    pass


class FakeLcd:
    def __init__(self, frames: int) -> None:
        self.pushed: list[np.ndarray] = []
        self.frames = frames

    def push_rgb(self, arr) -> None:
        self.pushed.append(arr)
        if len(self.pushed) >= self.frames:
            raise _Stop


@pytest.fixture
def loop_parts(monkeypatch, restore_signals):
    camera_frame = np.full((240, 320, 3), 7, dtype=np.uint8)  # カメラ配信サーバの MJPEG と同じ大きさ
    face_frame = np.full((240, 240, 3), 9, dtype=np.uint8)

    class FakeCamera:
        available = True
        closed = False

        def frame(self):
            return camera_frame if FakeCamera.available else None

        def close(self):
            FakeCamera.closed = True

    class FakeAnimator:
        def frame(self, t, expression):
            return face_frame

    monkeypatch.setattr(display, '_create_camera', lambda token: FakeCamera())
    monkeypatch.setattr(display, 'FaceAnimator', FakeAnimator)
    monkeypatch.setattr(display, 'start_socket_listener', lambda handler, path: None)
    monkeypatch.delenv('DESK_MONITOR_TOKEN', raising=False)
    return camera_frame, face_frame, FakeCamera


def _run(lcd, mode, **kwargs):
    with pytest.raises(_Stop):
        display.run(lcd, fps=1000, expression='auto', initial_mode=mode,
                    socket_path='unused', **kwargs)


def test_run_shows_center_of_camera_frames(loop_parts):
    _, _, fake_camera = loop_parts
    lcd = FakeLcd(2)
    _run(lcd, 'camera')
    assert all(f.shape == (240, 240, 3) and (f == 7).all() for f in lcd.pushed)
    assert fake_camera.closed  # 終了時にカメラを閉じる


def test_run_draws_detections_from_hub(loop_parts, monkeypatch):
    overlays = []
    original = display.DetectionOverlay

    def capture():
        overlay = original()
        # 全視野の中央付近の物体(LCD に映る範囲)
        overlay.update([{'x1': 0.4, 'y1': 0.4, 'x2': 0.6, 'y2': 0.6, 'score': 0.9, 'cls': 0}])
        overlays.append(overlay)
        return overlay

    monkeypatch.setattr(display, 'DetectionOverlay', capture)
    lcd = FakeLcd(1)
    _run(lcd, 'camera')
    frame = lcd.pushed[0]
    # 0.4 * 320 = 128 → LCD の x = 128 - 40 = 88 に枠の左辺が描かれる
    assert (frame[120, 88] != 7).any()
    assert (frame[120, 120] == 7).all()  # 枠の内側は塗らない


def test_run_falls_back_to_face_without_camera(loop_parts):
    _, face_frame, fake_camera = loop_parts
    fake_camera.available = False
    lcd = FakeLcd(2)
    _run(lcd, 'camera')
    assert all(f is face_frame for f in lcd.pushed)


def test_run_shows_face_in_face_mode(loop_parts):
    _, face_frame, _ = loop_parts
    lcd = FakeLcd(1)
    _run(lcd, 'face')
    assert lcd.pushed[0] is face_frame


def test_run_opens_control_port_with_token(loop_parts, monkeypatch):
    started = []
    monkeypatch.setenv('DESK_MONITOR_TOKEN', 'token')
    monkeypatch.setattr(display.ControlServer, 'start', lambda self: started.append(self))
    lcd = FakeLcd(1)
    _run(lcd, 'face', control_port=0)
    assert not started  # ポート 0 なら開かない
    _run(FakeLcd(1), 'face', control_port=18083)
    assert len(started) == 1
    status = started[0].control.get_status()
    assert status['mode'] == 'face' and status['showing'] == 'face'
    started[0].control.set_mode('camera')
    started[0].server_close()


def test_main_rejects_out_of_range_brightness(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['display', '--brightness', '1.5'])
    with pytest.raises(SystemExit) as exc:
        display.main()
    assert exc.value.code == 1
