// 観光 AR(PWA): 背面カメラの映像に、向けた方角にある観光地の名前と距離を重ねる。
// 向きは DeviceOrientation(iOS はジャイロで滑らかに動く相対の alpha を、webkitCompassHeading で真北に合わせる)、
// 位置は Geolocation、観光地は poi.js(OpenStreetMap)
import {
  angleDiff, compassReference, coverTransform, declinationJapan, deviceBasis, fovOf, headingOf, norm360,
  pitchOf, projectDir, rotateBasis, trueHeading,
} from './geo.js';
import { fetchPois, isStale, loadCache } from './poi.js';

const $ = (id) => document.getElementById(id);
// 読み込まれている版(キャッシュで古いものが使われていないかを開始の画面で確かめる)。sw.js の VERSION と合わせる
const APP_VERSION = 'v3';
$('version').textContent = APP_VERSION;
const video = $('video'), canvas = $('overlay');

// ─── 設定(端末ごとの使い勝手なので localStorage に保存する) ───
const SETTINGS_KEY = 'desk-monitor-ar:settings';
// longFov: 映像の長辺の画角(度)。iPhone の広角カメラ(35mm 換算 26mm)で約 67°
const DEFAULTS = { longFov: 67, minKm: 0, maxKm: 20, radiusKm: 30, maxLabels: 12, userOffset: 0 };
const settings = { ...DEFAULTS, ...readJson(SETTINGS_KEY) };

function readJson(key) {
  try { return JSON.parse(localStorage.getItem(key)) ?? {}; } catch { return {}; }
}
function saveSettings() {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch { /* 保存できなくても動く */ }
}

// ─── 状態 ───
let orient = null;            // { alpha, beta, gamma }
let northOffset = null;       // 相対の alpha を真北に合わせる回転量(度)
let compassAccuracy = null;   // iOS のコンパスの精度(度)
let lastOrientAt = 0;
let position = null;          // { lat, lon, accuracy }
let poiCache = loadCache();
let pois = [];                // 現在地からの distance_m / bearing 付き。近い順
let fetching = false, fetchError = null, lastFetchAttempt = 0;
let labelBoxes = [];          // タップの当たり判定
let startedAt = null;
let permissionError = null;
let geoError = null;

const screenAngle = () => screen.orientation?.angle ?? window.orientation ?? 0;
const here = () => position ?? { lat: 35.7, lon: 139.7 };  // 位置が取れるまでの偏角の計算用

// ─── 向き ───
function onOrientation(e, absolute) {
  if (e.alpha == null || e.beta == null || e.gamma == null) return;
  orient = { alpha: e.alpha, beta: e.beta, gamma: e.gamma };
  lastOrientAt = performance.now();
  const { lat, lon } = here();
  if (absolute) {
    // Android の絶対値の alpha は磁北基準なので、偏角だけ回す
    northOffset = -declinationJapan(lat, lon);
  } else if (typeof e.webkitCompassHeading === 'number' && e.webkitCompassAccuracy >= 0 && screenAngle() === 0) {
    // コンパスの値は揺れるので、ジャイロの相対値とのずれ(northOffset)だけをゆっくり追従させる
    compassAccuracy = e.webkitCompassAccuracy;
    const target = norm360(trueHeading(e.webkitCompassHeading, lat, lon)
      - headingOf(compassReference(e.alpha, e.beta, e.gamma)));
    northOffset = northOffset == null ? target : norm360(northOffset + 0.05 * angleDiff(target, northOffset));
  }
}

function currentBasis() {
  if (!orient || northOffset == null) return null;
  const b = deviceBasis(orient.alpha, orient.beta, orient.gamma, screenAngle());
  return rotateBasis(b, northOffset + settings.userOffset);
}

// ─── 位置と観光地 ───
function onPosition(p) {
  position = { lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy };
  updatePois();
  maybeFetch();
}

function updatePois() {
  if (!position || !poiCache) { pois = []; return; }
  const from = turf.point([position.lon, position.lat]);
  pois = poiCache.pois.map((p) => {
    const to = turf.point([p.lon, p.lat]);
    return { ...p, distance_m: turf.distance(from, to) * 1000, bearing: norm360(turf.bearing(from, to)) };
  }).sort((a, b) => a.distance_m - b.distance_m);
}

