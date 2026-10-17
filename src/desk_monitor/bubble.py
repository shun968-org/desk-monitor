"""face 表示に重ねる吹き出し(trend.py からの通知)と、通知を受ける Unix ソケット。

通信プロトコル(改行区切り JSON):
  {"cmd": "bubble", "text": "...", "source": "bluesky"|"trends_rss", "duration": 12}
"""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from desk_monitor.fonts import get_font

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
    icon_font = get_font(11)
    try:
        draw.text((ix + _ICON_SZ // 2, iy + _ICON_SZ // 2),
                  icon_label, fill=(255, 255, 255), font=icon_font, anchor='mm')
    except TypeError:
        draw.text((ix + 3, iy + 3), icon_label,
                  fill=(255, 255, 255), font=icon_font)

    # 本文テキスト（折り返しあり、最大 3 行）
    text_font = get_font(12)
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
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        """吹き出しを出すたびに listener(text, source, duration) を呼ぶ(ハブへの通知用)。"""
        self._listeners.append(listener)

    def show(self, text: str, source: str, duration: float) -> None:
        with self._lock:
            self._text = text
            self._source = source
            self._start = time.time()
            self._total = duration
        for listener in self._listeners:
            try:
                listener(text, source, duration)
            except Exception:
                pass  # 通知先の不調で吹き出しを止めない

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


def start_socket_listener(bubble: BubbleOverlay, sock_path: str) -> None:
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

