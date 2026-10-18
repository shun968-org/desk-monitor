// 端末の姿勢から、カメラの向き(東・北・上の座標)と観光地の画面上の位置を求める。
// ダッシュボード(hub.py)の cameraBasis / projectDir と同じ考え方で、固定の向きの代わりに端末のセンサーを使う。

const rad = (d) => d * Math.PI / 180;
const deg = (r) => r * 180 / Math.PI;
export const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

// 角度を 0〜360 に
export const norm360 = (a) => ((a % 360) + 360) % 360;
// a から b への最短の回転量(-180〜180)
export const angleDiff = (b, a) => norm360(b - a + 180) - 180;

// DeviceOrientation の alpha / beta / gamma(度)から、端末の座標(右・上・画面の手前)を
// 地上の座標(東・北・上)に移す回転行列。W3C の定義(Z-X'-Y'')どおり
export function rotationMatrix(alpha, beta, gamma) {
  const cA = Math.cos(rad(alpha)), sA = Math.sin(rad(alpha));
  const cB = Math.cos(rad(beta)), sB = Math.sin(rad(beta));
  const cG = Math.cos(rad(gamma)), sG = Math.sin(rad(gamma));
  return [
    [cA * cG - sA * sB * sG, -cB * sA, cG * sA * sB + cA * sG],
    [cG * sA + cA * sB * sG, cA * cB, sA * sG - cA * cG * sB],
    [-cB * sG, sB, cB * cG],
  ];
}

const apply = (m, v) => m.map((row) => dot(row, v));

// 背面カメラの座標軸(地上の座標で、右・上・前)。screenAngle は画面の回転(screen.orientation.angle)
export function deviceBasis(alpha, beta, gamma, screenAngle = 0) {
  const m = rotationMatrix(alpha, beta, gamma);
  const t = rad(screenAngle);
  return {
    f: apply(m, [0, 0, -1]),
    right: apply(m, [Math.cos(t), -Math.sin(t), 0]),
    up: apply(m, [Math.sin(t), Math.cos(t), 0]),
  };
}

// 水平面での向き(度。北 0、東 90)
export const headingOf = (v) => norm360(deg(Math.atan2(v[0], v[1])));
// 上向きの傾き(度)
export const pitchOf = (v) => deg(Math.asin(Math.max(-1, Math.min(1, v[2]))));

// コンパスの値(iOS の webkitCompassHeading)が指す向き: 寝かせているときは端末の上端、立てているときは背面。
// 2 つのうち水平に近い方を使う(傾けているだけなら両者の向きは一致する)
export function compassReference(alpha, beta, gamma) {
  const m = rotationMatrix(alpha, beta, gamma);
  const top = apply(m, [0, 1, 0]), back = apply(m, [0, 0, -1]);
  return Math.hypot(top[0], top[1]) >= Math.hypot(back[0], back[1]) ? top : back;
}

// 座標軸を鉛直軸のまわりに回す(向きを delta 度だけ時計回りに足す)
export function rotateBasis(basis, delta) {
  const c = Math.cos(rad(delta)), s = Math.sin(rad(delta));
  const turn = (v) => [v[0] * c + v[1] * s, -v[0] * s + v[1] * c, v[2]];
  return { f: turn(basis.f), right: turn(basis.right), up: turn(basis.up) };
}

// 画角(度): 長辺の画角と映像の縦横から、横・縦の画角を求める
export function fovOf(longFov, width, height) {
  const t = Math.tan(rad(longFov / 2));
  const short = 2 * deg(Math.atan(t * Math.min(width, height) / Math.max(width, height)));
  return width >= height ? { hfov: longFov, vfov: short } : { hfov: short, vfov: longFov };
}

// 方位 az(度)・仰角 el(度)の方向が、映像のどこ(0〜1)に来るか。カメラの後ろなら null
export function projectDir(az, basis, fov, el = 0) {
  const d = [Math.sin(rad(az)) * Math.cos(rad(el)), Math.cos(rad(az)) * Math.cos(rad(el)), Math.sin(rad(el))];
  const z = dot(d, basis.f);
  if (z <= 1e-6) return null;
  return {
    x: 0.5 + dot(d, basis.right) / z / (2 * Math.tan(rad(fov.hfov / 2))),
    y: 0.5 - dot(d, basis.up) / z / (2 * Math.tan(rad(fov.vfov / 2))),
  };
}

// 日本の偏角(度。磁北が真北から西にずれている量)のおおよその値。
// 東京 約 7.5°・札幌 約 9.2°・那覇 約 4.9° に合わせた 1 次式で、誤差は 1° 程度(残りは手での補正で吸収する)
export function declinationJapan(lat, lon) {
  return 7.5 + 0.222 * (lat - 35.7) + 0.041 * (lon - 139.7);
}

// 磁北基準のコンパスの値を真北基準に直す
export const trueHeading = (magnetic, lat, lon) => norm360(magnetic - declinationJapan(lat, lon));

// 映像(object-fit: cover)の 0〜1 の位置を、表示枠の画素に移す
export function coverTransform(videoW, videoH, boxW, boxH) {
  const scale = Math.max(boxW / videoW, boxH / videoH);
  const w = videoW * scale, h = videoH * scale;
  const x0 = (boxW - w) / 2, y0 = (boxH - h) / 2;
  return {
    toPx: (p) => ({ x: x0 + p.x * w, y: y0 + p.y * h }),
    // 表示枠に見えている範囲の横の割合(手での補正で、ドラッグ量を角度に直すため)
    visibleX: boxW / w,
  };
}
