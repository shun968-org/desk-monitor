from __future__ import annotations

import os
import signal
import sys

import numpy as np
import pytest

from desk_monitor import display


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


def test_detection_disabled_without_model(tmp_path):
    assert display._create_detection_worker(tmp_path / 'none.onnx', 0.4, 1.0) is None


def test_detection_disabled_when_onnxruntime_unusable(tmp_path, monkeypatch):
    model = tmp_path / 'm.onnx'
    model.touch()
    monkeypatch.setattr(display, '_onnxruntime_importable', lambda: False)
    assert display._create_detection_worker(model, 0.4, 1.0) is None


def test_detection_disabled_when_model_fails_to_load(tmp_path, monkeypatch):
    model = tmp_path / 'm.onnx'
    model.write_bytes(b'not a model')
    monkeypatch.setattr(display, '_onnxruntime_importable', lambda: True)
    # onnxruntime が無い環境(CI)では import で、ある環境ではモデルの読み込みで失敗する
    assert display._create_detection_worker(model, 0.4, 1.0) is None


def test_onnxruntime_check_uses_subprocess(monkeypatch):
    calls = []

    class Result:
        returncode = -4  # SIGILL

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(display.subprocess, 'run', fake_run)
    assert display._onnxruntime_importable() is False
    assert calls == [[sys.executable, '-c', 'import onnxruntime']]


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
    camera_frame = np.full((240, 240, 3), 7, dtype=np.uint8)
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

    monkeypatch.setattr(display, 'CameraSource', FakeCamera)
    monkeypatch.setattr(display, 'FaceAnimator', FakeAnimator)
    monkeypatch.setattr(display, 'start_socket_listener', lambda bubble, path: None)
    return camera_frame, face_frame, FakeCamera


def _run(lcd, mode):
    with pytest.raises(_Stop):
        display.run(lcd, fps=1000, expression='auto', initial_mode=mode,
                    socket_path='unused', detect=False, model_path=None,
                    conf=0.4, detect_interval=1.0)


def test_run_shows_camera_frames(loop_parts):
    camera_frame, _, fake_camera = loop_parts
    lcd = FakeLcd(2)
    _run(lcd, 'camera')
    assert all(f is camera_frame for f in lcd.pushed)
    assert fake_camera.closed  # 終了時にカメラを閉じる


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


def test_main_rejects_out_of_range_brightness(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['display', '--brightness', '1.5'])
    with pytest.raises(SystemExit) as exc:
        display.main()
    assert exc.value.code == 1
