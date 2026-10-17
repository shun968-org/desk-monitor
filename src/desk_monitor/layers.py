"""ダッシュボードの地図に重ねるレイヤのデータ(ハブで取得してキャッシュし、ブラウザに中継する)。

ブラウザから直接読めないもの・毎回取りに行きたくないものだけをここで扱う。
地理院タイル(画像として表示するだけのもの)はブラウザが直接読む。

  標高タイル     : 国土地理院の dem_png。ブラウザで等高線を描くのに画素を読むが、地理院タイルは
                   CORS のヘッダがないため中継する(メモリにキャッシュ)
  境界           : OpenStreetMap(Overpass API、ODbL)の都道府県(admin_level 4)・市区町村(7)の境界。
                   設置場所の周辺だけを切り出し、ファイルに保存して 30 日ごとに取り直す
  気温           : 気象庁のアメダス(10 分ごと)。周辺の観測所だけを返す
  雨雲           : 気象庁の降水ナウキャストの最新の時刻(5 分ごと)。タイル自体はブラウザが直接読む

気象庁のデータは政府標準利用規約に従い、出典を表示して使う。
"""
from __future__ import annotations

import collections
import datetime
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

USER_AGENT = 'desk-monitor (https://github.com/shun968-org/desk-monitor)'
GSI_DEM_URL = 'https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png'
DEM_MAX_ZOOM = 14
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
AMEDAS_BASE = 'https://www.jma.go.jp/bosai/amedas'
NOWCAST_TIMES_URL = 'https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json'

BOUNDARY_REFRESH_SEC = 30 * 86400
AMEDAS_REFRESH_SEC = 600
NOWCAST_REFRESH_SEC = 300


