// 利用状況の記録: 端末内(localStorage)にだけ集計して保存し、外部には送らない。
// 利用者が設定の「診断情報をコピー」で自分で共有したときだけ、開発者が受け取る。
// 位置・映像・観光地の名前は記録しない(どこで使ったかが分かるため)

export const METRICS_KEY = 'desk-monitor-ar:metrics';
// コンパスの精度(±度)の区切り: 5 以下・10 以下・20 以下・30 以下・それより大きい
export const COMPASS_BOUNDS = [5, 10, 20, 30];
// 値の並び(補正量・取得時間など)は新しいものから最大この件数
export const MAX_SAMPLES = 30;

export function emptyMetrics(now) {
  return {
    since: now, updated: now, counts: {}, compass: COMPASS_BOUNDS.map(() => 0).concat(0),
    samples: {}, sky: { frames: 0, found: 0 }, durationSec: 0, env: {},
  };
}

export function bucketIndex(value, bounds = COMPASS_BOUNDS) {
  const i = bounds.findIndex((b) => value <= b);
  return i < 0 ? bounds.length : i;
}

// ブラウザの種類(iPhone では Safari / Chrome / Google アプリの中のブラウザなどで動きが違うため)
export function browserOf(ua) {
  if (/CriOS/.test(ua)) return 'Chrome(iOS)';
  if (/FxiOS/.test(ua)) return 'Firefox(iOS)';
  if (/EdgiOS/.test(ua)) return 'Edge(iOS)';
  if (/GSA\//.test(ua)) return 'Google アプリ(iOS)';
  if (/iPhone|iPad/.test(ua) && /Safari/.test(ua)) return 'Safari(iOS)';
  if (/Android/.test(ua) && /Chrome/.test(ua)) return 'Chrome(Android)';
  return 'その他';
}

const median = (arr) => {
  if (!arr?.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
};

export function createMetrics({ storage = globalThis.localStorage, now = () => Date.now(), enabled = true } = {}) {
  let data;
  try {
    data = JSON.parse(storage.getItem(METRICS_KEY)) ?? emptyMetrics(now());
  } catch {
    data = emptyMetrics(now());
  }
  let dirty = false;
  const touch = () => { data.updated = now(); dirty = true; };

  return {
    get enabled() { return enabled; },
    setEnabled(v) { enabled = Boolean(v); },
    get data() { return data; },
    inc(key, n = 1) {
      if (!enabled) return;
      data.counts[key] = (data.counts[key] ?? 0) + n;
      touch();
    },
    compass(accuracy) {
      if (!enabled || !(accuracy >= 0)) return;
      data.compass[bucketIndex(accuracy)]++;
      touch();
    },
    sample(key, value) {
      if (!enabled || !Number.isFinite(value)) return;
      const list = (data.samples[key] ??= []);
      list.unshift(Math.round(value * 10) / 10);
      list.length = Math.min(list.length, MAX_SAMPLES);
      touch();
    },
    sky(found) {
      if (!enabled) return;
      data.sky.frames++;
      if (found) data.sky.found++;
      touch();
    },
    addDuration(sec) {
      if (!enabled || !(sec > 0)) return;
      data.durationSec += Math.round(sec);
      touch();
    },
    env(info) {
      if (!enabled) return;
      data.env = { ...data.env, ...info };
      touch();
    },
    // 保存(頻繁に書かないよう、呼び出し側が数秒ごとと画面を離れるときに呼ぶ)
    flush() {
      if (!dirty) return;
      try { storage.setItem(METRICS_KEY, JSON.stringify(data)); } catch { /* 保存できなくても動く */ }
      dirty = false;
    },
    reset() {
      data = emptyMetrics(now());
      try { storage.removeItem(METRICS_KEY); } catch { /* 同上 */ }
      dirty = false;
    },
    // 共有用のまとめ(人が読めて、そのまま貼り付けられる形)
    summary(appVersion) {
      const offsets = data.samples.offset ?? [];
      return {
        app: appVersion,
        period: { from: new Date(data.since).toISOString(), to: new Date(data.updated).toISOString() },
        env: data.env,
        counts: data.counts,
        compassAccuracy: Object.fromEntries(
          [...COMPASS_BOUNDS.map((b) => `±${b}°以下`), `±${COMPASS_BOUNDS.at(-1)}°超`].map((k, i) => [k, data.compass[i]])),
        manualOffset: { median: median(offsets), medianAbs: median(offsets.map(Math.abs)), recent: offsets },
        fetchSec: { median: median(data.samples.fetchSec), recent: data.samples.fetchSec ?? [] },
        poisAround: { median: median(data.samples.poisAround) },
        labelsShown: { median: median(data.samples.labelsShown) },
        skyFoundRatio: data.sky.frames ? Math.round(data.sky.found / data.sky.frames * 100) / 100 : null,
        usedMinutes: Math.round(data.durationSec / 60),
      };
    },
  };
}
