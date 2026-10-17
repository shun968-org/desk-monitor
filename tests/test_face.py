from __future__ import annotations

import numpy as np
import pytest

from desk_monitor import face


def _solid(value: int) -> np.ndarray:
    return np.full((240, 240, 3), value, dtype=np.uint8)


def test_cross_fade_endpoints_and_midpoint():
    a, b = _solid(0), _solid(200)
    assert face._cross_fade(a, b, 0.0) is a
    assert face._cross_fade(a, b, 1.0) is b
    assert face._cross_fade(a, b, 0.5)[0, 0, 0] == 100


def test_blend_regions_changes_only_regions():
    base, overlay = _solid(0), _solid(255)
    out = face._blend_regions(base, overlay, 1.0, [(10, 20, 30, 40)])
    assert (out[10:20, 30:40] >= 254).all()  # alpha=1 は 255/256 倍になる
    mask = np.ones(out.shape[:2], dtype=bool)
    mask[10:20, 30:40] = False
    assert (out[mask] == 0).all()
    assert (base == 0).all()  # 元画像は書き換えない
    assert face._blend_regions(base, overlay, 0.0, [(0, 1, 0, 1)]) is base


def test_all_face_images_exist():
    for fname in face._FACE_FILES.values():
        assert (face.FACES_DIR / fname).is_file(), fname


@pytest.fixture
def animator(monkeypatch):
    """乱数を固定した FaceAnimator(時刻は 0 から始める)。"""
    monkeypatch.setattr(face.time, 'time', lambda: 0.0)
    monkeypatch.setattr(face.random, 'uniform', lambda lo, hi: lo)
    monkeypatch.setattr(face.random, 'choice', lambda seq: seq[0])
    return face.FaceAnimator()


def test_frames_are_display_sized(animator):
    frame = animator.frame(0.0, 'auto')
    assert frame.shape == (240, 240, 3) and frame.dtype == np.uint8


def test_smile_is_fixed(animator):
    assert animator.frame(100.0, 'smile') is animator.imgs['smile']


def test_blink_cycle(animator):
    imgs = animator.imgs
    assert animator.frame(1.0, 'neutral') is imgs['normal']
    # 2.0 秒(uniform の下限)で瞬きが始まり、閉じ → 保持 → 開く → 通常に戻る
    animator.frame(2.0, 'neutral')
    assert animator._state is face._AnimState.BLINK_DOWN
    animator.frame(2.11, 'neutral')
    assert animator._state is face._AnimState.BLINK_HOLD
    closed = animator.frame(2.2, 'neutral')
    y0, y1, x0, x1 = face._BLINK_REGIONS[0]
    assert np.array_equal(closed[y0:y1, x0:x1], imgs['sleepy'][y0:y1, x0:x1])
    assert np.array_equal(closed[:y0], imgs['normal'][:y0])  # 領域外は通常のまま
    animator.frame(2.37, 'neutral')
    assert animator._state is face._AnimState.BLINK_UP
    assert animator.frame(2.48, 'neutral') is imgs['normal']
    assert animator._state is face._AnimState.NORMAL


def test_idle_expression_only_in_auto(animator):
    animator._next_blink = 1e9
    animator.frame(10.0, 'neutral')
    assert animator._state is face._AnimState.NORMAL  # neutral ではアイドル表情にしない
    animator.frame(10.0, 'auto')
    assert animator._state is face._AnimState.IDLE_IN
    animator.frame(10.2, 'auto')
    assert animator._state is face._AnimState.IDLE_HOLD
    key = face._IDLE_CHOICES[0][0]
    assert animator.frame(11.0, 'auto') is animator.imgs[key]
    animator.frame(12.3, 'auto')  # 保持時間(下限 2.0 秒)を過ぎたら戻り始める
    assert animator._state is face._AnimState.IDLE_OUT
    assert animator.frame(12.5, 'auto') is animator.imgs['normal']
    assert animator._state is face._AnimState.NORMAL


def test_missing_face_image_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(face, 'FACES_DIR', tmp_path)
    with pytest.raises(SystemExit):
        face.FaceAnimator()
