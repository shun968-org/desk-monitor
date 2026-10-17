"""GC9A01 ディスプレイの常駐型表示プロセス(systemd ユーザーサービス desk-monitor-display)。

  face   : 表情アニメーション(face.py)を表示し、trend.py からの吹き出し通知を重ねる
  camera : カメラの全視野の中央の正方形を表示する(camera.to_lcd)。
           ハブ(開発 PC)が物体検出の結果を送ってくれば、検出枠とラベルを重ねる。
           ハブが止まっていれば枠なしの映像だけを出す

カメラは、環境変数 DESK_MONITOR_CAMERA_ADDR があればカメラ配信サーバ(別の Pi の camera_server.py)
の映像を使い(remote_camera.py)、なければこの Pi に付けたカメラを使う(camera.py)。
映像が来ないときは camera モードでも face 表示にする。

プロセスは起動後常駐し、以下のシグナル、またはハブからの操作(control.py)でモードを即時切替する。

  SIGUSR1 : face モードに切替
  SIGUSR2 : camera モードに切替

環境変数(~/.config/desk-monitor/desk-monitor.env。scripts/setup-secrets.sh で作る):
  DESK_MONITOR_TOKEN        機器間の共有トークン。無ければ制御口を開かず、カメラ配信も使わない
  DESK_MONITOR_CAMERA_ADDR  カメラ配信サーバ(例 192.168.10.111:8082)

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.display
        [--mode camera|face] [--brightness 0.0-1.0] [--fps 30]
        [--expression auto|smile|neutral] [--control-port 8083]

依存パッケージ:
    sudo apt install python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio
    # この Pi のカメラを使うときのみ:
    sudo apt install python3-picamera2
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from desk_monitor import wire
from desk_monitor.bubble import BubbleOverlay, start_socket_listener
from desk_monitor.camera import CameraSource, lcd_region, to_lcd
from desk_monitor.control import ControlServer, DetectionOverlay, DisplayControl
from desk_monitor.detector import draw_detections, to_region
from desk_monitor.face import FaceAnimator
from desk_monitor.fonts import get_font
from desk_monitor.lcd import WIDTH, GC9A01
from desk_monitor.remote_camera import RemoteCamera


class ModeController:
    """SIGUSR1/SIGUSR2 で face/camera モードを即時切替する。"""

    def __init__(self, initial_mode: str) -> None:
        self.mode = initial_mode
        signal.signal(signal.SIGUSR1, self._on_face)
        signal.signal(signal.SIGUSR2, self._on_camera)

    def _on_face(self, signum: int, frame: object) -> None:
        self.mode = 'face'

    def _on_camera(self, signum: int, frame: object) -> None:
        self.mode = 'camera'


def _create_camera(token: str):
    """カメラ配信サーバが設定されていればその映像を、なければこの Pi のカメラを使う。"""
    addr = os.environ.get('DESK_MONITOR_CAMERA_ADDR', '')
    if addr and token:
        try:
            return RemoteCamera(wire.parse_addr(addr), token)
        except ValueError as exc:
            print(f'DESK_MONITOR_CAMERA_ADDR が不正です: {exc}', file=sys.stderr, flush=True)
    return CameraSource()


class _Stats:
    """表示プロセスの状態(制御口からハブに送る)。"""

    def __init__(self) -> None:
        self.showing = 'face'
        self.fps = 0.0
        self.detections = 0
        self._frames = 0
        self._since = time.time()

    def tick(self, now: float) -> None:
        self._frames += 1
        if now - self._since >= 2.0:
            self.fps = round(self._frames / (now - self._since), 1)
            self._frames, self._since = 0, now


def run(lcd: GC9A01, fps: float, expression: str, initial_mode: str,
        socket_path: str, control_port: int = 0) -> None:
    controller = ModeController(initial_mode)
    animator = FaceAnimator()
    token = os.environ.get('DESK_MONITOR_TOKEN', '')
    camera = _create_camera(token)
    bubble = BubbleOverlay()
    start_socket_listener(bubble, socket_path)
    overlay = DetectionOverlay()
    stats = _Stats()
    label_font = get_font(13)

    if control_port and token:
        def status() -> dict:
            return {'mode': controller.mode, 'showing': stats.showing, 'fps': stats.fps,
                    'detections': stats.detections,
                    'camera_connected': getattr(camera, 'connected', None)}

        def set_mode(mode: str) -> None:
            controller.mode = mode

        control = DisplayControl(status, set_mode, bubble.show, overlay)
        bubble.add_listener(control.notify_bubble)
        try:
            ControlServer(('0.0.0.0', control_port), token, control).start()
        except OSError as exc:
            print(f'制御口を開けません: {exc}', file=sys.stderr, flush=True)

    period = 1.0 / fps
    try:
        while True:
            t = time.time()
            # camera モードでもカメラの映像が来なければ face 表示にする
            full = camera.frame() if controller.mode == 'camera' else None
            if full is not None:
                arr = to_lcd(full)
                height, width = full.shape[:2]
                detections = to_region(overlay.latest(width, height, t),
                                       *lcd_region(height, width), WIDTH)
                if detections:
                    arr = draw_detections(arr, detections, label_font)
                stats.showing, stats.detections = 'camera', len(detections)
            else:
                # 吹き出しは face 表示のときのみ（camera 表示中は表示時間だけが進む）
                arr = bubble.apply(animator.frame(t, expression), t)
                stats.showing, stats.detections = 'face', 0
            lcd.push_rgb(arr)
            stats.tick(t)
            elapsed = time.time() - t
            if elapsed < period:
                time.sleep(period - elapsed)
    finally:
        camera.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='GC9A01 常駐型表示（SIGUSR1=face, SIGUSR2=camera で即時切替）',
    )
    parser.add_argument('--mode', choices=['face', 'camera'], default='camera',
                        help='起動時の初期表示モード (既定: camera)')
    parser.add_argument('--brightness', type=float, default=1.0,
                        help='バックライト輝度 0.0-1.0 (既定: 1.0)')
    parser.add_argument('--fps', type=float, default=30.0,
                        help='目標フレームレート (既定: 30)')
    parser.add_argument('--expression', choices=['auto', 'smile', 'neutral'],
                        default='auto',
                        help='face モードの表情 (既定: auto = 瞬き＋アイドル表情サイクル)')
    parser.add_argument('--socket-path', default='/tmp/show_face.sock',
                        help='吹き出し通知用 Unix ソケットパス (既定: /tmp/show_face.sock)')
    parser.add_argument('--control-port', type=int, default=8083,
                        help='ハブからの接続を受けるポート。0 で開かない (既定: 8083)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    if not 0.0 <= args.brightness <= 1.0:
        print('brightness は 0.0-1.0 の範囲で指定してください', file=sys.stderr)
        sys.exit(1)

    lcd = GC9A01()
    try:
        lcd.init()
        lcd.bl.value = args.brightness
        run(lcd, args.fps, args.expression, args.mode, args.socket_path, args.control_port)
    except KeyboardInterrupt:
        pass
    finally:
        lcd.close()


if __name__ == '__main__':
    main()
