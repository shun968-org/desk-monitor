"""固定設置のカメラの映像に、見えている方角にある観光地を重ねる(簡易 AR。ハブで使う)。

カメラの位置・画角は設定ファイル(既定 ~/.config/desk-monitor/ar.json。リポジトリには置かない)で与え、
観光地は OpenStreetMap(Overpass API、ODbL)から fetch_radius_km の範囲を取得して保存する。
各観光地の方位と距離をダッシュボードに渡し、画面上の位置はダッシュボード(ブラウザ)で計算する
(向きをダッシュボードで動かしたとき、すぐラベルと地図の扇形を追従させるため)。

ダッシュボードで変えた向きと距離の範囲(VIEW_FIELDS)は view_path に保存し、設定ファイルより優先する。

設定ファイルの例:
    {"lat": 35.0, "lon": 139.0, "heading": 0, "pitch": 0, "roll": 0, "hfov": 53.5, "vfov": 41.4,
     "min_distance_km": 0, "max_distance_km": 20, "fetch_radius_km": 50, "max_labels": 10, "mirror": false}

  heading / pitch / roll: カメラの方位(度。北 0、東 90)・上向きの傾き・右回りの回転
  hfov / vfov: 水平・垂直の画角(度。ov5647 の全視野は約 53.5 x 41.4)
  min_distance_km / max_distance_km: ラベルを出す距離の範囲(fetch_radius_km 以内)
  mirror:  映像が左右反転しているとき true
"""
from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000.0
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
# 観光地のデータを取り直す間隔
REFRESH_SEC = 7 * 86400
ATTRIBUTION = {
    'pois': '© OpenStreetMap contributors (ODbL)',
    'tiles': '地理院タイル(国土地理院)',
}

# 観光地とみなす OpenStreetMap のタグ。Wikidata の項目があるもの(ある程度知られた所)だけに絞る
_TOURISM = 'attraction|theme_park|zoo|aquarium|museum|viewpoint|gallery'
_HISTORIC = 'castle|monument|memorial|ship'
_KIND_JA = {
    'attraction': '名所', 'theme_park': 'テーマパーク', 'zoo': '動物園', 'aquarium': '水族館',
    'museum': '博物館', 'viewpoint': '展望所', 'gallery': '美術館', 'castle': '城',
    'monument': '記念碑', 'memorial': '記念碑', 'ship': '船',
}


# ダッシュボードから変えられる項目
VIEW_FIELDS = ('heading', 'pitch', 'roll', 'min_distance_km', 'max_distance_km')


