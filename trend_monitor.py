#!/usr/bin/env python3
"""トレンド監視デーモン。

Bluesky Jetstream と Google Trends + RSS の 2 経路でキーワードの急増を検知し、
show_face.py の Unix ドメインソケット経由で吹き出し通知を送る。

通信プロトコル (show_face.py との Unix socket — 改行区切り JSON):
  {"cmd": "bubble", "text": "...", "source": "bluesky"|"trends_rss", "duration": 12}

設定ファイル: 同ディレクトリの trend_config.json

Pi 側の依存パッケージ:
  sudo apt install python3-websockets python3-feedparser
  pip install pytrends   # Google Trends 使用時のみ（任意）
"""
from __future__ import annotations

import asyncio
import calendar
import collections
import json
import logging
import socket
import time
from pathlib import Path

import feedparser
import websockets

log = logging.getLogger(__name__)
_CONFIG_PATH = Path(__file__).parent / 'trend_config.json'


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def _notify(sock_path: str, text: str, source: str, duration: int) -> None:
    """show_face.py の Unix ソケットに JSON 1 行を送る。接続失敗は警告のみ。"""
    payload = (
        json.dumps({'cmd': 'bubble', 'text': text, 'source': source,
                    'duration': duration}, ensure_ascii=False)
        + '\n'
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall(payload.encode())
    except Exception as exc:
        log.warning('通知送信失敗 (%s): %s', sock_path, exc)


class _Cooldown:
    """(keyword, source) ペアごとに一定期間の通知抑制を管理する。"""

    def __init__(self, seconds: float) -> None:
        self._s = seconds
        self._last: dict[tuple[str, str], float] = {}

    def try_acquire(self, keyword: str, source: str) -> bool:
        key = (keyword, source)
        now = time.time()
        if now - self._last.get(key, 0.0) >= self._s:
            self._last[key] = now
            return True
        return False


async def _bluesky_task(cfg: dict, cooldown: _Cooldown) -> None:
    """Bluesky Jetstream WebSocket でキーワードをカウントし急増を検知する。"""
    bc = cfg['bluesky']
    keywords: list[str] = cfg['keywords']
    window: float = bc['window_seconds']
    threshold: int = bc['threshold']
    sock_path: str = cfg['socket_path']
    duration: int = cfg['bubble_duration_seconds']
    url: str = bc['url']

    counts: dict[str, collections.deque] = {kw: collections.deque() for kw in keywords}

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                log.info('Bluesky Jetstream 接続完了')
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    record = (data.get('commit') or {}).get('record') or {}
                    text = record.get('text', '')
                    if not text:
                        continue
                    now = time.time()
                    for kw in keywords:
                        if kw not in text:
                            continue
                        dq = counts[kw]
                        dq.append(now)
                        while dq and dq[0] < now - window:
                            dq.popleft()
                        count = len(dq)
                        if count >= threshold and cooldown.try_acquire(kw, 'bluesky'):
                            msg = f'「{kw}」が多数投稿されています（{count}件/{int(window)}秒）'
                            log.info('[Bluesky] %s', msg)
                            _notify(sock_path, msg, 'bluesky', duration)
                            dq.clear()
        except Exception as exc:
            log.warning('Bluesky エラー（30 秒後に再接続）: %s', exc)
            await asyncio.sleep(30)


async def _rss_trends_task(cfg: dict, cooldown: _Cooldown) -> None:
    """RSS フィードと Google Trends をポーリングしてキーワードを検知する。"""
    rc = cfg['trends_rss']
    keywords: list[str] = cfg['keywords']
    sock_path: str = cfg['socket_path']
    duration: int = cfg['bubble_duration_seconds']
    interval: float = rc['poll_interval_seconds']
    feeds: list[str] = rc['rss_feeds']
    item_window: float = rc['rss_item_window_minutes'] * 60

    pytrends = None
    if rc.get('use_pytrends', False):
        try:
            from pytrends.request import TrendReq  # noqa: PLC0415
            pytrends = TrendReq(hl=rc.get('google_trends_hl', 'ja-JP'), geo='')
            log.info('pytrends 利用可能')
        except ImportError:
            log.info('pytrends 未インストール — Google Trends チェックをスキップ')

    loop = asyncio.get_event_loop()

    while True:
        now = time.time()

        # --- RSS チェック ---
        for feed_url in feeds:
            try:
                parsed = await loop.run_in_executor(None, feedparser.parse, feed_url)
                for entry in parsed.entries:
                    pub = entry.get('published_parsed')
                    if pub and (now - calendar.timegm(pub)) > item_window:
                        continue
                    body = entry.get('title', '') + ' ' + entry.get('summary', '')
                    for kw in keywords:
                        if kw in body and cooldown.try_acquire(kw, 'trends_rss'):
                            msg = f'「{kw}」がニュースに登場しています'
                            log.info('[RSS] %s (%s)', msg, feed_url)
                            _notify(sock_path, msg, 'trends_rss', duration)
            except Exception as exc:
                log.warning('RSS エラー (%s): %s', feed_url, exc)

        # --- Google Trends チェック ---
        if pytrends is not None:
            try:
                geo = rc.get('google_trends_geo', 'japan')

                def _fetch_trends() -> list[str]:
                    df = pytrends.trending_searches(pn=geo)
                    return df[0].tolist() if not df.empty else []

                trending = await loop.run_in_executor(None, _fetch_trends)
                for kw in keywords:
                    if any(kw in str(t) for t in trending) and \
                            cooldown.try_acquire(kw, 'trends_rss'):
                        msg = f'「{kw}」が Google トレンドに登場しています'
                        log.info('[Trends] %s', msg)
                        _notify(sock_path, msg, 'trends_rss', duration)
            except Exception as exc:
                log.warning('Google Trends エラー: %s', exc)

        await asyncio.sleep(interval)


async def _main_async() -> None:
    cfg = _load_config()
    cooldown = _Cooldown(cfg['cooldown_seconds'])
    tasks = []
    if cfg['bluesky'].get('enabled', True):
        tasks.append(_bluesky_task(cfg, cooldown))
    if cfg['trends_rss'].get('enabled', True):
        tasks.append(_rss_trends_task(cfg, cooldown))
    if not tasks:
        log.warning('有効な監視ソースがありません。trend_config.json を確認してください')
        return
    await asyncio.gather(*tasks)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    asyncio.run(_main_async())


if __name__ == '__main__':
    main()
