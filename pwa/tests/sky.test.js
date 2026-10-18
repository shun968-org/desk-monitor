import assert from 'node:assert/strict';
import { test } from 'node:test';

import { blend, colorDiff, plausibleSky, skyColor, skyline, smooth } from '../sky.js';

const SKY = [120, 170, 230], CLOUD = [235, 235, 238], WALL = [90, 80, 70], WIRE = [30, 30, 30];
const LEAF = [70, 130, 60], ROOF = [150, 150, 160];
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, `${a} ≈ ${b}`);

// colorAt(x, y) から RGBA を作る
function image(width, height, colorAt) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const [r, g, b] = colorAt(x, y);
      data.set([r, g, b, 255], (y * width + x) * 4);
    }
  }
  return data;
}
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));

test('空の色として使えるのは明るく、葉の緑でない色', () => {
  assert.equal(plausibleSky(SKY), true);
  assert.equal(plausibleSky(CLOUD), true);
  assert.equal(plausibleSky([250, 160, 90]), true);  // 夕焼け
  assert.equal(plausibleSky(WALL), false);
  assert.equal(plausibleSky(LEAF), false);
});

test('色の差: 同じ色は 0、空と壁は大きい', () => {
  assert.equal(colorDiff(SKY, SKY), 0);
  assert.ok(colorDiff(SKY, WALL) > 170);
  assert.ok(colorDiff(SKY, mix(SKY, CLOUD, 0.1)) < 28);
});

test('上端の帯の色を空の色とし、暗ければ空なし', () => {
  assert.deepEqual(skyColor(image(4, 20, (x, y) => (y < 10 ? SKY : WALL)), 4, 20), SKY);
  assert.equal(skyColor(image(4, 20, () => WALL), 4, 20), null);
});

test('上半分が空なら境界は 0.5', () => {
  const data = image(10, 20, (x, y) => (y < 10 ? SKY : WALL));
  assert.deepEqual([...skyline(data, 10, 20)], Array(10).fill(0.5));
});

test('列ごとに高さの違う建物の輪郭をたどる', () => {
  const data = image(10, 20, (x, y) => (y < (x < 5 ? 4 : 12) ? SKY : WALL));
  const s = skyline(data, 10, 20);
  close(s[0], 0.2);
  close(s[9], 0.6);
});

test('地平線に向かって明るくなる空にも追従し、明るい屋根で止まる', () => {
  const data = image(6, 40, (x, y) => (y < 30 ? mix(SKY, CLOUD, y / 30) : ROOF));
  const s = skyline(data, 6, 40);
  close(s[0], 0.75);
});

test('夕焼けの空も空として扱う', () => {
  const dusk = [245, 150, 90];
  const data = image(6, 20, (x, y) => (y < 8 ? mix(dusk, [250, 190, 140], y / 8) : WALL));
  close(skyline(data, 6, 20)[0], 0.4);
});

test('1 画素の電線は飛ばし、室内(空なし)は 0', () => {
  const data = image(5, 20, (x, y) => {
    if (x === 4) return WALL;
    if (y === 3) return WIRE;
    return y < 15 ? SKY : WALL;
  });
  const s = skyline(data, 5, 20);
  close(s[0], 0.75);
  close(s[4], 0.75);  // 1 列だけの違いは中央値でならされる
  assert.deepEqual([...skyline(image(5, 20, () => WALL), 5, 20)], Array(5).fill(0));
});

test('最上段や途中の数画素の電線に引きずられない', () => {
  // 高さ 80 画素なら 2 画素(高さの 3%)まで飛ばす
  const data = image(8, 80, (x, y) => {
    if (y === 0 || y === 20 || y === 21) return WIRE;
    return y < 60 ? SKY : WALL;
  });
  close(skyline(data, 8, 80)[3], 0.75);
});

test('中央値でならす', () => {
  assert.deepEqual([...smooth(Float32Array.from([0.5, 0.5, 0.1, 0.5, 0.5]), 1)], [0.5, 0.5, 0.5, 0.5, 0.5]);
});

test('前回の境界となめらかにつなぎ、空なしの列や列数の変化では新しい値を使う', () => {
  const next = Float32Array.from([0.5, 0, 0.5]);
  const out = blend(Float32Array.from([0.3, 0.3, 0]), next);
  close(out[0], 0.3 * 0.6 + 0.5 * 0.4);
  close(out[1], 0);
  close(out[2], 0.5);
  assert.equal(blend(Float32Array.from([0.1]), next), next);
  assert.equal(blend(null, next), next);
});
