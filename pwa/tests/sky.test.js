import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isSky, skyline, smooth } from '../sky.js';

const SKY = [120, 170, 230], CLOUD = [235, 235, 238], WALL = [90, 80, 70], WIRE = [30, 30, 30];

// rows[y](各行の色)か、cols[x][y](列ごとの色)から RGBA を作る
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

test('青空と曇り空は空、壁や暗い色は空でない', () => {
  assert.equal(isSky(...SKY), true);
  assert.equal(isSky(...CLOUD), true);
  assert.equal(isSky(...WALL), false);
  assert.equal(isSky(...WIRE), false);
});

test('上半分が空なら境界は 0.5', () => {
  const data = image(10, 20, (x, y) => (y < 10 ? SKY : WALL));
  assert.deepEqual([...skyline(data, 10, 20)], Array(10).fill(0.5));
});

test('列ごとに高さの違う建物の輪郭をたどる', () => {
  const data = image(10, 20, (x, y) => (y < (x < 5 ? 4 : 12) ? SKY : WALL));
  const s = skyline(data, 10, 20);
  assert.ok(Math.abs(s[0] - 0.2) < 1e-6);
  assert.ok(Math.abs(s[9] - 0.6) < 1e-6);
});

test('1 画素の電線は飛ばし、上端が空でない列は 0', () => {
  const data = image(5, 20, (x, y) => {
    if (x === 4) return WALL;
    if (y === 3) return WIRE;
    return y < 15 ? SKY : WALL;
  });
  const s = skyline(data, 5, 20);
  assert.equal(s[0], 0.75);
  assert.equal(s[4], 0.75);  // 1 列だけの違いは中央値でならされる
  const indoor = image(5, 20, () => WALL);
  assert.deepEqual([...skyline(indoor, 5, 20)], Array(5).fill(0));
});

test('中央値でならす', () => {
  assert.deepEqual([...smooth(Float32Array.from([0.5, 0.5, 0.1, 0.5, 0.5]), 1)], [0.5, 0.5, 0.5, 0.5, 0.5]);
});
