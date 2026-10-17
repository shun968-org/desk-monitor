"""GC9A01 ディスプレイの常駐型表示プロセス(systemd ユーザーサービス desk-monitor-display)。

  face   : 表情アニメーション(face.py)を表示し、trend.py からの吹き出し通知を重ねる
  camera : Picamera2 のライブ映像を表示する(鏡像を 180° 回転して表示)
           YOLO で物体を検出し、検出枠とラベル(種類・信頼度)を重ねて描く
           (detector.py。onnxruntime かモデルが無ければ映像のみ表示)
           ダッシュボード(pi_b の dashboard.py)が設定されていれば、閲覧者がいるときだけ
           同じ画を送る(feed.py)

プロセスは起動後常駐し、以下のシグナルでモードを即時切替する
(プロセス再起動・LCD/カメラの再初期化を伴わないため瞬時に切り替わる)。

  SIGUSR1 : face モードに切替
  SIGUSR2 : camera モードに切替

カメラは初回 camera モード移行時に起動し、以後プロセス終了まで停止しない
(次回以降の camera モードへの切替を瞬時にするため、消費電力よりも
切替速度を優先する)。

使い方(リポジトリ直下で):
    PYTHONPATH=src python3 -m desk_monitor.display
        [--mode camera|face] [--brightness 0.0-1.0] [--fps 30]
        [--expression auto|smile|neutral] [--no-detect] [--model PATH]
        [--conf 0.4] [--detect-interval 1.0] [--dashboard-fps 5]

依存パッケージ:
    sudo apt install python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio
    # camera モード時のみ:
    sudo apt install python3-picamera2
    # 物体検出を使う場合のみ(apt の python3-onnxruntime は Pi 3 で落ちるため pip で入れる):
    ~/desk-monitor/.venv/bin/pip install -r requirements-pi.txt  # scripts/deploy.sh が実行する
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from desk_monitor.bubble import BubbleOverlay, start_socket_listener
from desk_monitor.camera import CameraSource
from desk_monitor.face import FaceAnimator
from desk_monitor.fonts import get_font
from desk_monitor.lcd import GC9A01
from desk_monitor.paths import MODELS_DIR

DEFAULT_MODEL = MODELS_DIR / 'yolo11n.onnx'


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


def _onnxruntime_importable() -> bool:
    """onnxruntime を別プロセスで import できるか確かめる。

    CPU が対応しない命令でビルドされた onnxruntime（Debian の python3-onnxruntime を
    Pi 3 で使った場合など）は import 時に SIGILL でプロセスごと落ち、例外では
    捕捉できないため、表示プロセス本体で import する前に子プロセスで試す。
    """
    try:
        result = subprocess.run([sys.executable, '-c', 'import onnxruntime'],
                                capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _create_detection_worker(model_path: Path, conf: float, interval: float):
    """物体検出ワーカーを作る。使えない環境では None を返し、映像表示だけを続ける。"""
    if not model_path.is_file():
        print(f'物体検出モデルが見つからないため検出を無効にします: {model_path}',
              file=sys.stderr, flush=True)
        return None
    if not _onnxruntime_importable():
        print('onnxruntime を読み込めないため検出を無効にします',
              file=sys.stderr, flush=True)
        return None
    try:
        from desk_monitor.detector import DetectionWorker, YoloDetector
        return DetectionWorker(YoloDetector(model_path, conf_threshold=conf),
                               min_interval=interval)
    except Exception as exc:
        print(f'物体検出を初期化できないため無効にします: {exc}',
              file=sys.stderr, flush=True)
        return None


def _create_dashboard_feed(fps: float):
    """ダッシュボード(pi_b)への送信を用意する。接続先が設定されていなければ None。"""
    addr = os.environ.get('DESK_MONITOR_FEED_ADDR', '')
    token = os.environ.get('DESK_MONITOR_FEED_TOKEN', '')
    if not addr or not token:
        return None
    try:
        from desk_monitor.feed import DashboardFeed, parse_addr
        return DashboardFeed(parse_addr(addr), token, fps=fps)
    except ValueError as exc:
        print(f'ダッシュボードの接続先が不正なため送信しません: {exc}',
              file=sys.stderr, flush=True)
        return None


def run(lcd: GC9A01, fps: float, expression: str, initial_mode: str,
        socket_path: str, detect: bool, model_path: Path, conf: float,
        detect_interval: float, dashboard_fps: float = 0.0) -> None:
    controller = ModeController(initial_mode)
    animator = FaceAnimator()
    camera = CameraSource()
    bubble = BubbleOverlay()
    start_socket_listener(bubble, socket_path)
    detector = (_create_detection_worker(model_path, conf, detect_interval)
                if detect else None)
    if detector is not None:
        from desk_monitor.detector import draw_detections
        label_font = get_font(13)
    feed = _create_dashboard_feed(dashboard_fps) if dashboard_fps > 0 else None
    period = 1.0 / fps

    try:
        while True:
            t = time.time()
            # camera モードでもカメラが使えなければ face 表示にする
            arr = camera.frame() if controller.mode == 'camera' else None
            if arr is not None:
                if detector is not None:
                    detector.submit(arr)
                    arr = draw_detections(arr, detector.latest(t), label_font)
                if feed is not None:
                    # ダッシュボードには LCD と同じ画(検出枠つき)を、見ている人がいるときだけ送る
                    feed.set_mode('camera')
                    if feed.wanted(t):
                        feed.publish(arr, t)
            else:
                # 吹き出しは face 表示のときのみ（camera 表示中は表示時間だけが進む）
                arr = bubble.apply(animator.frame(t, expression), t)
                if feed is not None:
                    feed.set_mode('face')
            lcd.push_rgb(arr)
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
    parser.add_argument('--detect', action=argparse.BooleanOptionalAction, default=True,
                        help='camera モードで物体検出の枠とラベルを表示する (既定: 有効)')
    parser.add_argument('--model', type=Path, default=DEFAULT_MODEL,
                        help=f'物体検出の ONNX モデル (既定: {DEFAULT_MODEL})')
    parser.add_argument('--conf', type=float, default=0.4,
                        help='物体検出の信頼度しきい値 0.0-1.0 (既定: 0.4)')
    parser.add_argument('--detect-interval', type=float, default=1.0,
                        help='物体検出の実行間隔の下限 秒 (既定: 1.0。発熱を抑えるため)')
    parser.add_argument('--dashboard-fps', type=float, default=5.0,
                        help='ダッシュボードに送る映像の上限 fps。0 で送らない (既定: 5。'
                             '接続先は環境変数 DESK_MONITOR_FEED_ADDR / DESK_MONITOR_FEED_TOKEN)')
    args = parser.parse_args()

    if not 0.0 <= args.brightness <= 1.0:
        print('brightness は 0.0-1.0 の範囲で指定してください', file=sys.stderr)
        sys.exit(1)

    lcd = GC9A01()
    try:
        lcd.init()
        lcd.bl.value = args.brightness
        run(lcd, args.fps, args.expression, args.mode, args.socket_path,
            args.detect, args.model, args.conf, args.detect_interval,
            args.dashboard_fps)
    except KeyboardInterrupt:
        pass
    finally:
        lcd.close()


if __name__ == '__main__':
    main()
