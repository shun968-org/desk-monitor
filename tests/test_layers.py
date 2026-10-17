from __future__ import annotations

import urllib.error

import pytest

from desk_monitor import layers


def test_bbox_around():
    s, w, n, e = layers.bbox_around(35.0, 139.0, 111.0)
    assert (s, n) == pytest.approx((34.0, 36.0))
    assert w < 138.0 and e > 140.0  # 経度方向は緯度に応じて広がる


def test_dem_tile_cache_fetches_once_and_handles_sea():
    calls = []

    def fetch(url):
        calls.append(url)
        if '/14/' in url:
            raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
        return b'png'

    cache = layers.DemTileCache(max_tiles=2, fetch=fetch)
    assert cache.get(12, 3635, 1617) == b'png'
    assert cache.get(12, 3635, 1617) == b'png'
    assert len(calls) == 1 and calls[0].endswith('/dem_png/12/3635/1617.png')
    assert cache.get(14, 1, 1) is None  # 海など、タイルがない
    cache.get(12, 1, 1)
    cache.get(12, 2, 2)  # 上限を超えたら古いものから捨てる
    cache.get(12, 3635, 1617)
    assert len(calls) == 5
    for bad in ((15, 0, 0), (2, 4, 0), (-1, 0, 0)):
        with pytest.raises(ValueError):
            cache.get(*bad)


def test_parse_boundaries_merges_shared_ways():
    way = lambda ref, n: {'type': 'way', 'ref': ref, 'geometry': [{'lat': 35 + i / 100, 'lon': 139.0} for i in range(n)]}
    data = {'elements': [
        {'type': 'relation', 'tags': {'admin_level': '7', 'name': '横浜市'}, 'members': [way(1, 3), way(2, 2)]},
        {'type': 'relation', 'tags': {'admin_level': '4', 'name': '神奈川県'}, 'members': [way(1, 3), {'type': 'node'}]},
        {'type': 'relation', 'tags': {'admin_level': 'x'}, 'members': [way(3, 2)]},
        {'type': 'relation', 'tags': {'admin_level': '7', 'name': '町田市'}, 'members': [way(4, 1)]},
    ]}
    lines = sorted(layers.parse_boundaries(data), key=lambda l: l['level'])
    # 共有する線は 1 本にまとめ、都道府県の方を残す。点が 1 つの線は捨てる
    assert [(l['level'], l['name'], len(l['points'])) for l in lines] == [(4, '神奈川県', 3), (7, '横浜市', 2)]


def test_boundary_store_caches(tmp_path):
    calls = []

    def fetch(query):
        calls.append(query)
        return {'elements': [{'type': 'relation', 'tags': {'admin_level': '4', 'name': '県'},
                              'members': [{'type': 'way', 'ref': 1,
                                           'geometry': [{'lat': 1, 'lon': 2}, {'lat': 3, 'lon': 4}]}]}]}

    bbox = (35.0, 139.0, 36.0, 140.0)
    store = layers.BoundaryStore(bbox, tmp_path / 'b.json', fetch=fetch)
    assert 'out geom(35.0,139.0,36.0,140.0)' in store.query()
    store.load(now=0.0)
    again = layers.BoundaryStore(bbox, tmp_path / 'b.json', fetch=fetch)
    again.load(now=100.0)
    assert len(calls) == 1 and len(again.data['lines']) == 1
    again.load(now=layers.BOUNDARY_REFRESH_SEC + 10)
    assert len(calls) == 2

    def fail(query):
        raise OSError('down')

    broken = layers.BoundaryStore(bbox, tmp_path / 'b.json', fetch=fail)
    broken.load(now=layers.BOUNDARY_REFRESH_SEC * 3)
    assert broken.data['lines'] and 'down' in broken.error  # 古いデータを使い続ける


AMEDAS_TABLE = {
    '46106': {'kjName': '横浜', 'lat': [35, 26.3], 'lon': [139, 39.1]},
    '11001': {'kjName': '宗谷岬', 'lat': [45, 31.2], 'lon': [141, 56.1]},
}
AMEDAS_OBS = {
    '46106': {'temp': [18.5, 0], 'humidity': [60, 0], 'wind': [3.2, 0], 'precipitation1h': [0.0, 5]},
    '11001': {'temp': [8.5, 0]},
    '99999': {'temp': [1.0, 0]},
}


def test_stations_in_bbox():
    stations = layers.stations_in_bbox(AMEDAS_TABLE, AMEDAS_OBS, (35.0, 139.0, 36.0, 140.0))
    assert stations == [{'name': '横浜', 'lat': 35.4383, 'lon': 139.6517, 'temp': 18.5,
                         'precip1h': None, 'wind': 3.2, 'humidity': 60}]  # 品質の悪い値は None


def test_amedas_store_refreshes_every_10_minutes():
    urls = []

    def fetch(url):
        urls.append(url)
        return AMEDAS_TABLE if url.endswith('amedastable.json') else AMEDAS_OBS

    store = layers.AmedasStore((35.0, 139.0, 36.0, 140.0), fetch=fetch,
                               fetch_text=lambda url: '2026-10-18T00:20:00+09:00')
    store.refresh(now=1000.0)
    assert store.data['time'] == '2026-10-18T00:20:00+09:00'
    assert store.data['stations'][0]['name'] == '横浜'
    assert urls[-1].endswith('/data/map/20261018002000.json')
    store.refresh(now=1300.0)
    assert len(urls) == 2  # 10 分以内は取りに行かない
    store.refresh(now=1700.0)
    assert len(urls) == 3  # 表(観測所の一覧)は 1 日ごと

    def fail(url):
        raise OSError('down')

    broken = layers.AmedasStore((0, 0, 1, 1), fetch=fail, fetch_text=fail)
    broken.refresh(now=0.0)
    assert 'down' in broken.error and broken.data['stations'] == []


def test_nowcast_times_picks_latest_observation():
    times = [{'basetime': '20261017152500', 'validtime': '20261017153000'},  # 予測
             {'basetime': '20261017152500', 'validtime': '20261017152500'},
             {'basetime': '20261017152000', 'validtime': '20261017152000'}]
    calls = []
    nowcast = layers.NowcastTimes(fetch=lambda url: calls.append(url) or times)
    assert nowcast.get(now=0.0) == {'basetime': '20261017152500', 'validtime': '20261017152500'}
    nowcast.get(now=100.0)
    assert len(calls) == 1  # 5 分以内は取りに行かない
