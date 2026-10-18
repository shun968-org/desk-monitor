import assert from 'node:assert/strict';
import { test } from 'node:test';

import { browserOf, bucketIndex, createMetrics, MAX_SAMPLES, METRICS_KEY } from '../metrics.js';

const memoryStorage = () => {
  const m = new Map();
  return {
    getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v), removeItem: (k) => m.delete(k),
  };
};

test('コンパスの精度の区切り', () => {
  assert.equal(bucketIndex(3), 0);
  assert.equal(bucketIndex(10), 1);
  assert.equal(bucketIndex(25), 3);
  assert.equal(bucketIndex(45), 4);
});

test('ブラウザの種類', () => {
  const ios = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)';
  assert.equal(browserOf(`${ios} Version/18.0 Mobile/15E148 Safari/604.1`), 'Safari(iOS)');
  assert.equal(browserOf(`${ios} CriOS/130.0 Mobile/15E148 Safari/604.1`), 'Chrome(iOS)');
  assert.equal(browserOf(`${ios} GSA/340.0 Mobile/15E148 Safari/604.1`), 'Google アプリ(iOS)');
  assert.equal(browserOf('Mozilla/5.0 (Linux; Android 15) Chrome/130.0 Mobile Safari/537.36'), 'Chrome(Android)');
});

test('数え上げ・分布・値の並びを記録し、保存して読み戻せる', () => {
  const storage = memoryStorage();
  const m = createMetrics({ storage, now: () => 1000 });
  m.inc('start'); m.inc('start');
  m.compass(8); m.compass(40);
  m.sample('offset', -12.34); m.sample('offset', 5);
  m.sky(true); m.sky(false);
  m.addDuration(125);
  m.env({ browser: 'Safari(iOS)' });
  m.flush();
  const again = createMetrics({ storage, now: () => 2000 });
  assert.equal(again.data.counts.start, 2);
  assert.deepEqual(again.data.compass, [0, 1, 0, 0, 1]);
  assert.deepEqual(again.data.samples.offset, [5, -12.3]);
  const s = again.summary('v6');
  assert.equal(s.app, 'v6');
  assert.equal(s.manualOffset.medianAbs, 12.3);
  assert.equal(s.skyFoundRatio, 0.5);
  assert.equal(s.usedMinutes, 2);
  assert.equal(s.compassAccuracy['±10°以下'], 1);
  assert.equal(s.env.browser, 'Safari(iOS)');
});

test('値の並びは新しい順に上限まで', () => {
  const m = createMetrics({ storage: memoryStorage() });
  for (let i = 0; i < MAX_SAMPLES + 5; i++) m.sample('fetchSec', i);
  assert.equal(m.data.samples.fetchSec.length, MAX_SAMPLES);
  assert.equal(m.data.samples.fetchSec[0], MAX_SAMPLES + 4);
});

test('記録を止めると何も増えず、消すと空になる', () => {
  const storage = memoryStorage();
  const m = createMetrics({ storage, enabled: false });
  m.inc('start'); m.compass(5); m.sample('offset', 1); m.sky(true);
  assert.deepEqual(m.data.counts, {});
  m.setEnabled(true);
  m.inc('start'); m.flush();
  assert.ok(storage.getItem(METRICS_KEY));
  m.reset();
  assert.equal(storage.getItem(METRICS_KEY), null);
  assert.deepEqual(m.data.counts, {});
});

test('壊れた保存データは捨てて新しく始める', () => {
  const storage = memoryStorage();
  storage.setItem(METRICS_KEY, '{');
  assert.deepEqual(createMetrics({ storage }).data.counts, {});
});

test('位置に当たる項目は持たない', () => {
  const m = createMetrics({ storage: memoryStorage() });
  const keys = JSON.stringify(m.summary('v')).toLowerCase();
  assert.ok(!/lat|lon|position|location/.test(keys));
});
