from __future__ import annotations

import calendar
import json
import socket
import threading
import time

import pytest

from desk_monitor import trend


def test_config_has_required_keys():
    cfg = trend._load_config()
    assert cfg['keywords']
    for key in ('socket_path', 'cooldown_seconds'):
        assert key in cfg
    for key in ('url', 'window_seconds', 'threshold'):
        assert key in cfg['bluesky']
    for key in ('poll_interval_seconds', 'rss_feeds', 'rss_item_window_minutes'):
        assert key in cfg['trends_rss']


def test_burst_counter_reports_when_threshold_reached():
    counter = trend._BurstCounter(['地震', '台風'], window=60, threshold=3)
    assert counter.add('地震だ', 0) == []
    assert counter.add('関係ない投稿', 1) == []
    assert counter.add('また地震', 2) == []
    assert counter.add('地震と台風', 3) == [('地震', 3)]


def test_burst_counter_forgets_old_posts():
    counter = trend._BurstCounter(['地震'], window=10, threshold=2)
    counter.add('地震', 0)
    assert counter.add('地震', 11) == []  # 窓の外の投稿は数えない
    assert counter.add('地震', 12) == [('地震', 2)]


def test_burst_counter_reset():
    counter = trend._BurstCounter(['地震'], window=60, threshold=2)
    counter.add('地震', 0)
    counter.reset('地震')
    assert counter.add('地震', 1) == []


@pytest.mark.parametrize('raw, expected', [
    (json.dumps({'commit': {'record': {'text': '本文'}}}), '本文'),
    (json.dumps({'kind': 'identity'}), ''),
    (json.dumps({'commit': None}), ''),
    ('{壊れた JSON', ''),
])
def test_post_text(raw, expected):
    assert trend._post_text(raw) == expected


def test_rss_matches_recent_entries_only():
    now = 1_000_000.0
    recent = time.gmtime(now - 60)
    old = time.gmtime(now - 3600)
    entries = [
        {'title': '台風が接近', 'summary': '', 'published_parsed': recent},
        {'title': '地震', 'summary': '台風', 'published_parsed': old},
        {'title': '速報', 'summary': '地震'},  # 日時のない記事も対象にする
    ]
    assert trend._rss_matches(entries, ['地震', '台風', '速報'], now, 600) == ['台風', '地震', '速報']
    assert calendar.timegm(recent) == now - 60


def test_cooldown_per_keyword_and_source(monkeypatch):
    now = {'t': 1000.0}
    monkeypatch.setattr(trend.time, 'time', lambda: now['t'])
    cd = trend._Cooldown(300)
    assert cd.try_acquire('地震', 'bluesky')
    assert not cd.try_acquire('地震', 'bluesky')
    assert cd.try_acquire('地震', 'trends_rss')  # 通知元が違えば別扱い
    assert cd.try_acquire('台風', 'bluesky')
    now['t'] += 300
    assert cd.try_acquire('地震', 'bluesky')


def test_notify_sends_json_line(tmp_path):
    path = str(tmp_path / 'n.sock')
    received = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
        srv.bind(path)
        srv.listen(1)

        def serve():
            conn, _ = srv.accept()
            with conn:
                received.append(conn.recv(4096))

        t = threading.Thread(target=serve)
        t.start()
        trend._notify(path, '地震速報', 'bluesky')
        t.join(timeout=3)

    assert received[0].endswith(b'\n')
    assert json.loads(received[0]) == {
        'cmd': 'notify', 'text': '地震速報', 'source': 'bluesky'}


def test_notify_failure_only_warns(tmp_path, caplog):
    trend._notify(str(tmp_path / 'missing.sock'), 'x', 'bluesky')
    assert '通知送信失敗' in caplog.text
