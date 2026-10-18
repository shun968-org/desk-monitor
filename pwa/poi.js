// 観光地のデータ: OpenStreetMap(Overpass API、ODbL)から現在地の周辺を取得し、端末(localStorage)に保存する。
// 取得の条件は ar.py と同じ(Wikidata の項目がある、ある程度知られた所だけ)

export const OVERPASS_URL = 'https://overpass-api.de/api/interpreter';
export const ATTRIBUTION = '© OpenStreetMap contributors (ODbL)';
const TOURISM = 'attraction|theme_park|zoo|aquarium|museum|viewpoint|gallery';
const HISTORIC = 'castle|monument|memorial|ship';
const KIND_JA = {
  attraction: '名所', theme_park: 'テーマパーク', zoo: '動物園', aquarium: '水族館',
  museum: '博物館', viewpoint: '展望所', gallery: '美術館', castle: '城',
  monument: '記念碑', memorial: '記念碑', ship: '船',
};
// 取り直す間隔と、取得した中心からこれだけ(半径に対する割合)離れたら取り直す
export const REFRESH_MS = 7 * 86400 * 1000;
export const MOVE_RATIO = 0.3;
const STORAGE_KEY = 'desk-monitor-ar:pois';

// 外部に送る中心の座標は約 1km 単位(小数点以下 2 桁)に丸める。取得範囲は数十 km なので精度は要らない
export function overpassQuery(lat, lon, radiusKm) {
  const around = `(around:${Math.round(radiusKm * 1000)},${lat.toFixed(2)},${lon.toFixed(2)})`;
  return `[out:json][timeout:60];(`
    + `nwr["tourism"~"^(${TOURISM})$"]["name"]["wikidata"]${around};`
    + `nwr["historic"~"^(${HISTORIC})$"]["name"]["wikidata"]${around};`
    + ');out center tags;';
}

// Overpass API の応答から観光地を取り出す。名前は日本語名を優先し、同じ名前は 1 つにまとめる
export function parseOverpass(data) {
  const pois = new Map();
  for (const el of data?.elements ?? []) {
    const tags = el.tags ?? {};
    const point = el.center ?? el;
    const name = tags['name:ja'] || tags.name;
    if (!name || typeof point.lat !== 'number' || typeof point.lon !== 'number' || pois.has(name)) continue;
    const kind = tags.tourism || tags.historic || '';
    pois.set(name, { name, lat: point.lat, lon: point.lon, kind: KIND_JA[kind] ?? kind, wikidata: tags.wikidata ?? null });
  }
  return [...pois.values()];
}

// 2 点間の距離(m)。数十 km までなら球面の近似で足りる
export function distanceM(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180, R = 6371008.8;
  const a = Math.sin((lat2 - lat1) * r / 2) ** 2
    + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin((lon2 - lon1) * r / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
}

// 保存済みのデータを取り直すべきか
export function isStale(cache, lat, lon, radiusKm, now = Date.now()) {
  if (!cache || cache.radius_km !== radiusKm || now - cache.updated_at > REFRESH_MS) return true;
  return distanceM(cache.lat, cache.lon, lat, lon) > radiusKm * 1000 * MOVE_RATIO;
}

export function loadCache(storage = globalThis.localStorage) {
  try {
    return JSON.parse(storage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

// 公開の Overpass API は混んでいると 1 分ほどかかるので、2 分で打ち切る
export const FETCH_TIMEOUT_MS = 120_000;

export async function fetchPois(lat, lon, radiusKm, { fetch = globalThis.fetch, storage = globalThis.localStorage } = {}) {
  let res;
  try {
    res = await fetch(OVERPASS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'data=' + encodeURIComponent(overpassQuery(lat, lon, radiusKm)),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
  } catch (err) {
    if (err?.name === 'TimeoutError') throw new Error(`Overpass API が ${FETCH_TIMEOUT_MS / 1000} 秒応答しません`);
    throw err;
  }
  if (!res.ok) throw new Error(`Overpass API: HTTP ${res.status}`);
  const cache = { lat, lon, radius_km: radiusKm, updated_at: Date.now(), pois: parseOverpass(await res.json()) };
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(cache));
  } catch { /* 保存できなくても表示はする(プライベートブラウズなど) */ }
  return cache;
}
