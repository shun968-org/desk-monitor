import assert from 'node:assert/strict';
import { test } from 'node:test';

import { distanceM, fetchPois, isStale, loadCache, overpassQuery, parseOverpass, REFRESH_MS } from '../poi.js';

const memoryStorage = () => {
  const m = new Map();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v) };
};

test('問い合わせは ar.py と同じ条件で、半径を m にする', () => {
  const q = overpassQuery(35.1234567, 139.5, 30);
  assert.match(q, /around:30000,35\.12346,139\.50000/);
  assert.match(q, /"tourism"~"\^\(attraction\|/);
  assert.match(q, /\["wikidata"\]/);
});

test('日本語名を優先し、同じ名前は 1 つにまとめ、座標のないものは除く', () => {
  const pois = parseOverpass({
    elements: [
      { type: 'node', lat: 35, lon: 139, tags: { name: 'Castle', 'name:ja': '城跡', historic: 'castle', wikidata: 'Q1' } },
      { type: 'way', center: { lat: 35.1, lon: 139.1 }, tags: { name: '城跡', tourism: 'museum' } },
      { type: 'way', center: { lat: 35.2, lon: 139.2 }, tags: { name: '美術館', tourism: 'gallery' } },
      { type: 'relation', tags: { name: '座標なし', tourism: 'zoo' } },
    ],
  });
  assert.deepEqual(pois.map((p) => [p.name, p.kind]), [['城跡', '城'], ['美術館', '美術館']]);
});

test('距離: 緯度 1° は約 111km', () => {
  assert.ok(Math.abs(distanceM(35, 139, 36, 139) - 111195) < 100);
});

test('取り直すのは、ない・古い・半径が違う・中心から離れたとき', () => {
  const now = 1_000_000_000_000;
  const cache = { lat: 35, lon: 139, radius_km: 30, updated_at: now };
  assert.equal(isStale(null, 35, 139, 30, now), true);
  assert.equal(isStale(cache, 35, 139, 30, now), false);
  assert.equal(isStale(cache, 35.05, 139, 30, now), false);     // 約 5.6km
  assert.equal(isStale(cache, 35.1, 139, 30, now), true);       // 約 11km(半径の 3 割を超える)
  assert.equal(isStale(cache, 35, 139, 50, now), true);
  assert.equal(isStale(cache, 35, 139, 30, now + REFRESH_MS + 1), true);
});

test('取得したデータを保存し、読み戻せる', async () => {
  const storage = memoryStorage();
  const fetch = async (url, init) => {
    assert.equal(init.method, 'POST');
    assert.match(init.body, /^data=/);
    return { ok: true, json: async () => ({ elements: [{ lat: 35, lon: 139, tags: { name: '展望台', tourism: 'viewpoint' } }] }) };
  };
  const cache = await fetchPois(35, 139, 30, { fetch, storage });
  assert.equal(cache.pois[0].kind, '展望所');
  assert.deepEqual(loadCache(storage).pois, cache.pois);
});

test('取得に失敗したら例外にする', async () => {
  const fetch = async () => ({ ok: false, status: 429 });
  await assert.rejects(fetchPois(35, 139, 30, { fetch, storage: memoryStorage() }), /HTTP 429/);
});

test('保存がなければ null', () => {
  assert.equal(loadCache(memoryStorage()), null);
});
