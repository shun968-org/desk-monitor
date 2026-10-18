// 映像の空と景色の境界(スカイライン)を、列ごとに上から空らしい色が続く所までとして求める。
// 縮小した映像(数十〜百数十画素四方)の RGBA を受け取る。電線のような細い線は 1 画素までなら飛ばす

// 空らしい色: 明るい青系か、曇り空のような明るい灰色
export function isSky(r, g, b) {
  const lum = 0.299 * r + 0.587 * g + 0.114 * b;
  const spread = Math.max(r, g, b) - Math.min(r, g, b);
  return (b >= r && b + 10 >= g && lum > 110) || (lum > 190 && spread < 35);
}

// 各列の境界の高さ(0〜1、上端 0)。上端が空でない列は 0
export function skyline(data, width, height) {
  const out = new Float32Array(width);
  const px = (x, y) => {
    const i = (y * width + x) * 4;
    return [data[i], data[i + 1], data[i + 2]];
  };
  const sky = (x, y) => isSky(...px(x, y));
  for (let x = 0; x < width; x++) {
    let y = 0;
    while (y < height) {
      if (sky(x, y)) { y++; continue; }
      // 細い線(1 画素)の向こうがまた空なら、続きとみなす
      if (y + 1 < height && sky(x, y + 1) && y > 0) { y += 2; continue; }
      break;
    }
    out[x] = y / height;
  }
  return smooth(out, 2);
}

// 前後 radius 列の中央値でならす(木の枝などで 1 列だけ飛び出すのを抑える)
export function smooth(values, radius) {
  const out = new Float32Array(values.length);
  for (let i = 0; i < values.length; i++) {
    const win = [];
    for (let j = Math.max(0, i - radius); j <= Math.min(values.length - 1, i + radius); j++) win.push(values[j]);
    win.sort((a, b) => a - b);
    out[i] = win[Math.floor(win.length / 2)];
  }
  return out;
}
