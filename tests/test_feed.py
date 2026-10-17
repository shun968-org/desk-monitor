from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from desk_monitor import display, feed


@pytest.mark.parametrize('addr, expected', [
    ('192.168.10.111:8081', ('192.168.10.111', 8081)),
    ('pi-b.local:9000', ('pi-b.local', 9000)),
])
def test_parse_addr(addr, expected):
    assert feed.parse_addr(addr) == expected


@pytest.mark.parametrize('addr', ['192.168.10.111', ':8081', 'host:port'])
def test_parse_addr_rejects_invalid(addr):
    with pytest.raises(ValueError):
        feed.parse_addr(addr)


def test_encode_jpeg_roundtrip():
    arr = np.zeros((240, 240, 3), dtype=np.uint8)
    arr[:, :120] = (255, 0, 0)
    img = Image.open(io.BytesIO(feed.encode_jpeg(arr)))
    assert img.format == 'JPEG' and img.size == (240, 240)
    r, g, b = img.getpixel((60, 120))
    assert r > 200 and g < 50 and b < 50


def test_wanted_respects_viewers_and_rate():
    sender = feed.DashboardFeed(('127.0.0.1', 1), 't', fps=5, start=False)
    assert not sender.wanted(100.0)
    sender._viewers = 1
    assert sender.wanted(100.0)
    sender.publish(np.zeros((1, 1, 3), dtype=np.uint8), 100.0)
    assert not sender.wanted(100.1)
    assert sender.wanted(100.2)


def test_display_feed_disabled_without_settings(monkeypatch):
    monkeypatch.delenv('DESK_MONITOR_FEED_ADDR', raising=False)
    monkeypatch.delenv('DESK_MONITOR_FEED_TOKEN', raising=False)
    assert display._create_dashboard_feed(5.0) is None


def test_display_feed_rejects_invalid_addr(monkeypatch):
    monkeypatch.setenv('DESK_MONITOR_FEED_ADDR', 'no-port')
    monkeypatch.setenv('DESK_MONITOR_FEED_TOKEN', 't')
    assert display._create_dashboard_feed(5.0) is None


def test_display_feed_created_with_settings(monkeypatch):
    created = []
    monkeypatch.setattr(feed.DashboardFeed, '_run', lambda self: created.append(self))
    monkeypatch.setenv('DESK_MONITOR_FEED_ADDR', '127.0.0.1:8081')
    monkeypatch.setenv('DESK_MONITOR_FEED_TOKEN', 't')
    sender = display._create_dashboard_feed(5.0)
    assert isinstance(sender, feed.DashboardFeed)
