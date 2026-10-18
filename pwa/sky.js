// 映像の空と景色の境界(スカイライン)を、列ごとに上から空らしい色が続く所までとして求める。
// 縮小した映像(数十〜百数十画素四方)の RGBA を受け取る。
// 空の色は決め打ちにせず、画面上端の帯の色をその場の空の色とみなす(青空・曇り・夕焼けのどれでも使える)。
// 空は地平線に近いほど明るくなるので、すぐ上の空の画素との差も見て、ゆるやかな色の変化には追従する

// 上端の帯(高さの割合)。この帯の色の中央値をその場の空の色とする
export const TOP_BAND = 0.08;
// 空の色との差の上限(全体)と、すぐ上の空の画素との差の上限(なめらかに変わる範囲)。
// 天頂の青と地平線近くの白は大きく違うので全体の上限はゆるくし、建物などの輪郭は急な変化で捉える
export const GLOBAL_DIFF = 170;
export const LOCAL_DIFF = 28;
// 飛ばす横線の太さの上限(高さの割合)
export const GAP_RATIO = 0.03;

const lum = ([r, g, b]) => 0.299 * r + 0.587 * g + 0.114 * b;
// 色の差(明るさの差を少し軽く見る。日の当たり方で空の明るさは場所により変わるため)
export function colorDiff([r1, g1, b1], [r2, g2, b2]) {
  const dl = (lum([r1, g1, b1]) - lum([r2, g2, b2])) * 0.6;
  const dr = r1 - r2, dg = g1 - g2, db = b1 - b2;
  const mean = (dr + dg + db) / 3;
  // 色味の差(明るさをそろえたときの差)と明るさの差
  return Math.hypot(dr - mean, dg - mean, db - mean) * 1.5 + Math.abs(dl);
}

// 空の色として使えるか: 暗すぎず(夜・室内)、木の葉のような緑でない
export function plausibleSky(c) {
  const [r, g, b] = c;
  return lum(c) > 90 && !(g > r + 25 && g > b + 15);
}

const median = (arr) => {
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
};

// 上端の帯から空の色を求める。空が映っていなければ null
export function skyColor(data, width, height) {
  const rows = Math.max(1, Math.round(height * TOP_BAND));
  const rs = [], gs = [], bs = [];
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < width; x++) {
      const i = (y * width + x) * 4;
      rs.push(data[i]); gs.push(data[i + 1]); bs.push(data[i + 2]);
    }
  }
  const c = [median(rs), median(gs), median(bs)];
  return plausibleSky(c) ? c : null;
}

// 各列の境界の高さ(0〜1、上端 0)。空が映っていない列は 0
export function skyline(data, width, height) {
  const out = new Float32Array(width);
  const ref = skyColor(data, width, height);
  if (!ref) return out;
  const maxGap = Math.max(1, Math.round(height * GAP_RATIO));
  const px = (x, y) => {
    const i = (y * width + x) * 4;
    return [data[i], data[i + 1], data[i + 2]];
  };
  for (let x = 0; x < width; x++) {
    // その場の空の色から始める(最上段が電線にかかっていても、その色に引きずられない)
    let prev = ref;
    const isSkyHere = (c) => colorDiff(c, ref) <= GLOBAL_DIFF && colorDiff(c, prev) <= LOCAL_DIFF;
    let y = 0;
    scan: while (y < height) {
      if (isSkyHere(px(x, y))) { prev = px(x, y); y++; continue; }
      // 電線のような細い横線(maxGap 画素まで)の向こうがまた空なら、続きとみなす
      for (let k = 1; k <= maxGap && y + k < height; k++) {
        if (isSkyHere(px(x, y + k))) { prev = px(x, y + k); y += k + 1; continue scan; }
      }
      break;
    }
    out[x] = y / height;
  }
  // 電柱のような細い縦の物は、前後の列の中央値でならして消す
  return smooth(out, 3);
}

// 前後 radius 列の中央値でならす(木の枝などで 1 列だけ飛び出すのを抑える)
export function smooth(values, radius) {
  const out = new Float32Array(values.length);
  for (let i = 0; i < values.length; i++) {
    const win = [];
    for (let j = Math.max(0, i - radius); j <= Math.min(values.length - 1, i + radius); j++) win.push(values[j]);
    out[i] = median(win);
  }
  return out;
}

// 前回の境界となめらかにつなぐ(映像の揺れや雲でちらつかないように)。列の数が変わったら新しい値を使う
export function blend(prev, next, keep = 0.6) {
  if (!prev || prev.length !== next.length) return next;
  return next.map((v, i) => (prev[i] > 0 && v > 0 ? prev[i] * keep + v * (1 - keep) : v));
}
