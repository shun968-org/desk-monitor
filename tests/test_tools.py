from __future__ import annotations

import sys

import pytest
from PIL import Image

from desk_monitor.tools import show_image


def test_show_image_sends_resized_image(fake_hardware, monkeypatch, tmp_path):
    monkeypatch.setattr('desk_monitor.lcd.time.sleep', lambda s: None)
    path = tmp_path / 'red.png'
    Image.new('RGB', (100, 50), (255, 0, 0)).save(path)
    monkeypatch.setattr(sys, 'argv', ['show_image', str(path), '--brightness', '0.5'])
    show_image.main()
    spi = fake_hardware[0]
    assert spi.writes[-1] == b'\xf8\x00' * (240 * 240)  # 全画素が赤(RGB565)
    assert spi.closed


@pytest.mark.parametrize('args', [['missing.png'], ['IMAGE', '--brightness', '2']])
def test_show_image_rejects_bad_arguments(monkeypatch, tmp_path, args):
    image = tmp_path / 'a.png'
    Image.new('RGB', (1, 1)).save(image)
    argv = [str(image) if a == 'IMAGE' else str(tmp_path / a) if a.endswith('.png') else a
            for a in args]
    monkeypatch.setattr(sys, 'argv', ['show_image', *argv])
    with pytest.raises(SystemExit):
        show_image.main()
