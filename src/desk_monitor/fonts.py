"""吹き出し・検出ラベルの描画に使う日本語フォント。"""
from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

from desk_monitor.paths import FONTS_DIR

# フォント候補(先頭から順に試す)。assets/fonts/ の IPAex ゴシックを同梱しているので、
# 通常は先頭が使われる。
_FONT_CANDIDATES = [
    str(FONTS_DIR / 'ipaexg.ttf'),
    '/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]


def get_font(size: int) -> ImageFont.ImageFont:
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
