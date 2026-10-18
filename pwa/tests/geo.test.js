// 実行: node --test pwa/tests/
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  angleDiff, compassReference, coverTransform, declinationJapan, deviceBasis, fovOf, headingOf,
  pitchOf, projectDir, rotateBasis, trueHeading,
} from '../geo.js';

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} ≈ ${b}`);
const closeVec = (a, b, eps = 1e-6) => a.forEach((x, i) => close(x, b[i], eps));

test('立てた端末(alpha 0)の背面カメラは北を向き、右は東・上は天頂', () => {
  const b = deviceBasis(0, 90, 0);
  closeVec(b.f, [0, 1, 0]);
  closeVec(b.right, [1, 0, 0]);
  closeVec(b.up, [0, 0, 1]);
  close(headingOf(b.f), 0);
  close(pitchOf(b.f), 0);
});

test('alpha は反時計回り: alpha 90 で西を向く', () => {
  close(headingOf(deviceBasis(90, 90, 0).f), 270);
});

test('上に傾けると仰角が付き、向きは変わらない', () => {
  const b = deviceBasis(0, 120, 0);
  close(pitchOf(b.f), 30);
  close(headingOf(b.f), 0);
});

test('横持ち(画面の回転 90°)では端末の右側が上になる', () => {
  // 上端を北に向けて寝かせた端末を、右側が上になるよう起こすと背面は東を向く
  const b = deviceBasis(0, 0, -90, 90);
  closeVec(b.up, [0, 0, 1]);
  closeVec(b.right, [0, -1, 0]);
  close(headingOf(b.f), 90);
});

test('コンパスの基準は寝かせると上端、立てると背面で、傾けるだけなら同じ向き', () => {
  close(headingOf(compassReference(30, 10, 0)), 330);
  close(headingOf(compassReference(30, 80, 0)), 330);
});

test('座標軸を回すと向きが時計回りに増える', () => {
  close(headingOf(rotateBasis(deviceBasis(0, 90, 0), 90).f), 90);
});

test('正面は中央、右の方角は右、後ろは null', () => {
  const b = deviceBasis(0, 90, 0), fov = { hfov: 60, vfov: 45 };
  const c = projectDir(0, b, fov);
  close(c.x, 0.5); close(c.y, 0.5);
  close(projectDir(30, b, fov).x, 1);   // 画角の右端
  assert.ok(projectDir(-10, b, fov).x < 0.5);
  assert.equal(projectDir(180, b, fov), null);
  assert.ok(projectDir(0, b, fov, 10).y < 0.5);  // 仰角があれば上
});

test('画角: 長辺の画角から短辺を求め、縦長なら縦が長辺', () => {
  const land = fovOf(67, 1920, 1440);
  close(land.hfov, 67);
  close(Math.tan(land.vfov * Math.PI / 360), Math.tan(67 * Math.PI / 360) * 0.75);
  const port = fovOf(67, 1440, 1920);
  close(port.vfov, 67); close(port.hfov, land.vfov);
});

test('偏角は日本で西に 5〜10° 程度、真北への変換は磁北の値から引く', () => {
  close(declinationJapan(35.7, 139.7), 7.5);
  assert.ok(declinationJapan(43.06, 141.35) > 9 && declinationJapan(43.06, 141.35) < 10);
  assert.ok(declinationJapan(26.2, 127.7) > 4 && declinationJapan(26.2, 127.7) < 5.5);
  close(trueHeading(3, 35.7, 139.7), 355.5);
});

test('最短の回転量', () => {
  close(angleDiff(10, 350), 20);
  close(angleDiff(350, 10), -20);
});

test('cover の表示: 縦長の枠に横長の映像を入れると左右が切れる', () => {
  const t = coverTransform(1600, 1200, 300, 600);
  const left = t.toPx({ x: 0, y: 0 }), right = t.toPx({ x: 1, y: 1 });
  close(right.y - left.y, 600);
  close(right.x - left.x, 800);
  close(t.toPx({ x: 0.5, y: 0.5 }).x, 150);
  close(t.visibleX, 300 / 800);
});
