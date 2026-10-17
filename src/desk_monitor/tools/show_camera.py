"""GC9A01 (Waveshare 1.28inch LCD) にカメラ映像をライブ表示する。

desk-monitor-display サービスと LCD・カメラを取り合うため、止めてから使う。

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.tools.show_camera [--brightness 0.0-1.0]

依存パッケージ:
    sudo apt install -y python3-picamera2 python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio
"""
from __future__ import annotations

import argparse
import time

from desk_monitor.lcd import GC9A01, HEIGHT, WIDTH


def main() -> None:
    parser = argparse.ArgumentParser(description='GC9A01 にカメラ映像を表示する')
    parser.add_argument('--brightness', type=float, default=1.0,
                        help='バックライト輝度 0.0-1.0 (既定: 1.0)')
    args = parser.parse_args()

    from picamera2 import Picamera2

    lcd = GC9A01()
    lcd.init()
    lcd.bl.value = args.brightness

    cam = Picamera2()
    config = cam.create_preview_configuration(
        main={"size": (WIDTH, HEIGHT), "format": "RGB888"},
    )
    cam.configure(config)
    cam.start()

    frames = 0
    t0 = time.time()
    try:
        while True:
            arr = cam.capture_array()
            # picamera2 の "RGB888" は実際には BGR 順で返るため反転する
            lcd.push_rgb(arr[:, :, ::-1])
            frames += 1
            if frames % 60 == 0:
                fps = frames / (time.time() - t0)
                print(f'{fps:.1f} fps', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
        cam.close()
        lcd.close()


if __name__ == '__main__':
    main()
