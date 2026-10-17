#!/usr/bin/env python3
"""GC9A01 ディスプレイの常駐型表示スクリプト。

  face   : faces/ ディレクトリの 10 種類の表情画像でアニメーションを表示する
           （通常・笑顔・驚き・嬉しい・考え中・困り顔・悲しい・照れ・
             ウィンク・おやすみ）
  camera : Picamera2 のライブ映像を表示する（鏡像を 180° 回転して表示）
           YOLO で物体を検出し、検出枠とラベル（種類・信頼度）を重ねて描く
           （object_detector.py。onnxruntime かモデルが無ければ映像のみ表示）

プロセスは起動後常駐し、以下のシグナルでモードを即時切替する
（プロセス再起動・LCD/カメラの再初期化を伴わないため瞬時に切り替わる）。

  SIGUSR1 : face モードに切替
  SIGUSR2 : camera モードに切替

カメラは初回 camera モード移行時に起動し、以後プロセス終了まで停止しない
（次回以降の camera モードへの切替を瞬時にするため、消費電力よりも
切替速度を優先する）。

使い方:
    python3 show_face.py [--mode camera|face] [--brightness 0.0-1.0]
                          [--fps 30] [--expression auto|smile|neutral]
                          [--no-detect] [--model PATH] [--conf 0.4]
                          [--detect-interval 1.0]

依存パッケージ:
    sudo apt install python3-pil python3-numpy python3-spidev python3-gpiozero python3-lgpio
    # camera モード時のみ:
    sudo apt install python3-picamera2
    # 物体検出を使う場合のみ（apt の python3-onnxruntime は Pi 3 で落ちるため pip で入れる）:
    ~/desk-monitor/.venv/bin/pip install -r requirements-pi.txt  # scripts/deploy.sh が実行する
"""
from __future__ import annotations

import argparse
import enum
import json
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import spidev
from gpiozero import DigitalOutputDevice, PWMOutputDevice
from PIL import Image, ImageDraw, ImageFont

# Pin (BCM)
PIN_DC = 25
PIN_RST = 27
PIN_BL = 18

WIDTH = 240
HEIGHT = 240
SPI_HZ = 40_000_000

FACES_DIR = Path(__file__).parent / 'faces'
DEFAULT_MODEL = Path(__file__).parent / 'models' / 'yolo11n.onnx'

# アイドルアニメーションで使う表情と保持時間 (秒)
_IDLE_CHOICES = [
    ('smile',      2.0, 3.5),
    ('thinking',   2.0, 4.0),
    ('wink',       1.5, 2.5),
    ('happy',      1.5, 2.5),
    ('embarrassed', 1.5, 2.5),
]

# faces/ ディレクトリ内のファイルマッピング
_FACE_FILES: dict[str, str] = {
    'normal':      '01_normal.png',
    'smile':       '02_smile.png',
    'surprised':   '03_surprised.png',
    'happy':       '04_happy.png',
    'thinking':    '05_thinking.png',
    'troubled':    '06_troubled.png',
    'sad':         '07_sad.png',
    'embarrassed': '08_embarrassed.png',
    'wink':        '09_wink.png',
    'sleepy':      '10_sleepy.png',
}


