from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from desk_monitor import camera


class FakePicamera2:
    """picamera2.Picamera2 の代わり。BGR 順で左上だけ値の違うフレームを返す。"""

    fail_on_init = False
    fail_on_capture = False
    instances: list[FakePicamera2] = []

    def __init__(self) -> None:
        if FakePicamera2.fail_on_init:
            raise IndexError('list index out of range')  # カメラ未接続時と同じ例外
        self.started = self.closed = False
        FakePicamera2.instances.append(self)

    def create_preview_configuration(self, main):
        return main

    def configure(self, config) -> None:
        self.config = config

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def capture_array(self) -> np.ndarray:
        if FakePicamera2.fail_on_capture:
            raise RuntimeError('capture failed')
        frame = np.zeros((camera.CAPTURE_HEIGHT, camera.CAPTURE_WIDTH, 3), dtype=np.uint8)
        frame[0, 0] = (1, 2, 3)  # B, G, R
        return frame


@pytest.fixture
def clock(monkeypatch):
    now = {'t': 1000.0}
    monkeypatch.setattr(camera.time, 'time', lambda: now['t'])
    return now


@pytest.fixture(autouse=True)
def fake_picamera2(monkeypatch):
    FakePicamera2.fail_on_init = FakePicamera2.fail_on_capture = False
    FakePicamera2.instances.clear()
    monkeypatch.setitem(sys.modules, 'picamera2', types.SimpleNamespace(Picamera2=FakePicamera2))


def test_frame_converts_bgr_and_flips_vertically(clock):
    source = camera.CameraSource()
    frame = source.frame()
    assert frame.shape == (480, 640, 3)  # 全視野(4:3)
    # 上下反転で左上の画素は左下に移り、BGR → RGB で並びが逆になる
    assert tuple(frame[-1, 0]) == (3, 2, 1)
    cam = FakePicamera2.instances[0]
    assert cam.started and cam.config == {'size': (640, 480), 'format': 'RGB888'}


def test_to_lcd_crops_center_square_and_downscales():
    full = np.zeros((480, 640, 3), dtype=np.uint8)
    full[:, :80] = 255   # 左右の帯は LCD に出ない
    full[:, 560:] = 255
    full[0:2, 80:82] = (40, 80, 120)  # 中央の正方形の左上 2x2 画素
    full[2:4, 80:82] = (0, 0, 0)
    lcd = camera.to_lcd(full)
    assert lcd.shape == (240, 240, 3) and lcd.dtype == np.uint8
    assert tuple(lcd[0, 0]) == (40, 80, 120)  # 2x2 画素の平均
    assert not lcd[1:, 1:-1].any()
    assert camera.lcd_region(480, 640) == (80, 0, 480)


def test_to_lcd_crops_remote_frame_without_scaling():
    # カメラ配信サーバの MJPEG(320x240)は中央の 240x240 を切り出すだけ
    full = np.zeros((240, 320, 3), dtype=np.uint8)
    full[:, 40:280] = 9
    lcd = camera.to_lcd(full)
    assert lcd.shape == (240, 240, 3) and (lcd == 9).all()
    assert camera.lcd_region(240, 320) == (40, 0, 240)


def test_to_lcd_rejects_non_integer_scale():
    with pytest.raises(ValueError):
        camera.to_lcd(np.zeros((300, 400, 3), dtype=np.uint8))


def test_camera_is_opened_once(clock):
    source = camera.CameraSource()
    source.frame()
    source.frame()
    assert len(FakePicamera2.instances) == 1


def test_init_failure_returns_none_and_retries_later(clock):
    FakePicamera2.fail_on_init = True
    source = camera.CameraSource()
    assert source.frame() is None

    FakePicamera2.fail_on_init = False
    clock['t'] += camera.CameraSource.RETRY_SEC - 1
    assert source.frame() is None  # 再試行までは初期化しない
    assert not FakePicamera2.instances

    clock['t'] += 1
    assert source.frame() is not None


def test_capture_failure_closes_camera_and_retries(clock):
    source = camera.CameraSource()
    source.frame()
    FakePicamera2.fail_on_capture = True
    assert source.frame() is None
    assert FakePicamera2.instances[0].closed

    FakePicamera2.fail_on_capture = False
    clock['t'] += camera.CameraSource.RETRY_SEC
    assert source.frame() is not None
    assert len(FakePicamera2.instances) == 2


def test_configure_failure_closes_camera(clock, monkeypatch):
    def boom(self, config):
        raise RuntimeError('configure failed')
    monkeypatch.setattr(FakePicamera2, 'configure', boom)
    source = camera.CameraSource()
    assert source.frame() is None
    assert FakePicamera2.instances[0].closed


def test_close_ignores_errors(clock, monkeypatch):
    source = camera.CameraSource()
    source.frame()
    monkeypatch.setattr(FakePicamera2, 'stop', lambda self: (_ for _ in ()).throw(RuntimeError()))
    source.close()
    source.close()  # 2 回目は何もしない