async function maybeFetch(force = false) {
  if (!position || fetching) return;
  // 失敗したら 1 分は問い合わせない(公開の Overpass API に負荷をかけない)
  if (!force && fetchError && Date.now() - lastFetchAttempt < 60_000) return;
  if (!force && !isStale(poiCache, position.lat, position.lon, settings.radiusKm)) return;
  fetching = true; lastFetchAttempt = Date.now();
  renderFetchText();
  try {
    poiCache = await fetchPois(position.lat, position.lon, settings.radiusKm);
    fetchError = null;
    updatePois();
  } catch (err) {
    fetchError = err.message || String(err);
  } finally {
    fetching = false;
    renderFetchText();
  }
}

function renderFetchText() {
  const t = poiCache ? new Date(poiCache.updated_at).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' }) : '—';
  $('fetchText').textContent = fetching ? '取得中…'
    : `${poiCache?.pois.length ?? 0} 件(${t})` + (fetchError ? ` 失敗: ${fetchError}` : '');
}

// ─── 描画 ───
const fmtKm = (m) => (m >= 10000 ? `${Math.round(m / 1000)}km` : `${(m / 1000).toFixed(1)}km`);

function videoFov() {
  return fovOf(settings.longFov, video.videoWidth || 3, video.videoHeight || 4);
}

// 画面に出す観光地: 画面内かつ距離の範囲内で、近い順に maxLabels 件
function visiblePois(basis, cover, fov, w, h) {
  const out = [];
  for (const p of pois) {
    if (p.distance_m < settings.minKm * 1000 || p.distance_m > settings.maxKm * 1000) continue;
    const pos = projectDir(p.bearing, basis, fov);
    if (!pos) continue;
    const pt = cover.toPx(pos);
    if (pt.x < 0 || pt.x > w || pt.y < 0 || pt.y > h) continue;
    out.push({ ...p, pt });
    if (out.length >= settings.maxLabels) break;
  }
  return out;
}

function draw() {
  requestAnimationFrame(draw);
  const w = canvas.clientWidth, h = canvas.clientHeight, ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  labelBoxes = [];
  const basis = currentBasis();
  if (!basis || !video.videoWidth) return;
  const fov = videoFov(), cover = coverTransform(video.videoWidth, video.videoHeight, w, h);

  // 地平線(仰角 0 の線。傾ければ傾く)
  const hd = headingOf(basis.f);
  const a = projectDir(hd - fov.hfov, basis, fov), b = projectDir(hd + fov.hfov, basis, fov);
  if (a && b) {
    const pa = cover.toPx(a), pb = cover.toPx(b);
    ctx.strokeStyle = '#ffffff55'; ctx.setLineDash([6, 6]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke(); ctx.setLineDash([]);
  }

  const fontSize = Math.max(13, Math.round(Math.min(w, h) / 26));
  ctx.font = `600 ${fontSize}px system-ui, -apple-system, sans-serif`;
  ctx.textBaseline = 'middle';
  // 左から順に、横に重ならない段へ並べる(重なるときは一段上に積む)。ダッシュボードと同じ
  const rowH = fontSize + 12, rows = [];
  const labels = visiblePois(basis, cover, fov, w, h).sort((p, q) => p.pt.x - q.pt.x).map((p) => {
    const text = `${p.name} ${fmtKm(p.distance_m)}`;
    const lw = ctx.measureText(text).width + 14;
    let row = 0;
    while (rows[row] !== undefined && rows[row] > p.pt.x - lw / 2 - 4) row++;
    rows[row] = p.pt.x + lw / 2;
    return { p, text, lw, row };
  });
  for (const { p, text, lw, row } of labels) {
    const { x, y } = p.pt;
    const cy = y - 28 - row * rowH;
    ctx.strokeStyle = '#ffcc80'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, cy + rowH / 2 - 4); ctx.lineTo(x, y); ctx.stroke();
    ctx.fillStyle = '#ffcc80';
    ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
    const lx = Math.min(Math.max(x - lw / 2, 4), w - lw - 4);
    const box = { x: lx, y: cy - rowH / 2 + 2, w: lw, h: rowH - 4, poi: p };
    ctx.fillStyle = '#000000b8';
    ctx.fillRect(box.x, box.y, box.w, box.h);
    ctx.fillStyle = '#fff';
    ctx.fillText(text, lx + 7, cy);
    labelBoxes.push(box);
  }
  lastShown = labels.length;
}
let lastShown = 0;