class GC9A01:
    def __init__(self) -> None:
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
        seq: list[tuple[int, list[int]]] = [
            (0xEF, []), (0xEB, [0x14]), (0xFE, []), (0xEF, []), (0xEB, [0x14]),
            (0x84, [0x40]), (0x85, [0xFF]), (0x86, [0xFF]), (0x87, [0xFF]),
            (0x88, [0x0A]), (0x89, [0x21]), (0x8A, [0x00]), (0x8B, [0x80]),
            (0x8C, [0x01]), (0x8D, [0x01]), (0x8E, [0xFF]), (0x8F, [0xFF]),
            (0xB6, [0x00, 0x20]),
            (0x36, [0x08]),
            (0x3A, [0x05]),
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
            (0x35, []), (0x21, []), (0x11, []),
        ]
        for cmd, args in seq:
            self._cmd(cmd)
            if args:
                self._data(*args)
        time.sleep(0.12)
        self._cmd(0x29)
        time.sleep(0.02)

    def _set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._cmd(0x2A); self._data(x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)
        self._cmd(0x2B); self._data(y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._cmd(0x2C)

    def push_rgb(self, arr: np.ndarray) -> None:
        a = arr.astype(np.uint16)
        r = (a[..., 0] & 0xF8) << 8
        g = (a[..., 1] & 0xFC) << 3
        b = a[..., 2] >> 3
        rgb565 = (r | g | b).astype('>u2')
        self._set_window(0, 0, WIDTH - 1, HEIGHT - 1)
        self.dc.on()
        self.spi.writebytes2(rgb565.tobytes())


def _load_resized(path: Path) -> np.ndarray:
    img = Image.open(path).convert('RGB').resize((WIDTH, HEIGHT), Image.LANCZOS)
    return np.asarray(img, dtype=np.uint8)


def _cross_fade(a: np.ndarray, b: np.ndarray, alpha: float) -> np.ndarray:
    """alpha=0 で a、alpha=1 で b。"""
    if alpha <= 0.0:
        return a
    if alpha >= 1.0:
        return b
    w = int(alpha * 256)
    return ((a.astype(np.uint32) * (256 - w)
             + b.astype(np.uint32) * w) >> 8).astype(np.uint8)


# 瞬きアニメーション対象領域 [(y0, y1, x0, x1)]
# 目と口のみをブレンドし、髪・背景・服は 01_normal のまま保持する。
# zzz テキスト (x < 45) を含まないよう x0=45 から開始。
_BLINK_REGIONS: list[tuple[int, int, int, int]] = [
    (88, 148, 45, 215),   # 両目（眉含む）
    (148, 182, 65, 178),  # 口
]


def _blend_regions(
    base: np.ndarray,
    overlay: np.ndarray,
    alpha: float,
    regions: list[tuple[int, int, int, int]],
) -> np.ndarray:
    """指定矩形領域のみ overlay をブレンドし、それ以外は base をそのまま返す。

    alpha=0 で base 全体、alpha=1 で regions 内のみ overlay に置換。
    """
    if alpha <= 0.0:
        return base
    result = base.copy()
    w = int(alpha * 256)
    for y0, y1, x0, x1 in regions:
        b = base[y0:y1, x0:x1].astype(np.uint32)
        o = overlay[y0:y1, x0:x1].astype(np.uint32)
        result[y0:y1, x0:x1] = ((b * (256 - w) + o * w) >> 8).astype(np.uint8)
    return result


# ─── 吹き出し描画 ────────────────────────────────────────────────────────

# フォント候補（先頭から順に試す）
# show_face_fonts/ は apt-get download + dpkg-deb -x で sudo なしで展開したフォントの置き場
_FONT_CANDIDATES = [
    str(Path(__file__).parent / 'show_face_fonts/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf'),
    '/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]

# ソース別アイコン色・ラベル
_BUBBLE_COLORS = {
    'bluesky':    (0, 133, 255),   # Bluesky ブルー
    'trends_rss': (255, 102, 0),   # RSS/トレンド オレンジ
}
_BUBBLE_LABELS = {
    'bluesky':    'B',
    'trends_rss': 'R',
}

# 吹き出しレイアウト定数
# GC9A01 は 240×240 の円形ディスプレイ（中心 120,120 半径 120）。
# y=35 での利用可能幅: sqrt(120²-85²)×2 ≈ 169px → 両端 5px マージンで x=40-200。
_BX = 40;  _BY = 35
_BW = 160; _BH = 68
_BR = 10               # 角丸半径
_TAIL_H = 12           # しっぽの高さ
_ICON_SZ = 20          # アイコン円の直径
_ICON_PAD = 6          # アイコン左マージン


def _get_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    line = ''
    for ch in text:
        test = line + ch
        try:
            w = draw.textlength(test, font=font)
        except AttributeError:
            w = len(test) * getattr(font, 'size', 8)
        if w > max_width and line:
            lines.append(line)
            line = ch
        else:
            line = test
    if line:
        lines.append(line)
    return lines


def _render_bubble(
    arr: np.ndarray, text: str, source: str, alpha: float
) -> np.ndarray:
    """吹き出しを arr に alpha ブレンドして返す。alpha=1 で完全表示。"""
    overlay = Image.fromarray(arr).copy()
    draw = ImageDraw.Draw(overlay)
    border_color = _BUBBLE_COLORS.get(source, (100, 100, 100))
    icon_label = _BUBBLE_LABELS.get(source, '?')

    bx1, by1 = _BX, _BY
    bx2, by2 = _BX + _BW, _BY + _BH

    # 吹き出し本体（丸角矩形）
    try:
        draw.rounded_rectangle(
            [bx1, by1, bx2, by2], radius=_BR,
            fill=(255, 255, 255), outline=border_color, width=2,
        )
    except AttributeError:
        draw.rectangle(
            [bx1, by1, bx2, by2],
            fill=(255, 255, 255), outline=border_color, width=2,
        )

    # しっぽ（下向き三角）
    cx = (bx1 + bx2) // 2
    tip = by2 + _TAIL_H
    draw.polygon([(cx - 8, by2), (cx + 8, by2), (cx, tip)],
                 fill=(255, 255, 255))
    draw.line([(cx - 8, by2 - 1), (cx, tip)], fill=border_color, width=2)
    draw.line([(cx + 8, by2 - 1), (cx, tip)], fill=border_color, width=2)

    # ソースアイコン（円 + ラベル）
    ix = bx1 + _ICON_PAD
    iy = by1 + (_BH - _ICON_SZ) // 2
    draw.ellipse([ix, iy, ix + _ICON_SZ, iy + _ICON_SZ], fill=border_color)
    icon_font = _get_font(11)
    try:
        draw.text((ix + _ICON_SZ // 2, iy + _ICON_SZ // 2),
                  icon_label, fill=(255, 255, 255), font=icon_font, anchor='mm')
    except TypeError:
        draw.text((ix + 3, iy + 3), icon_label,
                  fill=(255, 255, 255), font=icon_font)

    # 本文テキスト（折り返しあり、最大 3 行）
    text_font = _get_font(12)
    tx = ix + _ICON_SZ + 6
    lines = _wrap_text(draw, text, text_font, bx2 - tx - 6)
    for i, line in enumerate(lines[:3]):
        draw.text((tx, by1 + 8 + i * 15), line,
                  fill=(30, 30, 30), font=text_font)

    result = Image.blend(Image.fromarray(arr), overlay, alpha)
    return np.asarray(result, dtype=np.uint8)


class BubbleOverlay:
    """吹き出しオーバーレイをスレッドセーフに管理しフェードイン/アウトを適用する。"""

    FADE_DUR = 0.15

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ''
        self._source = ''
        self._start: float | None = None
        self._total = 0.0

    def show(self, text: str, source: str, duration: float) -> None:
        with self._lock:
            self._text = text
            self._source = source
            self._start = time.time()
            self._total = duration

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        with self._lock:
            if self._start is None:
                return frame
            dt = t - self._start
            if dt >= self._total:
                self._start = None
                return frame
            fade = self.FADE_DUR
            if dt < fade:
                alpha = dt / fade
            elif dt > self._total - fade:
                alpha = (self._total - dt) / fade
            else:
                alpha = 1.0
            text, source = self._text, self._source
        try:
            return _render_bubble(frame, text, source, alpha)
        except Exception:
            return frame


def _start_socket_listener(bubble: BubbleOverlay, sock_path: str) -> None:
    """Unix ドメインソケットを別スレッドで待機し bubble コマンドを受け取る。"""
    path = Path(sock_path)

    def _serve() -> None:
        if path.exists():
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(path))
            srv.listen(8)
            srv.settimeout(1.0)
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                try:
                    raw = conn.recv(4096).decode(errors='replace')
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get('cmd') == 'bubble':
                            bubble.show(
                                msg.get('text', ''),
                                msg.get('source', ''),
                                float(msg.get('duration', 10)),
                            )
                finally:
                    conn.close()

    threading.Thread(target=_serve, daemon=True, name='sock-listener').start()


# ─── アニメーション状態機械 ──────────────────────────────────────────────

class _AnimState(enum.Enum):
    """FaceAnimator の内部状態。"""
    NORMAL     = 'normal'      # 通常（次の瞬き/表情変化を待機）
    BLINK_DOWN = 'blink_down'  # 瞼を閉じる
    BLINK_HOLD = 'blink_hold'  # 目を閉じたまま保持
    BLINK_UP   = 'blink_up'    # 瞼を開く
    IDLE_IN    = 'idle_in'     # アイドル表情にクロスフェード
    IDLE_HOLD  = 'idle_hold'   # アイドル表情を保持
    IDLE_OUT   = 'idle_out'    # 通常にクロスフェードで戻る


class FaceAnimator:
    """10 種類の表情画像を使って face モードのアニメーションを生成する。

    状態機械でまばたき・アイドル表情サイクルを管理し、滑らかに表情を遷移させる。

    まばたき (blink):
        目・口領域のみ 01_normal → 10_sleepy をブレンド (0.10s) →
        目を閉じた状態を保持 (0.25s) → ブレンドを戻す (0.10s)。
        背景・髪・服は 01_normal のまま変化しない (_BLINK_REGIONS 参照)。

    アイドル表情 (idle, auto モードのみ):
        通常 → ランダム表情をクロスフェード (0.18s) → 保持 (1.5–4s) →
        通常にクロスフェード (0.18s)
    """

    BLINK_DOWN_DUR = 0.10
    BLINK_HOLD_DUR = 0.25
    BLINK_UP_DUR   = 0.10
    FADE_DUR       = 0.18

    def __init__(self) -> None:
        missing = [k for k, f in _FACE_FILES.items()
                   if not (FACES_DIR / f).is_file()]
        if missing:
            print(f'顔画像が見つかりません: {missing}', file=sys.stderr)
            sys.exit(1)
        self.imgs: dict[str, np.ndarray] = {
            key: _load_resized(FACES_DIR / fname)
            for key, fname in _FACE_FILES.items()
        }
        now = time.time()
        self._state = _AnimState.NORMAL
        self._state_at = now
        self._idle_key = 'normal'
        self._idle_hold_dur = 2.0
        self._next_blink = now + random.uniform(2.0, 5.0)
        self._next_idle  = now + random.uniform(10.0, 18.0)

    def frame(self, t: float, expression: str) -> np.ndarray:
        """t: 現在時刻 (time.time())、expression: 'auto'|'smile'|'neutral'"""
        if expression == 'smile':
            return self.imgs['smile']

        self._update_state(t, expression)
        return self._render(t)

    def _update_state(self, t: float, expression: str) -> None:
        dt = t - self._state_at

        if self._state == _AnimState.NORMAL:
            if expression == 'auto' and t >= self._next_idle:
                key, min_h, max_h = random.choice(_IDLE_CHOICES)
                self._idle_key = key
                self._idle_hold_dur = random.uniform(min_h, max_h)
                self._transition(t, _AnimState.IDLE_IN)
            elif t >= self._next_blink:
                self._transition(t, _AnimState.BLINK_DOWN)

        elif self._state == _AnimState.BLINK_DOWN and dt >= self.BLINK_DOWN_DUR:
            self._transition(t, _AnimState.BLINK_HOLD)

        elif self._state == _AnimState.BLINK_HOLD and dt >= self.BLINK_HOLD_DUR:
            self._transition(t, _AnimState.BLINK_UP)

        elif self._state == _AnimState.BLINK_UP and dt >= self.BLINK_UP_DUR:
            self._next_blink = t + random.uniform(2.5, 6.0)
            self._transition(t, _AnimState.NORMAL)

        elif self._state == _AnimState.IDLE_IN and dt >= self.FADE_DUR:
            self._transition(t, _AnimState.IDLE_HOLD)

        elif self._state == _AnimState.IDLE_HOLD and dt >= self._idle_hold_dur:
            self._transition(t, _AnimState.IDLE_OUT)

        elif self._state == _AnimState.IDLE_OUT and dt >= self.FADE_DUR:
            self._next_idle = t + random.uniform(10.0, 18.0)
            self._next_blink = t + random.uniform(2.5, 5.0)
            self._transition(t, _AnimState.NORMAL)

    def _transition(self, t: float, new_state: _AnimState) -> None:
        self._state = new_state
        self._state_at = t

    def _render(self, t: float) -> np.ndarray:
        dt = t - self._state_at

        if self._state == _AnimState.NORMAL:
            return self.imgs['normal']

        if self._state == _AnimState.BLINK_DOWN:
            alpha = min(1.0, dt / self.BLINK_DOWN_DUR)
            return _blend_regions(self.imgs['normal'], self.imgs['sleepy'],
                                  alpha, _BLINK_REGIONS)

        if self._state == _AnimState.BLINK_HOLD:
            return _blend_regions(self.imgs['normal'], self.imgs['sleepy'],
                                  1.0, _BLINK_REGIONS)

        if self._state == _AnimState.BLINK_UP:
            alpha = max(0.0, 1.0 - dt / self.BLINK_UP_DUR)
            return _blend_regions(self.imgs['normal'], self.imgs['sleepy'],
                                  alpha, _BLINK_REGIONS)

        if self._state == _AnimState.IDLE_IN:
            alpha = min(1.0, dt / self.FADE_DUR)
            return _cross_fade(self.imgs['normal'], self.imgs[self._idle_key], alpha)

        if self._state == _AnimState.IDLE_HOLD:
            return self.imgs[self._idle_key]

        if self._state == _AnimState.IDLE_OUT:
            alpha = max(0.0, 1.0 - dt / self.FADE_DUR)
            return _cross_fade(self.imgs['normal'], self.imgs[self._idle_key], alpha)

        return self.imgs['normal']


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
        from object_detector import DetectionWorker, YoloDetector
        return DetectionWorker(YoloDetector(model_path, conf_threshold=conf),
                               min_interval=interval)
    except Exception as exc:
        print(f'物体検出を初期化できないため無効にします: {exc}',
              file=sys.stderr, flush=True)
        return None


def run(lcd: GC9A01, fps: float, expression: str, initial_mode: str,
        socket_path: str, detect: bool, model_path: Path, conf: float,
        detect_interval: float) -> None:
    controller = ModeController(initial_mode)
    animator = FaceAnimator()
    camera = CameraSource()
    bubble = BubbleOverlay()
    _start_socket_listener(bubble, socket_path)
    detector = (_create_detection_worker(model_path, conf, detect_interval)
                if detect else None)
    if detector is not None:
        from object_detector import draw_detections
        label_font = _get_font(13)
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
            else:
                # 吹き出しは face 表示のときのみ（camera 表示中は表示時間だけが進む）
                arr = bubble.apply(animator.frame(t, expression), t)
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
    args = parser.parse_args()

    if not 0.0 <= args.brightness <= 1.0:
        print('brightness は 0.0-1.0 の範囲で指定してください', file=sys.stderr)
        sys.exit(1)

    lcd = GC9A01()
    try:
        lcd.init()
        lcd.bl.value = args.brightness
        run(lcd, args.fps, args.expression, args.mode, args.socket_path,
            args.detect, args.model, args.conf, args.detect_interval)
    except KeyboardInterrupt:
        pass
    finally:
        lcd.close()


if __name__ == '__main__':
    main()
