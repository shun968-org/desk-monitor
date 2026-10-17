"""Waveshare 1.28inch LCD(GC9A01、240x240 の円形 SPI ディスプレイ)のドライバ。

SPI を有効化しておく:
    sudo raspi-config  ->  Interface Options  ->  SPI  ->  Yes
"""
from __future__ import annotations

import time

import numpy as np

# Pin (BCM)
PIN_DC = 25
PIN_RST = 27
PIN_BL = 18

WIDTH = 240
HEIGHT = 240
SPI_HZ = 40_000_000

# 初期化シーケンス (コマンド, 引数)
_INIT_SEQUENCE: list[tuple[int, list[int]]] = [
    (0xEF, []), (0xEB, [0x14]), (0xFE, []), (0xEF, []), (0xEB, [0x14]),
    (0x84, [0x40]), (0x85, [0xFF]), (0x86, [0xFF]), (0x87, [0xFF]),
    (0x88, [0x0A]), (0x89, [0x21]), (0x8A, [0x00]), (0x8B, [0x80]),
    (0x8C, [0x01]), (0x8D, [0x01]), (0x8E, [0xFF]), (0x8F, [0xFF]),
    (0xB6, [0x00, 0x20]),
    (0x36, [0x08]),                              # MADCTL = BGR
    (0x3A, [0x05]),                              # COLMOD = RGB565
    (0x90, [0x08, 0x08, 0x08, 0x08]),
    (0xBD, [0x06]), (0xBC, [0x00]),
    (0xFF, [0x60, 0x01, 0x04]),
    (0xC3, [0x13]), (0xC4, [0x13]), (0xC9, [0x22]), (0xBE, [0x11]),
    (0xE1, [0x10, 0x0E]),
    (0xDF, [0x21, 0x0C, 0x02]),
    (0xF0, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF1, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xF2, [0x45, 0x09, 0x08, 0x08, 0x26, 0x2A]),
    (0xF3, [0x43, 0x70, 0x72, 0x36, 0x37, 0x6F]),
    (0xED, [0x1B, 0x0B]), (0xAE, [0x77]), (0xCD, [0x63]),
    (0x70, [0x07, 0x07, 0x04, 0x0E, 0x0F, 0x09, 0x07, 0x08, 0x03]),
    (0xE8, [0x34]),
    (0x62, [0x18, 0x0D, 0x71, 0xED, 0x70, 0x70,
            0x18, 0x0F, 0x71, 0xEF, 0x70, 0x70]),
    (0x63, [0x18, 0x11, 0x71, 0xF1, 0x70, 0x70,
            0x18, 0x13, 0x71, 0xF3, 0x70, 0x70]),
    (0x64, [0x28, 0x29, 0xF1, 0x01, 0xF1, 0x00, 0x07]),
    (0x66, [0x3C, 0x00, 0xCD, 0x67, 0x45, 0x45, 0x10, 0x00, 0x00, 0x00]),
    (0x67, [0x00, 0x3C, 0x00, 0x00, 0x00, 0x01, 0x54, 0x10, 0x32, 0x98]),
    (0x74, [0x10, 0x85, 0x80, 0x00, 0x00, 0x4E, 0x00]),
    (0x98, [0x3E, 0x07]),
    (0x35, []),                                  # TEON
    (0x21, []),                                  # INVON
    (0x11, []),                                  # SLPOUT
]


def to_rgb565(arr: np.ndarray) -> bytes:
    """(H, W, 3) uint8 RGB を GC9A01 に送る RGB565(ビッグエンディアン)のバイト列にする。"""
    a = arr.astype(np.uint16)
    r = (a[..., 0] & 0xF8) << 8
    g = (a[..., 1] & 0xFC) << 3
    b = a[..., 2] >> 3
    return (r | g | b).astype('>u2').tobytes()


class GC9A01:
    def __init__(self) -> None:
        # Pi 専用のパッケージなので、ハードウェアを使うときだけ import する
        import spidev
        from gpiozero import DigitalOutputDevice, PWMOutputDevice

        self.dc = DigitalOutputDevice(PIN_DC, initial_value=False)
        self.rst = DigitalOutputDevice(PIN_RST, initial_value=True)
        self.bl = PWMOutputDevice(PIN_BL, frequency=1000, initial_value=1.0)
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = SPI_HZ
        self.spi.mode = 0

    def close(self) -> None:
        self.spi.close()
        self.bl.close()
        self.dc.close()
        self.rst.close()

    def _cmd(self, c: int) -> None:
        self.dc.off()
        self.spi.writebytes([c])

    def _data(self, *vals: int) -> None:
        self.dc.on()
        self.spi.writebytes2(bytes(vals))

    def _reset(self) -> None:
        self.rst.on(); time.sleep(0.01)
        self.rst.off(); time.sleep(0.01)
        self.rst.on(); time.sleep(0.12)

    def init(self) -> None:
        self._reset()
        for cmd, args in _INIT_SEQUENCE:
            self._cmd(cmd)
            if args:
                self._data(*args)
        time.sleep(0.12)
        self._cmd(0x29)                                  # DISPON
        time.sleep(0.02)

    def _set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._cmd(0x2A); self._data(x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._cmd(0x2B); self._data(y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._cmd(0x2C)

    def push_rgb(self, arr: np.ndarray) -> None:
        """arr: (HEIGHT, WIDTH, 3) uint8 RGB を全画面に表示する。"""
        data = to_rgb565(arr)
        self._set_window(0, 0, WIDTH - 1, HEIGHT - 1)
        self.dc.on()
        self.spi.writebytes2(data)