// ─── 表示の文字(4 回 / 秒) ───
function renderHud() {
  const basis = currentBasis();
  const warn = [];
  if (!orient && startedAt != null && performance.now() - startedAt > 3000) warn.push('向きのセンサーの値が届きません(設定 > Safari > モーションと画面の向きのアクセス)');
  else if (orient && northOffset == null) warn.push('方位を待っています(端末を少し動かしてください)');
  if (compassAccuracy != null && compassAccuracy > 25) warn.push(`コンパスの精度が低い(±${Math.round(compassAccuracy)}°)。端末を 8 の字に動かしてください`);
  if (geoError) warn.push(geoError);
  else if (!position && startedAt != null) warn.push('位置を待っています');
  if (permissionError) warn.push(permissionError);
  if (fetchError) warn.push(`観光地を取得できません: ${fetchError}`);
  if (screenAngle() !== 0) warn.push('縦持ちで使ってください(横持ちでは方位を合わせ直しません)');

  const off = settings.userOffset;
  $('hudMain').textContent = basis
    ? `方位 ${Math.round(headingOf(basis.f))}° ・ 傾き ${Math.round(pitchOf(basis.f))}°` + (off ? ` ・ 補正 ${off > 0 ? '+' : ''}${off.toFixed(1)}°` : '')
    : '方位 —';
  const parts = [];
  if (position) parts.push(`位置 ±${Math.round(position.accuracy)}m`);
  if (compassAccuracy != null) parts.push(`コンパス ±${Math.round(compassAccuracy)}°`);
  parts.push(`表示 ${lastShown} / 周辺 ${pois.length} 件`);
  if (fetching) parts.push('観光地を取得中…');
  $('hudSub').textContent = parts.join(' ・ ');
  $('hudWarn').hidden = !warn.length;
  $('hudWarn').textContent = warn.join(' / ');
  $('offsetText').textContent = `${off > 0 ? '+' : ''}${off.toFixed(1)}°`;
}

// ─── 地図(2 回 / 秒) ───
let map = null;
function renderMap() {
  if ($('mapBox').hidden || !position) return;
  if (!map) {
    map = L.map('map', { zoomControl: false, attributionControl: true }).setView([position.lat, position.lon], 11);
    L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
      maxZoom: 18, attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener">地理院タイル</a>',
    }).addTo(map);
    map.poiLayer = L.layerGroup().addTo(map);
    map.layer = L.layerGroup().addTo(map);
    map.follow = true;
    map.on('dragstart', () => { map.follow = false; });
  }
  if (map.pois !== pois) {
    // 観光地の点は、データか現在地が変わったときだけ描き直す
    map.pois = pois;
    map.poiLayer.clearLayers();
    for (const p of pois) {
      L.circleMarker([p.lat, p.lon], { radius: 3, weight: 1, color: '#000', fillColor: '#ff7043', fillOpacity: 0.8 })
        .bindTooltip(`${p.name}(${p.kind}、${fmtKm(p.distance_m)})`).addTo(map.poiLayer);
    }
  }
  map.layer.clearLayers();
  const basis = currentBasis();
  const { lat, lon } = position;
  if (basis) {
    // 扇形(内側を切り欠いた形): 画角の左端〜右端、距離の範囲
    const fov = videoFov(), hd = headingOf(basis.f), n = 24, outer = [], inner = [];
    const dest = (b, km) => {
      const [x, y] = turf.destination([lon, lat], km, b).geometry.coordinates;
      return [y, x];
    };
    for (let i = 0; i <= n; i++) {
      const b = hd - fov.hfov / 2 + fov.hfov * i / n;
      outer.push(dest(b, settings.maxKm));
      inner.push(settings.minKm > 0 ? dest(b, settings.minKm) : [lat, lon]);
    }
    L.polygon([...outer, ...inner.reverse()], { color: '#ffa726', weight: 1.5, fillOpacity: 0.12, interactive: false }).addTo(map.layer);
  }
  L.circle([lat, lon], { radius: position.accuracy, weight: 0, fillColor: '#1e88e5', fillOpacity: 0.15, interactive: false }).addTo(map.layer);
  L.circleMarker([lat, lon], { radius: 6, color: '#fff', weight: 2, fillColor: '#1e88e5', fillOpacity: 1 }).addTo(map.layer);
  if (map.follow) map.panTo([lat, lon], { animate: false });
}

