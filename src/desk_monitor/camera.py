"""camera モードの映像入力(Picamera2)。

CameraSource は表示用の Pi に直接付けたカメラを使う(カメラ配信サーバを使わない構成)。
カメラ配信サーバ(camera_server.py)の映像は remote_camera.RemoteCamera で受ける。
どちらも全視野(4:3)の画を返し、円形 LCD には to_lcd で中央の正方形を出す。
"""
from __future__ import annotations

import sys
import time

import numpy as np
from PIL import Image

from desk_monitor.lcd import HEIGHT, WIDTH

# ov5647 の 640x480 モード(全視野をビニングしたもの)と同じ大きさで取得し、ISP での拡大縮小を避ける
CAPTURE_WIDTH = 640
CAPTURE_HEIGHT = 480


def lcd_region(height: int, width: int) -> tuple[int, int, int]:
    """全視野の画のうち LCD に出す領域 (x0, y0, 一辺)。中央の正方形。"""
    side = min(width, height)
    return (width - side) // 2, (height - side) // 2, side


def to_lcd(full: np.ndarray) -> np.ndarray:
    """全視野のフレームから中央の正方形を切り出し、LCD の大きさに縮小する。"""
    x0, y0, side = lcd_region(*full.shape[:2])
    factor = side // WIDTH
    if factor < 1 or factor * WIDTH != side or HEIGHT != WIDTH:
        raise ValueError(f'LCD への縮小は整数倍に限る: {side} -> {WIDTH}')
    if factor == 1:
        return full[y0:y0 + side, x0:x0 + side]
    # factor x factor 画素の平均で縮小する(間引きよりちらつきが少ない)。
    # numpy で平均すると Pi 3 では 1 フレーム 120ms かかるため、PIL の reduce を使う(約 7ms)
    img = Image.fromarray(np.ascontiguousarray(full)).crop((x0, y0, x0 + side, y0 + side))
    return np.asarray(img.reduce(factor))


class CameraSource:
    """Picamera2 を遅延初期化し、以後プロセス終了まで保持する。

    camera モードへの切替を毎回瞬時にするため、一度起動したカメラは
    face モードに戻っても停止しない（消費電力よりも切替速度を優先する）。
    """

    # カメラの初期化に失敗したあと、再試行するまでの秒数
    RETRY_SEC = 30.0

    def __init__(self) -> None:
        self._cam = None
        self._retry_at = 0.0

    def frame(self) -> np.ndarray | None:
        """カメラの全視野の映像 (CAPTURE_HEIGHT, CAPTURE_WIDTH, 3) RGB を返す。
        カメラが使えないときは None（呼び出し側で face 表示にする）。

        カメラ未接続（Picamera2() が IndexError）などで落ちると LCD が初期化途中の
        表示のまま止まるため、失敗は握りつぶし、RETRY_SEC ごとに初期化をやり直す。
        """
        if self._cam is None:
            if time.time() < self._retry_at:
                return None
            try:
                self._open()
            except Exception as exc:
                print(f'カメラを初期化できないため face 表示にします: {exc!r}',
                      file=sys.stderr, flush=True)
                self._retry_at = time.time() + self.RETRY_SEC
                return None
        try:
            # picamera2 の "RGB888" は実際には BGR 順で返るため反転し、
            # 鏡像表示に上下左右の反転（180° 回転）を重ねた結果として上下のみ反転する。
            return self._cam.capture_array()[::-1, :, ::-1]
        except Exception as exc:
            print(f'カメラ映像を取得できないため face 表示にします: {exc!r}',
                  file=sys.stderr, flush=True)
            self.close()
            self._retry_at = time.time() + self.RETRY_SEC
            return None

    def _open(self) -> None:
        from picamera2 import Picamera2  # camera モード初回のみ遅延 import
        cam = Picamera2()
        try:
            config = cam.create_preview_configuration(
                main={"size": (CAPTURE_WIDTH, CAPTURE_HEIGHT), "format": "RGB888"},
            )
            cam.configure(config)
            cam.start()
        except Exception:
            cam.close()
            raise
        self._cam = cam

    def close(self) -> None:
        if self._cam is not None:
            cam, self._cam = self._cam, None
            try:
                cam.stop()
                cam.close()
            except Exception:
                pass

