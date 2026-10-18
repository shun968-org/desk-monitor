// オフラインでも開けるよう、アプリの本体と CDN のライブラリを端末に保存する。
// 地図のタイルは開いた範囲だけ保存する(件数に上限)。観光地のデータは app.js が localStorage に保存する
const VERSION = 'v4';
const APP_CACHE = `app-${VERSION}`;
const TILE_CACHE = 'tiles';
const TILE_LIMIT = 500;
const APP_FILES = [
  './', 'index.html', 'style.css', 'app.js', 'geo.js', 'layout.js', 'poi.js', 'sky.js', 'manifest.webmanifest',
  'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
  'https://cdn.jsdelivr.net/npm/nouislider@15.7.1/dist/nouislider.min.css',
  'https://cdn.jsdelivr.net/npm/nouislider@15.7.1/dist/nouislider.min.js',
  'https://cdn.jsdelivr.net/npm/@turf/turf@7.1.0/turf.min.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(APP_CACHE)
    .then((c) => c.addAll(APP_FILES.map((u) => new Request(u, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith('app-') && k !== APP_CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function trimTiles() {
  const cache = await caches.open(TILE_CACHE);
  const keys = await cache.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - TILE_LIMIT)).map((k) => cache.delete(k)));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;  // Overpass API(POST)はそのまま通す
  const url = new URL(req.url);
  if (url.hostname === 'cyberjapandata.gsi.go.jp') {
    // 地図のタイル: 通信を優先し、圏外なら保存したものを出す
    e.respondWith(fetch(req).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(TILE_CACHE).then((c) => c.put(req, copy)).then(trimTiles);
      }
      return res;
    }).catch(() => caches.match(req)));
    return;
  }
  if (url.origin === self.location.origin) {
    // アプリの本体: 通信を優先して更新をすぐ反映し、圏外なら保存したものを出す。
    // GitHub Pages は 10 分キャッシュさせるので、ブラウザのキャッシュを使わずサーバーに確かめる
    e.respondWith(fetch(req, { cache: 'no-cache' }).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(APP_CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req, { ignoreSearch: true })));
    return;
  }
  // CDN のライブラリ(版を固定しているので、保存したものを優先する)
  e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
});
