"""Waveshare 1.28inch LCD (GC9A01) で任意画像を表示する。

desk-monitor-display サービスと LCD を取り合うため、止めてから使う。

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.tools.show_image <image_file> [--brightness 0.0-1.0]

依存パッケージ (Raspberry Pi OS Bookworm 以降):
    sudo apt install python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from desk_monitor.lcd import GC9A01, HEIGHT, WIDTH


def main() -> None:
    parser = argparse.ArgumentParser(description='GC9A01 で画像を表示する')
    parser.add_argument('image', type=Path, help='表示する画像ファイル')
    parser.add_argument('--brightness', type=float, default=1.0,
                        help='バックライト輝度 0.0-1.0 (既定: 1.0)')
    args = parser.parse_args()

    if not args.image.is_file():
        print(f'画像が見つからない: {args.image}', file=sys.stderr)
        sys.exit(1)
    if not 0.0 <= args.brightness <= 1.0:
        print('brightness は 0.0-1.0 の範囲で指定する', file=sys.stderr)
        sys.exit(1)

    img = Image.open(args.image).convert('RGB').resize((WIDTH, HEIGHT))
    lcd = GC9A01()
    try:
        lcd.init()
        lcd.bl.value = args.brightness
        lcd.push_rgb(np.asarray(img, dtype=np.uint8))
    finally:
        lcd.close()


if __name__ == '__main__':
    main()
