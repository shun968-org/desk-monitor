from __future__ import annotations

import json
import socket
import time

import numpy as np
from PIL import Image, ImageDraw

from desk_monitor import bubble
from desk_monitor.fonts import get_font


def test_apply_without_bubble_returns_frame(blank_frame):
    overlay = bubble.BubbleOverlay()
    assert overlay.apply(blank_frame, time.time()) is blank_frame


def test_bubble_fades_in_shows_and_expires(blank_frame):
    overlay = bubble.BubbleOverlay()
    overlay.show('地震速報', 'bluesky', 2.0)
    start = overlay._start

    full = overlay.apply(blank_frame, start + 1.0)
    # 吹き出し本体(白)が描かれる
    assert (full[bubble._BY + 5, bubble._BX + bubble._BW - 10] == 255).all()

    half = overlay.apply(blank_frame, start + bubble.BubbleOverlay.FADE_DUR / 2)
    px = half[bubble._BY + 5, bubble._BX + bubble._BW - 10]
    assert 0 < int(px[0]) < 255  # フェード途中は半透明

    assert overlay.apply(blank_frame, start + 2.0) is blank_frame
    assert overlay._start is None  # 表示時間を過ぎたら消える


def test_render_failure_falls_back_to_frame(blank_frame, monkeypatch):
    def boom(*args):
        raise RuntimeError('描画失敗')
    monkeypatch.setattr(bubble, '_render_bubble', boom)
    overlay = bubble.BubbleOverlay()
    overlay.show('x', 'bluesky', 5)
    assert overlay.apply(blank_frame, overlay._start + 1) is blank_frame


def test_unknown_source_uses_default_color(blank_frame):
    out = bubble._render_bubble(blank_frame, 'テスト', 'unknown', 1.0)
    assert out.shape == blank_frame.shape


def test_wrap_text_respects_width():
    draw = ImageDraw.Draw(Image.new('RGB', (10, 10)))
    font = get_font(12)
    text = 'あいうえおかきくけこさしすせそたちつてと'
    lines = bubble._wrap_text(draw, text, font, 60)
    assert ''.join(lines) == text
    assert len(lines) > 1
    assert all(draw.textlength(line, font=font) <= 60 for line in lines)


def _wait_until(cond, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_socket_listener_receives_bubble(tmp_path):
    path = tmp_path / 'bubble.sock'
    path.touch()  # 残っている古いソケットファイルは消して作り直す
    overlay = bubble.BubbleOverlay()
    bubble.start_socket_listener(overlay, str(path))
    assert _wait_until(lambda: path.is_socket())

    msg = {'cmd': 'bubble', 'text': '台風', 'source': 'trends_rss', 'duration': 7}
    payload = '壊れた行\n' + json.dumps({'cmd': 'other'}) + '\n' + json.dumps(msg, ensure_ascii=False) + '\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        s.sendall(payload.encode())

    assert _wait_until(lambda: overlay._start is not None)
    assert (overlay._text, overlay._source, overlay._total) == ('台風', 'trends_rss', 7.0)