@dataclass(frozen=True)
class CameraConfig:
    lat: float
    lon: float
    heading: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    hfov: float = 53.5
    vfov: float = 41.4
    min_distance_km: float = 0.0
    max_distance_km: float = 20.0
    fetch_radius_km: float = 50.0
    max_labels: int = 10
    mirror: bool = False

    @classmethod
    def load(cls, path: Path) -> CameraConfig | None:
        """設定ファイルを読む。なければ None(AR を使わない)。"""
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data}).validated()

    def validated(self) -> CameraConfig:
        if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError(f'緯度・経度が範囲外です: {self.lat}, {self.lon}')
        if not 0 < self.hfov < 180 or not 0 < self.vfov < 180:
            raise ValueError('画角は 0〜180 度で指定してください')
        if not -90 <= self.pitch <= 90 or not -180 <= self.roll <= 180:
            raise ValueError('傾きは -90〜90 度、回転は -180〜180 度で指定してください')
        if not 0 <= self.min_distance_km < self.max_distance_km <= self.fetch_radius_km:
            raise ValueError('距離の範囲は 0 ≦ 開始 < 終了 ≦ fetch_radius_km で指定してください')
        return self

    def with_view(self, view: dict) -> CameraConfig:
        """向きと距離の範囲を差し替えた設定。値が不正なら ValueError。"""
        changes = {k: float(view[k]) for k in VIEW_FIELDS if k in view}
        if 'heading' in changes:
            changes['heading'] %= 360.0
        return replace(self, **changes).validated()

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class Poi:
    name: str
    lat: float
    lon: float
    kind: str


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2 点間の大圏距離(m)。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """地点 1 から地点 2 への方位(度。北 0、時計回り)。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def describe(config: CameraConfig, pois: list[Poi]) -> list[dict]:
    """各観光地の種類(日本語)・方位・距離。近い順。"""
    items = [{'name': p.name, 'kind': _KIND_JA.get(p.kind, p.kind), 'lat': p.lat, 'lon': p.lon,
              'distance_m': round(distance_m(config.lat, config.lon, p.lat, p.lon)),
              'bearing': round(bearing_deg(config.lat, config.lon, p.lat, p.lon), 2)}
             for p in pois]
    items.sort(key=lambda i: i['distance_m'])
    return items


def overpass_query(config: CameraConfig) -> str:
    radius = round(config.fetch_radius_km * 1000)
    around = f'(around:{radius},{config.lat},{config.lon})'
    return (f'[out:json][timeout:60];('
            f'nwr["tourism"~"^({_TOURISM})$"]["name"]["wikidata"]{around};'
            f'nwr["historic"~"^({_HISTORIC})$"]["name"]["wikidata"]{around};'
            f');out center tags;')


def parse_overpass(data: dict) -> list[Poi]:
    """Overpass API の応答から観光地を取り出す。名前は日本語名を優先する。同じ名前は 1 つにまとめる。"""
    pois: dict[str, Poi] = {}
    for el in data.get('elements', []):
        tags = el.get('tags') or {}
        point = el.get('center') or el
        name = tags.get('name:ja') or tags.get('name')
        if not name or 'lat' not in point or 'lon' not in point:
            continue
        kind = tags.get('tourism') or tags.get('historic') or ''
        pois.setdefault(name, Poi(name, float(point['lat']), float(point['lon']), kind))
    return list(pois.values())


class PoiStore:
    """観光地のデータを取得してファイルに保存し、REFRESH_SEC ごとか設定が変わったときに取り直す。"""

    def __init__(self, config: CameraConfig, cache_path: Path, fetch=None,
                 view_path: Path | None = None) -> None:
        self._base = config
        self.config = config
        self._cache_path = Path(cache_path)
        self._view_path = Path(view_path) if view_path else None
        self._load_view()
        self._fetch = fetch or self._fetch_overpass
        self.pois: list[Poi] = []
        self.updated_at = 0.0
        self.error: str | None = None

    def _load_view(self) -> None:
        """ダッシュボードで保存した向きと距離の範囲を読む。不正なら設定ファイルの値を使う。"""
        if self._view_path is None:
            return
        try:
            self.config = self._base.with_view(json.loads(self._view_path.read_text(encoding='utf-8')))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as exc:
            log.warning('保存した AR の向きを使えません: %s', exc)

    def set_view(self, view: dict) -> CameraConfig:
        """向きと距離の範囲を変えて保存する。不正なら ValueError(何も変えない)。"""
        config = self.config.with_view(view)
        self.config = config
        if self._view_path is not None:
            self._view_path.parent.mkdir(parents=True, exist_ok=True)
            self._view_path.write_text(json.dumps({k: getattr(config, k) for k in VIEW_FIELDS}),
                                       encoding='utf-8')
        return config

    def _key(self) -> dict:
        return {'lat': self.config.lat, 'lon': self.config.lon,
                'fetch_radius_km': self.config.fetch_radius_km}

    def load(self, now: float | None = None) -> None:
        """保存したデータを読み、古いか設定が変わっていれば取り直す。失敗しても古いデータは使い続ける。"""
        now = time.time() if now is None else now
        try:
            cached = json.loads(self._cache_path.read_text(encoding='utf-8'))
            self.pois = [Poi(**p) for p in cached['pois']]
            self.updated_at = float(cached['updated_at'])
            fresh = cached.get('key') == self._key() and now - self.updated_at < REFRESH_SEC
        except (OSError, ValueError, KeyError, TypeError):
            fresh = False
        if fresh:
            return
        try:
            pois = parse_overpass(self._fetch(overpass_query(self.config)))
        except Exception as exc:  # ネットワークの不調で止めない
            self.error = repr(exc)
            log.warning('観光地のデータを取得できません: %r', exc)
            return
        self.pois, self.updated_at, self.error = pois, now, None
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(
            {'key': self._key(), 'updated_at': now, 'pois': [p.__dict__ for p in pois]},
            ensure_ascii=False), encoding='utf-8')
        log.info('観光地のデータを取得しました: %d 件', len(pois))

    @staticmethod
    def _fetch_overpass(query: str) -> dict:
        req = urllib.request.Request(
            OVERPASS_URL, data=urllib.parse.urlencode({'data': query}).encode(),
            headers={'User-Agent': 'desk-monitor (https://github.com/shun968-org/desk-monitor)'})
        with urllib.request.urlopen(req, timeout=90) as res:
            return json.load(res)

    def snapshot(self) -> dict:
        return {
            'camera': self.config.to_dict(),
            'pois': describe(self.config, self.pois),
            'updated_at': self.updated_at,
            'error': self.error,
            'attribution': ATTRIBUTION,
        }
