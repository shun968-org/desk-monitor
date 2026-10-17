"""トレンド監視デーモン。

Bluesky Jetstream と Google Trends + RSS の 2 経路でキーワードの急増を検知し、
表示プロセス(display.py)の Unix ドメインソケット経由で通知を送る。

通信プロトコル (display.py との Unix socket — 改行区切り JSON。notification.py が受ける):
  {"cmd": "notify", "text": "...", "source": "bluesky"|"trends_rss"}
通知は円形 LCD には出さず、表示プロセスがハブに中継してダッシュボードにトースト表示する。

設定ファイル: config/trend_config.json

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

import feedparser
import websockets

from desk_monitor.paths import CONFIG_DIR

log = logging.getLogger(__name__)
_CONFIG_PATH = CONFIG_DIR / 'trend_config.json'


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def _notify(sock_path: str, text: str, source: str) -> None:
    """表示プロセスの Unix ソケットに JSON 1 行を送る。接続失敗は警告のみ。"""
    payload = (
        json.dumps({'cmd': 'notify', 'text': text, 'source': source}, ensure_ascii=False)
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


class _BurstCounter:
    """キーワードごとに直近 window 秒の出現時刻を数え、しきい値以上になったものを返す。"""

    def __init__(self, keywords: list[str], window: float, threshold: int) -> None:
        self._keywords = keywords
        self._window = window
        self._threshold = threshold
        self._counts: dict[str, collections.deque] = {kw: collections.deque() for kw in keywords}

    def add(self, text: str, now: float) -> list[tuple[str, int]]:
        """text に含まれるキーワードを数え、しきい値以上の (キーワード, 件数) を返す。"""
        bursts: list[tuple[str, int]] = []
        for kw in self._keywords:
            if kw not in text:
                continue
            dq = self._counts[kw]
            dq.append(now)
            while dq and dq[0] < now - self._window:
                dq.popleft()
            if len(dq) >= self._threshold:
                bursts.append((kw, len(dq)))
        return bursts

    def reset(self, keyword: str) -> None:
        self._counts[keyword].clear()


def _post_text(raw: str | bytes) -> str:
    """Jetstream のメッセージから投稿本文を取り出す。投稿でなければ空文字。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ''
    record = (data.get('commit') or {}).get('record') or {}
    return record.get('text', '')


def _rss_matches(entries: list, keywords: list[str], now: float,
                 item_window: float) -> list[str]:
    """直近 item_window 秒の記事のタイトル・概要に登場したキーワードを、記事ごとに返す。"""
    matches: list[str] = []
    for entry in entries:
        pub = entry.get('published_parsed')
        if pub and (now - calendar.timegm(pub)) > item_window:
            continue
        body = entry.get('title', '') + ' ' + entry.get('summary', '')
        matches += [kw for kw in keywords if kw in body]
    return matches


async def _bluesky_task(cfg: dict, cooldown: _Cooldown) -> None:
    """Bluesky Jetstream WebSocket でキーワードをカウントし急増を検知する。"""
    bc = cfg['bluesky']
    keywords: list[str] = cfg['keywords']
    window: float = bc['window_seconds']
    threshold: int = bc['threshold']
    sock_path: str = cfg['socket_path']
    url: str = bc['url']

    counter = _BurstCounter(keywords, window, threshold)

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=15) as ws:
                log.info('Bluesky Jetstream 接続完了')
                async for raw in ws:
                    text = _post_text(raw)
                    if not text:
                        continue
                    for kw, count in counter.add(text, time.time()):
                        if cooldown.try_acquire(kw, 'bluesky'):
                            msg = f'「{kw}」が多数投稿されています（{count}件/{int(window)}秒）'
                            log.info('[Bluesky] %s', msg)
                            _notify(sock_path, msg, 'bluesky')
                            counter.reset(kw)
        except Exception as exc:
            log.warning('Bluesky エラー（30 秒後に再接続）: %s', exc)
            await asyncio.sleep(30)


async def _rss_trends_task(cfg: dict, cooldown: _Cooldown) -> None:
    """RSS フィードと Google Trends をポーリングしてキーワードを検知する。"""
    rc = cfg['trends_rss']
    keywords: list[str] = cfg['keywords']
    sock_path: str = cfg['socket_path']
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
                for kw in _rss_matches(parsed.entries, keywords, now, item_window):
                    if cooldown.try_acquire(kw, 'trends_rss'):
                        msg = f'「{kw}」がニュースに登場しています'
                        log.info('[RSS] %s (%s)', msg, feed_url)
                        _notify(sock_path, msg, 'trends_rss')
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
                        _notify(sock_path, msg, 'trends_rss')
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
