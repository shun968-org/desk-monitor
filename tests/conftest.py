"""テスト共通のフィクスチャ。

Pi 専用のパッケージ(spidev / gpiozero / picamera2)は偽モジュールで置き換え、
開発機や CI でもハードウェアなしにテストできるようにする。
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest


class FakePin:
    """gpiozero の DigitalOutputDevice / PWMOutputDevice の代わり。"""

    def __init__(self, pin, **kwargs) -> None:
        self.pin = pin
        self.value = kwargs.get('initial_value', 0)
        self.closed = False

    def on(self) -> None:
        self.value = 1

    def off(self) -> None:
        self.value = 0

    def close(self) -> None:
        self.closed = True


class FakeSpi:
    """spidev.SpiDev の代わり。書き込みを (DC ピンの値, バイト列) で記録する。"""

    instances: list[FakeSpi] = []

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.dc: FakePin | None = None
        self.closed = False
        FakeSpi.instances.append(self)

    def open(self, bus: int, device: int) -> None:
        self.bus_device = (bus, device)

    def writebytes(self, data) -> None:
        self.writes.append(bytes(data))

    def writebytes2(self, data) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_hardware(monkeypatch):
    """spidev と gpiozero を偽モジュールに差し替える。"""
    FakeSpi.instances.clear()
    monkeypatch.setitem(sys.modules, 'spidev', types.SimpleNamespace(SpiDev=FakeSpi))
    monkeypatch.setitem(sys.modules, 'gpiozero', types.SimpleNamespace(
        DigitalOutputDevice=FakePin, PWMOutputDevice=FakePin))
    return FakeSpi.instances


@pytest.fixture
def blank_frame() -> np.ndarray:
    return np.zeros((240, 240, 3), dtype=np.uint8)
