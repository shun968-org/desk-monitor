"""カメラ配信サーバ(camera_server.py)の MJPEG を受けて、表示用のフレームにする。

camera.CameraSource と同じく frame() で全視野の RGB 画を返し、映像が来ていなければ None を返す
(呼び出し側で face 表示にする)。接続は保ったまま、JPEG の展開は camera 表示中だけ行う。
"""
from __future__ import annotations

import io
import logging
import threading
import time

import numpy as np
from PIL import Image

from desk_monitor import wire

log = logging.getLogger(__name__)


class RemoteCamera:
    # これより古いフレームは出さない(配信が止まったら face 表示に戻す)
    STALE_SEC = 1.0
    # frame() がこの秒数呼ばれていなければ、届いた JPEG を展開しない(face 表示中の CPU を節約)
    IDLE_SEC = 2.0
    RETRY_SEC = 5.0

    def __init__(self, addr: tuple[str, int], token: str, start: bool = True) -> None:
        self._addr = addr
        self._token = token
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0
        self._wanted_at = 0.0
        self.connected = False
        self.camera_status: dict | None = None
        if start:
            threading.Thread(target=self._run, daemon=True, name='remote-camera').start()

    def frame(self) -> np.ndarray | None:
        now = time.time()
        with self._lock:
            self._wanted_at = now
            if self._frame is None or now - self._frame_at > self.STALE_SEC:
                return None
            return self._frame

    def close(self) -> None:
        pass  # 接続は daemon スレッドが持ち、プロセス終了とともに閉じる

    def handle(self, header: dict, payload: bytes, now: float | None = None) -> None:
        """カメラ配信サーバからの 1 メッセージを処理する。"""
        now = time.time() if now is None else now
        kind = header.get('type')
        if kind == 'frame':
            with self._lock:
                wanted = now - self._wanted_at < self.IDLE_SEC
            if not wanted:
                return
            try:
                rgb = np.asarray(Image.open(io.BytesIO(payload)).convert('RGB'))
            except OSError as exc:
                log.warning('JPEG を展開できません: %s', exc)
                return
            with self._lock:
                self._frame, self._frame_at = rgb, now
        elif kind == 'status':
            self.camera_status = header

    def _run(self) -> None:
        while True:
            try:
                sock, reader = wire.connect(self._addr, self._token, stream='mjpeg')
                with sock:
                    self.connected = True
                    log.info('カメラ配信に接続しました: %s:%d', *self._addr)
                    while True:
                        header, payload = reader.read()
                        self.handle(header, payload)
            except (OSError, EOFError, wire.ProtocolError) as exc:
                if self.connected:
                    log.warning('カメラ配信との接続が切れました: %r', exc)
                self.connected = False
            time.sleep(self.RETRY_SEC)