// ─── 操作: 横のドラッグで方位を補正、タップでラベルの詳細 ───
let drag = null;
canvas.addEventListener('pointerdown', (e) => {
  drag = { x0: e.clientX, y0: e.clientY, x: e.clientX, t: performance.now(), moved: false };
  canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove', (e) => {
  if (!drag) return;
  if (Math.abs(e.clientX - drag.x0) > 8) drag.moved = true;
  if (!drag.moved || !video.videoWidth) return;
  // ラベルを指に付いて動かす: 右へドラッグ = 向きを左に回す
  const cover = coverTransform(video.videoWidth, video.videoHeight, canvas.clientWidth, canvas.clientHeight);
  const visibleHfov = videoFov().hfov * cover.visibleX;
  settings.userOffset = Math.max(-60, Math.min(60, settings.userOffset - (e.clientX - drag.x) / canvas.clientWidth * visibleHfov));
  drag.x = e.clientX;
});
canvas.addEventListener('pointerup', (e) => {
  if (drag && !drag.moved) {
    const hit = labelBoxes.find((b) => e.clientX >= b.x && e.clientX <= b.x + b.w && e.clientY >= b.y && e.clientY <= b.y + b.h);
    if (hit) showInfo(hit.poi); else closeSheets();
  } else if (drag) {
    saveSettings();
  }
  drag = null;
});

function showInfo(p) {
  closeSheets();
  $('infoName').textContent = p.name;
  $('infoMeta').textContent = `${p.kind} ・ ${fmtKm(p.distance_m)} ・ 方位 ${Math.round(p.bearing)}°`;
  const wiki = $('infoWiki');
  wiki.hidden = !p.wikidata;
  if (p.wikidata) wiki.href = `https://www.wikidata.org/wiki/Special:GoToLinkedPage/jawiki/${encodeURIComponent(p.wikidata)}`;
  $('infoMap').href = `https://maps.gsi.go.jp/#15/${p.lat}/${p.lon}/`;
  $('info').hidden = false;
}

function closeSheets() {
  $('info').hidden = true;
  $('settings').hidden = true;
  $('settingsBtn').setAttribute('aria-expanded', 'false');
}
$('infoClose').addEventListener('click', closeSheets);

// ─── 設定の画面 ───
const rangeSlider = noUiSlider.create($('range'), {
  start: [settings.minKm, settings.maxKm], connect: true, step: 0.5, margin: 0.5,
  range: { min: 0, max: settings.radiusKm },
  handleAttributes: [{ 'aria-label': 'ラベルを出す距離の開始(km)' }, { 'aria-label': 'ラベルを出す距離の終了(km)' }],
});
function syncSettingsUi() {
  rangeSlider.updateOptions({ range: { min: 0, max: settings.radiusKm } }, false);
  rangeSlider.set([settings.minKm, Math.min(settings.maxKm, settings.radiusKm)], false);
  $('rangeText').textContent = `${settings.minKm}〜${settings.maxKm} km`;
  $('fov').value = settings.longFov; $('fovText').textContent = `${settings.longFov}°`;
  $('maxLabels').value = settings.maxLabels; $('maxLabelsText').textContent = `${settings.maxLabels} 件`;
  $('radius').value = String(settings.radiusKm);
}
rangeSlider.on('slide', ([min, max]) => {
  settings.minKm = Number(min); settings.maxKm = Number(max);
  $('rangeText').textContent = `${settings.minKm}〜${settings.maxKm} km`;
});
rangeSlider.on('change', saveSettings);
$('fov').addEventListener('input', (e) => { settings.longFov = Number(e.target.value); syncSettingsUi(); saveSettings(); });
$('maxLabels').addEventListener('input', (e) => { settings.maxLabels = Number(e.target.value); syncSettingsUi(); saveSettings(); });
$('radius').addEventListener('change', (e) => {
  settings.radiusKm = Number(e.target.value);
  settings.maxKm = Math.min(settings.maxKm, settings.radiusKm);
  settings.minKm = Math.min(settings.minKm, settings.maxKm - 0.5);
  syncSettingsUi(); saveSettings(); maybeFetch();
});
$('offsetReset').addEventListener('click', () => { settings.userOffset = 0; saveSettings(); });
$('refetch').addEventListener('click', () => maybeFetch(true));
$('settingsBtn').addEventListener('click', () => {
  const open = $('settings').hidden;
  closeSheets();
  $('settings').hidden = !open;
  $('settingsBtn').setAttribute('aria-expanded', String(open));
  renderFetchText();
});
$('mapBtn').addEventListener('click', () => {
  const open = $('mapBox').hidden;
  $('mapBox').hidden = !open;
  $('mapBtn').setAttribute('aria-pressed', String(open));
  if (open && map) { map.invalidateSize(); map.follow = true; }
  renderMap();
});
syncSettingsUi();

