from __future__ import annotations

import json

import pytest

from desk_monitor import ar

# 横浜駅付近と、そこから見た既知の地点
YOKOHAMA = (35.4658, 139.6223)


def test_distance_and_bearing():
    # 東京駅まで約 27km、ほぼ北東
    d = ar.distance_m(*YOKOHAMA, 35.6812, 139.7671)
    assert 26_500 < d < 27_500
    assert 25 < ar.bearing_deg(*YOKOHAMA, 35.6812, 139.7671) < 35
    assert ar.bearing_deg(0, 0, 1, 0) == pytest.approx(0)      # 北
    assert ar.bearing_deg(0, 0, 0, 1) == pytest.approx(90)     # 東
    assert ar.bearing_deg(0, 0, -1, 0) == pytest.approx(180)   # 南


def _config(**kw):
    return ar.CameraConfig(lat=0.0, lon=0.0, **{'hfov': 60.0, 'max_distance_km': 50,
                                                 'fetch_radius_km': 100, **kw})


def test_describe_sorts_by_distance_with_bearing():
    north = ar.Poi('北', 0.1, 0.0, 'museum')
    east = ar.Poi('東', 0.0, 0.05, 'zoo')
    items = ar.describe(_config(), [north, east])
    assert [i['name'] for i in items] == ['東', '北']
    assert items[0]['bearing'] == pytest.approx(90) and items[1]['bearing'] == pytest.approx(0)
    assert items[1]['kind'] == '博物館' and 11_000 < items[1]['distance_m'] < 11_200


def test_with_view_validates_and_normalizes():
    config = _config()
    view = config.with_view({'heading': 370, 'pitch': 5, 'roll': -3,
                             'min_distance_km': 2, 'max_distance_km': 30, 'lat': 99})
    assert (view.heading, view.pitch, view.roll) == (10, 5, -3)
    assert (view.min_distance_km, view.max_distance_km) == (2, 30)
    assert view.lat == 0.0  # 位置などはダッシュボードから変えられない
    for bad in ({'min_distance_km': 30, 'max_distance_km': 10}, {'max_distance_km': 200},
                {'pitch': 120}, {'heading': 'x'}):
        with pytest.raises((ValueError, TypeError)):
            config.with_view(bad)


def test_config_load(tmp_path):
    assert ar.CameraConfig.load(tmp_path / 'missing.json') is None
    path = tmp_path / 'ar.json'
    path.write_text(json.dumps({'lat': 35.4, 'lon': 139.5, 'heading': 0, 'unknown': 1}))
    config = ar.CameraConfig.load(path)
    assert (config.lat, config.lon, config.hfov) == (35.4, 139.5, 53.5)
    path.write_text(json.dumps({'lat': 135, 'lon': 0}))
    with pytest.raises(ValueError):
        ar.CameraConfig.load(path)


def test_parse_overpass_prefers_japanese_names():
    data = {'elements': [
        {'type': 'node', 'lat': 35.5, 'lon': 139.6, 'tags': {'name': 'Zoorasia', 'name:ja': 'ズーラシア', 'tourism': 'zoo'}},
        {'type': 'way', 'center': {'lat': 35.51, 'lon': 139.61}, 'tags': {'name': '小机城址', 'historic': 'castle'}},
        {'type': 'way', 'center': {'lat': 35.52, 'lon': 139.62}, 'tags': {'name': '小机城址', 'historic': 'castle'}},
        {'type': 'node', 'lat': 35.5, 'lon': 139.6, 'tags': {'tourism': 'museum'}},  # 名前なし
        {'type': 'relation', 'tags': {'name': '座標なし'}},
    ]}
    pois = ar.parse_overpass(data)
    assert [(p.name, p.kind) for p in pois] == [('ズーラシア', 'zoo'), ('小机城址', 'castle')]


def test_overpass_query_uses_location_and_radius():
    q = ar.overpass_query(ar.CameraConfig(lat=35.38, lon=139.54, fetch_radius_km=50))
    assert 'around:50000,35.38,139.54' in q and '["wikidata"]' in q


def test_poi_store_caches_and_refreshes(tmp_path):
    calls = []

    def fetch(query):
        calls.append(query)
        return {'elements': [{'lat': 0.1, 'lon': 0.0, 'tags': {'name': '北の名所', 'tourism': 'attraction'}}]}

    cache = tmp_path / 'pois.json'
    store = ar.PoiStore(_config(), cache, fetch=fetch)
    store.load(now=1000.0)
    assert len(calls) == 1 and cache.exists()
    snap = store.snapshot()
    assert snap['pois'][0]['name'] == '北の名所' and snap['pois'][0]['bearing'] == pytest.approx(0)
    assert snap['camera']['heading'] == 0 and snap['attribution']['pois'].startswith('©')

    again = ar.PoiStore(_config(), cache, fetch=fetch)
    again.load(now=2000.0)
    assert len(calls) == 1 and again.pois == store.pois   # 新しければ保存したデータを使う
    again.load(now=1000.0 + ar.REFRESH_SEC + 1)
    assert len(calls) == 2                                 # 古くなったら取り直す
    narrowed = ar.PoiStore(_config(max_distance_km=10), cache, fetch=fetch)
    narrowed.load(now=2000.0)
    assert len(calls) == 2                                 # 表示の範囲を変えただけなら取り直さない
    moved = ar.PoiStore(_config(fetch_radius_km=60), cache, fetch=fetch)
    moved.load(now=2000.0)
    assert len(calls) == 3                                 # 取得の範囲が変わったら取り直す


def test_poi_store_keeps_old_data_on_failure(tmp_path):
    cache = tmp_path / 'pois.json'
    ar.PoiStore(_config(), cache, fetch=lambda q: {'elements': [
        {'lat': 0.1, 'lon': 0.0, 'tags': {'name': 'x', 'tourism': 'museum'}}]}).load(now=0.0)

    def fail(query):
        raise OSError('network down')

    store = ar.PoiStore(_config(), cache, fetch=fail)
    store.load(now=ar.REFRESH_SEC + 10)
    assert [p.name for p in store.pois] == ['x'] and 'network down' in store.error


def test_view_is_saved_and_restored(tmp_path):
    view_path = tmp_path / 'view.json'
    store = ar.PoiStore(_config(), tmp_path / 'pois.json', fetch=lambda q: {}, view_path=view_path)
    store.set_view({'heading': 45, 'min_distance_km': 5, 'max_distance_km': 15})
    with pytest.raises(ValueError):
        store.set_view({'min_distance_km': 20, 'max_distance_km': 10})
    assert store.config.heading == 45  # 不正な値では変えない
    again = ar.PoiStore(_config(), tmp_path / 'pois.json', fetch=lambda q: {}, view_path=view_path)
    assert (again.config.heading, again.config.min_distance_km, again.config.max_distance_km) == (45, 5, 15)
    view_path.write_text('{"min_distance_km": 50, "max_distance_km": 1}')
    broken = ar.PoiStore(_config(), tmp_path / 'pois.json', fetch=lambda q: {}, view_path=view_path)
    assert broken.config.heading == 0  # 保存した値が不正なら設定ファイルの値
