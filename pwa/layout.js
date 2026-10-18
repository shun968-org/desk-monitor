// 画面上部に並べるラベルの配置。近い観光地から順に、横に重ならない一番上の段へ置く。
// 段が足りなければ一番下の段に重ねて置き、描くときは遠い順(近いものが前面)にする

// items: [{ x(線の位置), w(ラベルの幅), distance_m, ... }]。返り値は { ...item, row, lx } を遠い順に並べたもの
export function layoutLabels(items, { width, maxRows, gap = 4, margin = 4 }) {
  const rows = Array.from({ length: Math.max(1, maxRows) }, () => []);
  const placed = [...items].sort((a, b) => a.distance_m - b.distance_m).map((item) => {
    const lx = Math.min(Math.max(item.x - item.w / 2, margin), Math.max(margin, width - item.w - margin));
    const span = [lx - gap, lx + item.w + gap];
    let row = rows.findIndex((r) => r.every(([a, b]) => span[1] <= a || span[0] >= b));
    if (row < 0) row = rows.length - 1;
    rows[row].push(span);
    return { ...item, row, lx };
  });
  return placed.reverse();
}

// 距離に応じた見た目: 近いほど大きく濃く(t は 0 = 近い 〜 1 = 遠い)
export function emphasis(distanceM, minM, maxM) {
  const t = maxM > minM ? Math.min(1, Math.max(0, (distanceM - minM) / (maxM - minM))) : 0;
  return { scale: 1.15 - 0.3 * t, alpha: 1 - 0.4 * t };
}
