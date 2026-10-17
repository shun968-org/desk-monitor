from __future__ import annotations

import numpy as np
import pytest

from desk_monitor import lcd


@pytest.mark.parametrize('rgb, expected', [
    ((255, 255, 255), 0xFFFF),
    ((0, 0, 0), 0x0000),
    ((255, 0, 0), 0xF800),
    ((0, 255, 0), 0x07E0),
    ((0, 0, 255), 0x001F),
    ((8, 4, 8), 0x0821),  # 各チャネルの下位ビットは切り捨てる
])
def test_to_rgb565_packs_big_endian(rgb, expected):
    arr = np.array([[rgb]], dtype=np.uint8)
    assert lcd.to_rgb565(arr) == expected.to_bytes(2, 'big')


def test_init_sends_sequence_and_display_on(fake_hardware, monkeypatch):
    monkeypatch.setattr(lcd.time, 'sleep', lambda s: None)
    dev = lcd.GC9A01()
    dev.init()
    spi = fake_hardware[0]
    commands = [w[0] for w in spi.writes if len(w) == 1]
    expected = [cmd for cmd, _ in lcd._INIT_SEQUENCE]
    # 引数 1 バイトのデータもあるので、コマンドの並びが含まれていることを確かめる
    it = iter(commands)
    assert all(c in it for c in expected)
    assert spi.writes[-1] == bytes([0x29])  # DISPON
    assert spi.bus_device == (0, 0)
    assert spi.max_speed_hz == lcd.SPI_HZ


def test_push_rgb_sets_full_window_and_sends_frame(fake_hardware, blank_frame):
    dev = lcd.GC9A01()
    dev.push_rgb(blank_frame)
    spi = fake_hardware[0]
    assert spi.writes[0] == bytes([0x2A])
    assert spi.writes[1] == bytes([0, 0, 0, lcd.WIDTH - 1])
    assert spi.writes[2] == bytes([0x2B])
    assert spi.writes[3] == bytes([0, 0, 0, lcd.HEIGHT - 1])
    assert spi.writes[4] == bytes([0x2C])
    assert len(spi.writes[5]) == lcd.WIDTH * lcd.HEIGHT * 2
    assert dev.dc.value == 1  # 画素データは DC=1(データ)で送る


def test_close_releases_all_devices(fake_hardware):
    dev = lcd.GC9A01()
    dev.close()
    assert fake_hardware[0].closed
    assert dev.bl.closed and dev.dc.closed and dev.rst.closed
