"""camera モードの映像入力(Picamera2)。"""
from __future__ import annotations

import sys
import time

import numpy as np

from desk_monitor.lcd import HEIGHT, WIDTH


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
        """カメラ映像を返す。カメラが使えないときは None（呼び出し側で face 表示にする）。

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
                main={"size": (WIDTH, HEIGHT), "format": "RGB888"},
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