def _get(url: str, timeout: float = 30, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def bbox_around(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """中心から radius_km の正方形の範囲 (南, 西, 北, 東)。"""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


class DemTileCache:
    """地理院の標高タイル(dem_png)を中継する。よく使うタイルはメモリに残す。"""

    def __init__(self, max_tiles: int = 512, fetch=None) -> None:
        self._fetch = fetch or (lambda url: _get(url, timeout=15))
        self._tiles: collections.OrderedDict[tuple[int, int, int], bytes | None] = collections.OrderedDict()
        self._max = max_tiles
        self._lock = threading.Lock()

    def get(self, z: int, x: int, y: int) -> bytes | None:
        """タイルの PNG。範囲外や海など、タイルがなければ None。"""
        if not (0 <= z <= DEM_MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError(f'範囲外のタイル: {z}/{x}/{y}')
        key = (z, x, y)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
        try:
            data = self._fetch(GSI_DEM_URL.format(z=z, x=x, y=y))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            data = None  # 海など、標高のない所
        with self._lock:
            self._tiles[key] = data
            while len(self._tiles) > self._max:
                self._tiles.popitem(last=False)
        return data


class BoundaryStore:
    """都道府県・市区町村の境界線。設置場所の周辺だけを取得してファイルに保存する。"""

    def __init__(self, bbox: tuple[float, float, float, float], cache_path: Path, fetch=None) -> None:
        self._bbox = tuple(round(v, 4) for v in bbox)
        self._cache_path = Path(cache_path)
        self._fetch = fetch or self._fetch_overpass
        self.data: dict = {'lines': []}
        self.error: str | None = None

    def query(self) -> str:
        s, w, n, e = self._bbox
        return (f'[out:json][timeout:90];'
                f'rel["boundary"="administrative"]["admin_level"~"^(4|7)$"]({s},{w},{n},{e});'
                f'out geom({s},{w},{n},{e});')

    def load(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            cached = json.loads(self._cache_path.read_text(encoding='utf-8'))
            if cached.get('bbox') == list(self._bbox) and now - cached['updated_at'] < BOUNDARY_REFRESH_SEC:
                self.data = cached
                return
            self.data = cached  # 古くても、取り直せるまでは使う
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            lines = parse_boundaries(self._fetch(self.query()))
        except Exception as exc:  # ネットワークの不調で止めない
            self.error = repr(exc)
            log.warning('境界のデータを取得できません: %r', exc)
            return
        self.data = {'bbox': list(self._bbox), 'updated_at': now, 'lines': lines}
        self.error = None
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        log.info('境界のデータを取得しました: %d 本', len(lines))

    @staticmethod
    def _fetch_overpass(query: str) -> dict:
        return json.loads(_get(OVERPASS_URL, timeout=120,
                               data=urllib.parse.urlencode({'data': query}).encode()))


def parse_boundaries(data: dict) -> list[dict]:
    """Overpass の応答(out geom)から、境界の線を {level, name, points: [[緯度, 経度], ...]} で返す。

    同じ線(隣り合う市区町村が共有する部分)は 1 本にまとめ、レベルの小さい方(都道府県)を残す。
    """
    lines: dict[tuple, dict] = {}
    for rel in data.get('elements', []):
        tags = rel.get('tags') or {}
        try:
            level = int(tags.get('admin_level', ''))
        except ValueError:
            continue
        name = tags.get('name:ja') or tags.get('name') or ''
        for member in rel.get('members', []):
            geometry = [g for g in member.get('geometry') or [] if g and 'lat' in g]
            if member.get('type') != 'way' or len(geometry) < 2:
                continue
            points = [[round(g['lat'], 5), round(g['lon'], 5)] for g in geometry]
            key = (member.get('ref'), len(points))
            if key not in lines or level < lines[key]['level']:
                lines[key] = {'level': level, 'name': name, 'points': points}
    return list(lines.values())


class AmedasStore:
    """アメダスの最新の観測値(気温・降水量・風)のうち、範囲内の観測所の分。"""

    def __init__(self, bbox: tuple[float, float, float, float], fetch=None, fetch_text=None) -> None:
        self._bbox = bbox
        self._fetch = fetch or (lambda url: json.loads(_get(url)))
        self._fetch_text = fetch_text or (lambda url: _get(url).decode().strip())
        self._table: dict | None = None
        self._table_at = 0.0
        self.data: dict = {'time': None, 'stations': []}
        self.updated_at = float('-inf')  # まだ一度も取得していない
        self.error: str | None = None

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self.updated_at < AMEDAS_REFRESH_SEC:
            return
        try:
            if self._table is None or now - self._table_at > 86400:
                self._table = self._fetch(f'{AMEDAS_BASE}/const/amedastable.json')
                self._table_at = now
            latest = self._fetch_text(f'{AMEDAS_BASE}/data/latest_time.txt')
            stamp = datetime.datetime.fromisoformat(latest).strftime('%Y%m%d%H%M%S')
            observed = self._fetch(f'{AMEDAS_BASE}/data/map/{stamp}.json')
        except Exception as exc:
            self.error = repr(exc)
            log.warning('アメダスのデータを取得できません: %r', exc)
            return
        self.data = {'time': latest, 'stations': stations_in_bbox(self._table, observed, self._bbox)}
        self.updated_at, self.error = now, None


def _degrees(value: list) -> float:
    """アメダスの表の [度, 分] を度にする。"""
    return value[0] + value[1] / 60


def stations_in_bbox(table: dict, observed: dict, bbox: tuple[float, float, float, float]) -> list[dict]:
    """範囲内の観測所の名前・位置と観測値。品質の悪い値(フラグ 0 以外)は None にする。"""
    s, w, n, e = bbox
    out = []
    for code, obs in observed.items():
        info = table.get(code)
        if not info:
            continue
        lat, lon = _degrees(info['lat']), _degrees(info['lon'])
        if not (s <= lat <= n and w <= lon <= e):
            continue

        def value(key):
            v = obs.get(key)
            return v[0] if isinstance(v, list) and len(v) == 2 and v[1] == 0 else None

        out.append({'name': info.get('kjName', code), 'lat': round(lat, 4), 'lon': round(lon, 4),
                    'temp': value('temp'), 'precip1h': value('precipitation1h'), 'wind': value('wind'),
                    'humidity': value('humidity')})
    return out


class NowcastTimes:
    """気象庁の降水ナウキャストの最新の時刻(タイルの URL に使う)。"""

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or (lambda url: json.loads(_get(url, timeout=15)))
        self.latest: dict | None = None
        self.updated_at = float('-inf')  # まだ一度も取得していない

    def get(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if now - self.updated_at >= NOWCAST_REFRESH_SEC:
            try:
                times = self._fetch(NOWCAST_TIMES_URL)
                first = max(times, key=lambda t: t['validtime'] if t['basetime'] == t['validtime'] else '')
                self.latest = {'basetime': first['basetime'], 'validtime': first['validtime']}
                self.updated_at = now
            except Exception as exc:
                log.warning('雨雲の時刻を取得できません: %r', exc)
        return self.latest


class MapLayers:
    """地図のレイヤのデータをまとめて持つ(ハブが 1 つ作る)。"""

    ATTRIBUTION = {
        'gsi': '地理院タイル(国土地理院)',
        'osm': '© OpenStreetMap contributors (ODbL)',
        'jma': '気象庁(アメダス・降水ナウキャスト)',
    }

    def __init__(self, lat: float, lon: float, radius_km: float, data_dir: Path) -> None:
        bbox = bbox_around(lat, lon, radius_km)
        self.dem = DemTileCache()
        self.boundaries = BoundaryStore(bbox, Path(data_dir) / 'boundaries.json')
        self.amedas = AmedasStore(bbox)
        self.nowcast = NowcastTimes()

    def keep_updated(self) -> None:
        """境界は起動時と 1 日ごとに(古ければ取り直す)、アメダスは 10 分ごとに更新する。"""
        last_boundary = 0.0
        while True:
            now = time.time()
            if now - last_boundary > 86400 or (self.boundaries.error and now - last_boundary > 600):
                self.boundaries.load(now)
                last_boundary = now
            self.amedas.refresh(now)
            time.sleep(60)
