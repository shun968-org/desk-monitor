"""face モードの表情アニメーション(assets/faces/ の 10 種類の表情画像を使う)。

通常・笑顔・驚き・嬉しい・考え中・困り顔・悲しい・照れ・ウィンク・おやすみ
"""
from __future__ import annotations

import enum
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

from desk_monitor.lcd import HEIGHT, WIDTH
from desk_monitor.paths import FACES_DIR

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