// ─── 開始: iOS は、センサーの許可をボタンを押したときに求める必要がある ───
async function startCamera() {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1440 } }, audio: false,
  });
  video.srcObject = stream;
  // play() の Promise は環境によって解決しないことがあるので待たない(autoplay と muted で再生される)
  video.play().catch(() => {});
}

let listening = false;
let geoWatch = null;

// 位置を追う。拒否・失敗したら止めて理由を出し、次に画面へ戻ったとき(設定を変えて戻ったときなど)に求め直す
function watchGeo() {
  if (geoWatch != null) return;
  geoError = null;
  geoWatch = navigator.geolocation.watchPosition((p) => { geoError = null; onPosition(p); }, (err) => {
    if (err.code === err.TIMEOUT && position) return;  // 一時的に取れないだけなら、前の位置で続ける
    navigator.geolocation.clearWatch(geoWatch);
    geoWatch = null;
    geoError = err.code === err.PERMISSION_DENIED
      ? '位置情報が許可されていません(設定 > プライバシーとセキュリティ > 位置情報サービス > Safari Web サイト、と Web サイトの設定)'
      : `位置を取得できません: ${err.message}`;
  }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 30000 });
}
let wakeLock = null;
async function keepAwake() {
  try { wakeLock = await navigator.wakeLock?.request('screen'); } catch { /* 対応していなければ何もしない */ }
}

$('startBtn').addEventListener('click', () => {
  start().catch((err) => {
    // 想定外の失敗も、開始の画面に出して分かるようにする
    $('startError').hidden = false;
    $('startError').textContent = `開始できません: ${err?.message || err}`;
  });
});

async function start() {
  const errors = [];
  if (!window.isSecureContext) errors.push('HTTPS で開いてください(カメラとセンサーは HTTPS でしか使えません)');
  // requestPermission はクリックの処理の中で最初に呼ぶ(await の後だと拒否される)。
  // 結果は待たずに進め、拒否されたら画面上部に出す
  if (typeof DeviceOrientationEvent?.requestPermission === 'function') {
    DeviceOrientationEvent.requestPermission().catch(() => 'denied').then((p) => {
      if (p !== 'granted') permissionError = '動きと方向へのアクセスが許可されていません';
    });
  }
  // 位置もカメラより先に(ボタンを押した直後に)求める
  if (navigator.geolocation) watchGeo(); else errors.push('位置情報を使えません');
  try {
    await startCamera();
  } catch (err) {
    errors.push(`カメラを使えません: ${err.message || err.name}`);
  }
  if (!listening) {
    // カメラが使えずに「開始」を押し直したとき、二重に登録しない
    listening = true;
    if ('ondeviceorientationabsolute' in window) {
      window.addEventListener('deviceorientationabsolute', (e) => onOrientation(e, true));
    } else {
      window.addEventListener('deviceorientation', (e) => onOrientation(e, false));
    }
  }
  if (errors.length && !video.srcObject) {
    $('startError').hidden = false;
    $('startError').textContent = errors.join(' / ');
    return;
  }
  startedAt = performance.now();
  $('start').hidden = true;
  if (errors.length) fetchError = errors.join(' / ');
  keepAwake();
  updatePois();
}

document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState !== 'visible' || startedAt == null) return;
  keepAwake();
  if (geoError && navigator.geolocation) watchGeo();
  // バックグラウンドから戻ると iOS はカメラを止めているので掛け直す
  const track = video.srcObject?.getVideoTracks()[0];
  if (!track || track.readyState === 'ended') startCamera().catch(() => {});
});

setInterval(renderHud, 250);
setInterval(renderMap, 500);
requestAnimationFrame(draw);

if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
