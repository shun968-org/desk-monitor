import assert from 'node:assert/strict';
import { test } from 'node:test';

import { emphasis, layoutLabels } from '../layout.js';

const byName = (list) => Object.fromEntries(list.map((l) => [l.name, l]));

test('近いものから上の段に置き、重ならなければ同じ段に並べる', () => {
  const out = byName(layoutLabels([
    { name: '遠い', x: 100, w: 80, distance_m: 9000 },
    { name: '近い', x: 110, w: 80, distance_m: 1000 },
    { name: '離れている', x: 300, w: 80, distance_m: 5000 },
  ], { width: 400, maxRows: 4 }));
  assert.equal(out['近い'].row, 0);
  assert.equal(out['遠い'].row, 1);
  assert.equal(out['離れている'].row, 0);
});

test('描く順は遠い順(近いものが最後 = 前面)', () => {
  const out = layoutLabels([
    { name: 'a', x: 100, w: 50, distance_m: 2000 },
    { name: 'b', x: 100, w: 50, distance_m: 500 },
    { name: 'c', x: 100, w: 50, distance_m: 8000 },
  ], { width: 400, maxRows: 4 });
  assert.deepEqual(out.map((l) => l.name), ['c', 'a', 'b']);
});

test('段が足りなければ一番下の段に重ねる', () => {
  const items = [1, 2, 3].map((i) => ({ name: String(i), x: 100, w: 50, distance_m: i * 1000 }));
  const out = byName(layoutLabels(items, { width: 400, maxRows: 2 }));
  assert.equal(out['1'].row, 0);
  assert.equal(out['2'].row, 1);
  assert.equal(out['3'].row, 1);
});

test('画面の端ではみ出さないよう寄せる', () => {
  const [left] = layoutLabels([{ x: 5, w: 100, distance_m: 1 }], { width: 400, maxRows: 1 });
  const [right] = layoutLabels([{ x: 398, w: 100, distance_m: 1 }], { width: 400, maxRows: 1 });
  assert.equal(left.lx, 4);
  assert.equal(right.lx, 296);
});

test('近いほど大きく濃い', () => {
  assert.deepEqual(emphasis(0, 0, 20000), { scale: 1.15, alpha: 1 });
  const far = emphasis(20000, 0, 20000);
  assert.ok(Math.abs(far.scale - 0.85) < 1e-9 && Math.abs(far.alpha - 0.6) < 1e-9);
  assert.deepEqual(emphasis(99999, 0, 20000), far);
});
