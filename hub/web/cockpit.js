// Herbie cockpit: a 2D, interactive VW-style cabin on top of the Herbie Hub API.
// The windscreen is a simple pinhole-camera projection of the real route geometry and
// landmark coordinates: what you click on is where the car really is pointing.
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = (path, body) =>
  fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.json().catch(() => ({})));
const say = (text) => text && text.trim() && api("/api/say", { text: text.trim() });

let S = null;            // latest server state
let J = null;            // journey (route + stops)
let POIS = [];           // landmark catalogue
let CFG = {};
const known = new Set(); // landmark ids Herbie has already named for the driver
let rxAt = performance.now();

// ---------------------------------------------------------------- geometry
const D2R = Math.PI / 180;
const FOV_HALF = 50;
const CAM_H = 1.3;
const angleDiff = (a, b) => ((a - b + 540) % 360) - 180;
function bearing(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, dl = (lon2 - lon1) * D2R;
  const x = Math.sin(dl) * Math.cos(p2), y = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(x, y) / D2R + 360) % 360;
}
function dist(lat1, lon1, lat2, lon2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, dp = p2 - p1, dl = (lon2 - lon1) * D2R;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}
const hash = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

// Real-world heights (m) and drawing styles for the landmarks on the route.
const STYLE = {
  tv_tower: ["tower", 368], victory_column: ["column", 67], brandenburg_gate: ["gate", 26], reichstag: ["reichstag", 47],
  berliner_dom: ["dome", 98], hedwig: ["rotunda", 35], kwg: ["spire", 71], marienkirche: ["spire", 90],
  rotes_rathaus: ["hall", 74], humboldt_forum: ["palace", 32], altes_museum: ["temple", 26], neue_wache: ["temple", 14],
  zeughaus: ["palace", 22], humboldt_uni: ["palace", 22], staatsoper: ["temple", 28], adlon: ["block", 26],
  soviet_memorial: ["colonnade", 18], neptune_fountain: ["fountain", 10], world_clock: ["clock", 10],
  konzerthaus: ["temple", 30], chancellery: ["block", 36], hkw: ["block", 25], bellevue: ["palace", 20],
  bebelplatz: ["flat", 0], holocaust_memorial: ["flat", 2], checkpoint_charlie: ["flat", 4],
};
const styleOf = (p) => STYLE[p.id] || ["block", 22];

// ---------------------------------------------------------------- canvas
const cv = $("#view"), ctx = cv.getContext("2d");
let W = 0, H = 0, F = 1, HOR = 0, CX = 0, DPR = 1;
function resize() {
  const r = $("#windscreen").getBoundingClientRect();
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  cv.width = r.width * DPR; cv.height = r.height * DPR;
  W = r.width; H = r.height; CX = W / 2; HOR = H * 0.5;
  F = (W / 2) / Math.tan(FOV_HALF * D2R);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
window.addEventListener("resize", resize);

// view = smoothed camera pose, extrapolated between 4 Hz server ticks
const view = { lat: 0, lon: 0, heading: 0, odo: 0, route: 0, speed: 0 };
let lastFrame = performance.now();
const proj = (lat, fwd, z = 0) => ({ x: CX + (lat * F) / fwd, y: HOR + ((CAM_H - z) * F) / fwd });

function sky(hour, weather) {
  if (weather !== "dry") return ["#6c7a86", "#aab4bb"];
  if (hour >= 7 && hour < 17) return ["#4f93d1", "#cfe4f1"];
  if (hour >= 17 && hour < 20) return ["#38457a", "#f0a35e"];
  if (hour >= 5 && hour < 7) return ["#3d4f86", "#e8b48a"];
  return ["#040814", "#18223d"];
}

let hitboxes = [];
function drawScene(now) {
  const night = isNight();
  const [top, bottom] = sky(hour(), S.world.environment.weather);
  const svOn = svReady && CFG.streetview;
  ctx.clearRect(0, 0, W, H);
  hitboxes = [];
  if (!svOn) {
    let g = ctx.createLinearGradient(0, 0, 0, HOR);
    g.addColorStop(0, top); g.addColorStop(1, bottom);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, HOR + 1);
    if (night) for (let i = 0; i < 60; i++) { ctx.fillStyle = `rgba(255,255,255,${0.3 + hash(i) * 0.6})`; ctx.fillRect(hash(i + 7) * W, hash(i + 3) * HOR * 0.8, 1.5, 1.5); }
    drawSkyline(night);
    // ground
    ctx.fillStyle = night ? "#11161a" : "#6b7466"; ctx.fillRect(0, HOR, W, H - HOR);
    drawRoad(night);
  }
  // roadside objects + landmarks, far to near
  const items = svOn ? [] : roadside();
  for (const p of POIS) {
    const d = dist(view.lat, view.lon, p.lat, p.lon);
    const rel = angleDiff(bearing(view.lat, view.lon, p.lat, p.lon), view.heading);
    const [kind, h] = styleOf(p);
    const fwd = d * Math.cos(rel * D2R), lat = d * Math.sin(rel * D2R);
    const maxD = h >= 60 ? 3800 : 1100;
    if (fwd < 12 || d > maxD || Math.abs(rel) > 58) continue;
    items.push({ fwd, kind: "landmark", p, lat, d, rel, style: kind, h });
  }
  items.sort((a, b) => b.fwd - a.fwd);
  for (const it of items) {
    if (it.kind === "tree") drawTree(it, night);
    else if (it.kind === "building") drawBuilding(it, night);
    else if (!svOn) drawLandmark(it, night);
    else markOnly(it);
  }
  drawWeather(now);
  updateMarkers(items.filter((i) => i.kind === "landmark"));
}

function drawSkyline(night) {
  for (let a = 0; a < 360; a += 2.5) {
    const rel = angleDiff(a, view.heading);
    if (Math.abs(rel) > 56) continue;
    const x = CX + Math.tan(rel * D2R) * F, h = 6 + hash(a) * 26, w = Math.tan(2.6 * D2R) * F;
    ctx.fillStyle = night ? "#0b1020" : "rgba(120,135,150,.55)";
    ctx.fillRect(x - w / 2, HOR - h, w, h + 1);
  }
}

function drawRoad(night) {
  const q = (pts, fill) => { ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.closePath(); ctx.fillStyle = fill; ctx.fill(); };
  const near = 2.2, far = 900;
  q([proj(-12, near), proj(12, near), proj(12, far), proj(-12, far)], night ? "#2a2d31" : "#9a9890"); // sidewalks
  q([proj(-8, near), proj(8, near), proj(8, far), proj(-8, far)], wet() ? "#1d2125" : night ? "#191b1e" : "#3d4144");
  ctx.fillStyle = night ? "#bfae6a" : "#e8e4d0";
  const start = 12 - (view.odo % 12);
  for (let f = start; f < 220; f += 12) {
    const a = proj(-0.12, Math.max(f, near)), b = proj(0.12, Math.max(f, near)), c = proj(0.12, f + 4.5), d = proj(-0.12, f + 4.5);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.lineTo(c.x, c.y); ctx.lineTo(d.x, d.y); ctx.fill();
  }
  // bonnet
  ctx.fillStyle = "#c7ccd0";
  ctx.beginPath(); ctx.moveTo(W * 0.1, H); ctx.quadraticCurveTo(CX, H * 0.86, W * 0.9, H); ctx.fill();
  ctx.fillStyle = "rgba(255,255,255,.35)";
  ctx.beginPath(); ctx.moveTo(W * 0.3, H); ctx.quadraticCurveTo(CX, H * 0.9, W * 0.7, H); ctx.quadraticCurveTo(CX, H * 0.93, W * 0.3, H); ctx.fill();
}

// How each street of the route looks: half-width to the facades, trees, building heights.
const STREETS = {
  "Straße des 17. Juni": { half: 16, park: true },
  "Ebertstraße": { half: 18, trees: true },
  "Unter den Linden": { half: 34, trees: true },
  "Schloßplatz": { half: 45, trees: false },
  "Karl-Liebknecht-Straße": { half: 26, trees: true },
  "Bismarckstraße": { half: 22, trees: true },
  "Otto-Suhr-Allee": { half: 18, trees: true },
  "Holzmarktstraße": { half: 20 },
  "Warschauer Straße": { half: 22, trees: true },
};
const FACADES = [[214, 205, 186], [190, 184, 172], [222, 214, 196], [168, 160, 150], [205, 182, 150], [236, 232, 222]];

function roadside() {
  const out = [], spacing = 18;
  const d = S.world.drive || {};
  const cfg = STREETS[d.street] || { half: 18 };
  const onRoute = d.mode === "autopilot" && J;
  const base = onRoute ? view.route : view.odo;
  const first = spacing - (base % spacing);
  for (let f = first; f < 420; f += spacing) {
    const id = Math.round((base + f) / spacing);
    // leave gaps where real landmarks stand, so you can actually see them
    const nearStop = onRoute && J.stops.some((st) => Math.abs(st.at_m - (base + f)) < 55);
    for (const side of [-1, 1]) {
      const r = hash(id * 2 + (side > 0 ? 1 : 0));
      if (cfg.park) { out.push({ kind: "tree", fwd: f, lat: side * (13 + r * 12), r }); continue; }
      if (cfg.trees) out.push({ kind: "tree", fwd: f + 4, lat: side * (cfg.half - 7), r: 0.5 + r * 0.3 });
      if (!nearStop && r > 0.06) out.push({ kind: "building", fwd: f, lat: side * cfg.half, len: spacing - 1.5, h: 18 + r * 6, r, side });
    }
  }
  return out;
}

function drawTree(t, night) {
  const base = proj(t.lat, t.fwd), s = F / t.fwd;
  ctx.fillStyle = night ? "#120f0c" : "#4b3a2b";
  ctx.fillRect(base.x - 0.25 * s, base.y - 4 * s, 0.5 * s, 4 * s);
  ctx.fillStyle = night ? `rgba(14,28,18,${fade(t.fwd)})` : `rgba(${50 + t.r * 30},${100 + t.r * 40},${55 + t.r * 20},${fade(t.fwd)})`;
  ctx.beginPath(); ctx.ellipse(base.x, base.y - (7 + t.r * 3) * s, (3.5 + t.r * 2) * s, (4 + t.r * 2) * s, 0, 0, Math.PI * 2); ctx.fill();
}

function drawBuilding(b, night) {
  const f1 = b.fwd, f2 = b.fwd + b.len;
  const a = proj(b.lat, f1), c = proj(b.lat, f2), at = proj(b.lat, f1, b.h), ct = proj(b.lat, f2, b.h);
  const [cr, cg, cb] = FACADES[Math.floor(b.r * 997) % FACADES.length];
  ctx.fillStyle = night ? `rgba(25,28,34,${fade(f1)})` : `rgba(${cr},${cg},${cb},${fade(f1)})`;
  ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(c.x, c.y); ctx.lineTo(ct.x, ct.y); ctx.lineTo(at.x, at.y); ctx.closePath(); ctx.fill();
  if (f1 < 160) { // windows
    ctx.fillStyle = night ? "rgba(255,206,120,.75)" : "rgba(40,55,70,.45)";
    for (let floor = 1; floor < 6; floor++) for (let k = 0; k < 4; k++) {
      if (night && hash(b.r * 99 + floor * 7 + k) < 0.45) continue;
      const fa = f1 + 1.5 + k * 3.6, z = floor * 3.6 - 1.6;
      if (z + 1.6 > b.h) continue;
      const p1 = proj(b.lat, fa, z), p2 = proj(b.lat, fa + 1.6, z + 1.6);
      ctx.fillRect(Math.min(p1.x, p2.x), p2.y, Math.abs(p2.x - p1.x), p1.y - p2.y);
    }
  }
}

const fade = (fwd) => Math.max(0.25, 1 - fwd / 1600);

function drawLandmark(it, night) {
  const base = proj(it.lat, it.fwd), s = F / it.fwd, x = base.x, y = base.y, h = it.h * s;
  const stone = night ? "#4a4a52" : "#d8cfb8", dark = night ? "#2a2a32" : "#a99f88", copper = night ? "#2f4a44" : "#6fa595";
  ctx.globalAlpha = fade(it.fwd);
  const rect = (cx, w, hh, col, y0 = y) => { ctx.fillStyle = col; ctx.fillRect(cx - w / 2, y0 - hh, w, hh); };
  const dome = (cx, r, y0, col) => { ctx.fillStyle = col; ctx.beginPath(); ctx.ellipse(cx, y0, r, r * 1.05, 0, Math.PI, 0); ctx.fill(); };
  let w = h;
  switch (it.style) {
    case "tower": w = 32 * s;
      rect(x, 10 * s, h * 0.55, "#b9bec4"); rect(x, 6 * s, h * 0.05, "#b9bec4", y - h * 0.55);
      ctx.fillStyle = "#c9d0d6"; ctx.beginPath(); ctx.arc(x, y - h * 0.6, 16 * s, 0, Math.PI * 2); ctx.fill();
      rect(x, 3 * s, h * 0.35, "#d23a2e", y - h * 0.62);
      if (night) { ctx.fillStyle = "#ff3b30"; ctx.beginPath(); ctx.arc(x, y - h, 3, 0, 7); ctx.fill(); }
      break;
    case "column": w = 14 * s;
      rect(x, 18 * s, 7 * s, dark); rect(x, 6 * s, h * 0.82, stone, y - 7 * s);
      ctx.fillStyle = "#e3b23c"; rect(x, 4 * s, h * 0.12, "#e3b23c", y - h * 0.88); break;
    case "gate": w = 62 * s;
      rect(x, w, h * 0.12, stone, y - h * 0.62); rect(x, w * 0.95, h * 0.1, dark, y - h * 0.74);
      for (let i = 0; i < 6; i++) rect(x - w * 0.44 + i * w * 0.176, w * 0.07, h * 0.62, stone);
      ctx.fillStyle = "#3d6b5c"; rect(x, w * 0.16, h * 0.16, "#3d6b5c", y - h * 0.84); break;
    case "reichstag": w = 137 * s * 0.6;
      rect(x, w, h * 0.55, stone); rect(x - w * 0.45, w * 0.12, h * 0.68, stone); rect(x + w * 0.45, w * 0.12, h * 0.68, stone);
      dome(x, w * 0.14, y - h * 0.55, "rgba(180,210,230,.85)"); break;
    case "dome": w = 73 * s;
      rect(x, w, h * 0.42, stone); dome(x, w * 0.24, y - h * 0.42, copper); rect(x, w * 0.04, h * 0.18, copper, y - h * 0.42 - w * 0.24);
      [-0.42, 0.42].forEach((k) => { rect(x + w * k, w * 0.1, h * 0.5, stone); dome(x + w * k, w * 0.06, y - h * 0.5, copper); }); break;
    case "rotunda": w = 40 * s; rect(x, w, h * 0.5, stone); dome(x, w * 0.45, y - h * 0.5, copper); break;
    case "spire": w = 30 * s;
      rect(x, w, h * 0.32, night ? "#3a3036" : "#a8705a");
      ctx.fillStyle = night ? "#2f272b" : "#8f5c48"; ctx.beginPath(); ctx.moveTo(x - w * 0.18, y - h * 0.32);
      ctx.lineTo(x + w * 0.18, y - h * 0.32); ctx.lineTo(x + w * 0.18, y - h * 0.6); ctx.lineTo(x, y - h); ctx.lineTo(x - w * 0.18, y - h * 0.6); ctx.fill(); break;
    case "hall": w = 90 * s; rect(x, w, h * 0.3, night ? "#4a2a26" : "#b0533f"); rect(x, w * 0.16, h, night ? "#4a2a26" : "#a24a37"); break;
    case "palace": w = 110 * s; rect(x, w, h, stone);
      ctx.fillStyle = dark; for (let i = 0; i < 9; i++) rect(x - w * 0.44 + i * w * 0.11, w * 0.035, h * 0.55, dark, y - h * 0.2); break;
    case "temple": w = 60 * s; rect(x, w, h * 0.12, dark); rect(x, w * 0.92, h * 0.16, stone, y - h * 0.84);
      for (let i = 0; i < 8; i++) rect(x - w * 0.42 + i * w * 0.12, w * 0.05, h * 0.72, stone, y - h * 0.12);
      ctx.fillStyle = stone; ctx.beginPath(); ctx.moveTo(x - w * 0.46, y - h * 0.84); ctx.lineTo(x + w * 0.46, y - h * 0.84); ctx.lineTo(x, y - h); ctx.fill(); break;
    case "colonnade": w = 40 * s; rect(x, w, h * 0.15, dark); for (let i = 0; i < 5; i++) rect(x - w * 0.4 + i * w * 0.2, w * 0.06, h * 0.7, stone, y - h * 0.15);
      rect(x, w * 0.9, h * 0.15, stone, y - h * 0.85); break;
    case "fountain": w = 20 * s; ctx.fillStyle = dark; ctx.beginPath(); ctx.ellipse(x, y, w / 2, w / 8, 0, 0, 7); ctx.fill(); rect(x, w * 0.15, h, "#5f8f7f"); break;
    case "clock": w = 10 * s; rect(x, w * 0.15, h * 0.6, "#9aa3a8"); rect(x, w, h * 0.25, "#3f6fa0", y - h * 0.6); break;
    case "flat": w = 30 * s; break;
    default: w = 50 * s; rect(x, w, h, stone);
      ctx.fillStyle = dark; for (let i = 1; i < 5; i++) rect(x, w * 0.9, 1.5, dark, y - h * i / 5);
  }
  ctx.globalAlpha = 1;
  const top = it.style === "flat" ? y - 6 : y - Math.max(h, 8);
  it.box = { x0: x - w / 2, x1: x + w / 2, y0: top, y1: y };
  it.top = { x, y: top };
  hitboxes.push(it);
}

function markOnly(it) {
  const base = proj(it.lat, it.fwd), h = it.h * (F / it.fwd);
  it.top = { x: base.x, y: base.y - Math.max(h, 8) };
  it.box = { x0: base.x - 30, x1: base.x + 30, y0: it.top.y, y1: base.y };
  hitboxes.push(it);
}

let drops = Array.from({ length: 140 }, (_, i) => ({ x: hash(i), y: hash(i + 50), v: 0.6 + hash(i + 9) }));
function drawWeather(now) {
  const wx = S.world.environment.weather;
  if (wx === "dry") return;
  ctx.strokeStyle = wx === "rain" ? "rgba(200,215,230,.45)" : "rgba(255,255,255,.85)";
  ctx.fillStyle = "rgba(255,255,255,.85)";
  for (const d of drops) {
    d.y = (d.y + d.v * (wx === "rain" ? 0.02 : 0.004)) % 1;
    const x = d.x * W, y = d.y * H;
    if (wx === "rain") { ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - 3, y + 14); ctx.stroke(); }
    else { ctx.beginPath(); ctx.arc(x, y, 2, 0, 7); ctx.fill(); }
  }
}

// ---------------------------------------------------------------- AR markers + pointing
const hud = $("#hud");
const markerEls = new Map();
function updateMarkers(items) {
  const keep = new Set(), placed = [];
  const pointed = S.world.cabin.pointing_offset_deg;
  // Nearest first; a tag that would overlap a nearer one (or hide behind a side window) is skipped.
  for (const it of [...items].sort((a, b) => a.d - b.d)) {
    if (!it.top || it.d > (it.h >= 60 ? 2500 : 650)) continue;
    const ty = Math.max(34, it.top.y - 4);
    if (it.top.x < W * 0.13 || it.top.x > W * 0.87) continue;
    if (placed.some((q) => Math.abs(q.x - it.top.x) < 92 && Math.abs(q.y - ty) < 26)) continue;
    placed.push({ x: it.top.x, y: ty });
    keep.add(it.p.id);
    let el = markerEls.get(it.p.id);
    if (!el) {
      el = document.createElement("button");
      el.className = "marker";
      el.innerHTML = `<span class="tag"></span><span class="stem"></span>`;
      el.onclick = () => pointAndAsk(it.p.id);
      hud.appendChild(el); markerEls.set(it.p.id, el);
    }
    const label = known.has(it.p.id) ? esc(it.p.name) : "?";
    el.querySelector(".tag").innerHTML = `${label} <b>${fmtDist(it.d)}</b>`;
    el.style.left = `${it.top.x}px`; el.style.top = `${Math.max(34, it.top.y - 4)}px`;
    el.classList.toggle("pointed", pointed !== null && pointed !== undefined && Math.abs(angleDiff(it.rel, pointed)) < 6);
    el._rel = it.rel;
  }
  for (const [id, el] of markerEls) if (!keep.has(id)) { el.remove(); markerEls.delete(id); }

  // side windows: landmarks beside the car
  for (const side of ["left", "right"]) {
    const list = POIS.map((p) => ({ p, d: dist(view.lat, view.lon, p.lat, p.lon), rel: angleDiff(bearing(view.lat, view.lon, p.lat, p.lon), view.heading) }))
      .filter((o) => o.d < 450 && (side === "left" ? o.rel < -58 && o.rel > -150 : o.rel > 58 && o.rel < 150))
      .sort((a, b) => a.d - b.d).slice(0, 3);
    const box = $(`#side-${side} .side-list`);
    const html = list.map((o) => `<span class="tag" data-id="${o.p.id}">${known.has(o.p.id) ? esc(o.p.name) : "◆"} ${fmtDist(o.d)}</span>`).join("");
    if (box.innerHTML !== html) box.innerHTML = html;
  }
  const ptr = $("#pointer");
  if (pointed === null || pointed === undefined || Math.abs(pointed) > 55) {
    ptr.hidden = true;
  } else {
    ptr.hidden = false;
    ptr.style.left = `${CX + Math.tan(pointed * D2R) * F}px`; ptr.style.top = `${HOR - 30}px`;
  }
  $("#side-left").classList.toggle("pointed", pointed !== null && pointed !== undefined && pointed <= -55);
  $("#side-right").classList.toggle("pointed", pointed !== null && pointed !== undefined && pointed >= 55);
}
const fmtDist = (d) => (d >= 1000 ? `${(d / 1000).toFixed(1)} km` : `${Math.round(d / 10) * 10} m`);

function relOf(id) {
  const p = POIS.find((x) => x.id === id);
  return Math.round(angleDiff(bearing(view.lat, view.lon, p.lat, p.lon), view.heading));
}
async function point(offset) {
  await api("/api/gesture", { offset_deg: offset === null ? null : Math.max(-180, Math.min(180, offset)) });
}
async function pointAndAsk(id) {
  await point(relOf(id));
  say("Hey Herbie, what's that?");
}
cv.addEventListener("click", (e) => {
  const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const hit = hitboxes.filter((h) => x >= h.box.x0 && x <= h.box.x1 && y >= h.box.y0 - 10 && y <= h.box.y1).sort((a, b) => a.fwd - b.fwd)[0];
  if (hit) return pointAndAsk(hit.p.id);
  point(Math.round(Math.atan((x - CX) / F) / D2R));
  subtitle("You're pointing there. Now ask: “Hey Herbie, what's that?”", "Tip");
});
for (const side of ["left", "right"]) {
  $(`#side-${side}`).addEventListener("click", (e) => {
    const tag = e.target.closest("[data-id]");
    if (tag) return pointAndAsk(tag.dataset.id);
    point(side === "left" ? -90 : 90);
    subtitle(`Pointing out of the ${side} window. Ask away!`, "Tip");
  });
}

// ---------------------------------------------------------------- render loop
function hour() { return Number(S.world.clock.slice(11, 13)); }
function isNight() { const h = hour(); return h >= 20 || h < 6; }
function wet() { return S.world.environment.weather !== "dry"; }

function frame(now) {
  requestAnimationFrame(frame);
  if (!S) return;
  const dt = Math.min(0.1, (now - lastFrame) / 1000); lastFrame = now;
  const sig = S.world.signals, scale = S.world.drive?.time_scale || 1;
  const tLat = sig["Vehicle.CurrentLocation.Latitude"], tLon = sig["Vehicle.CurrentLocation.Longitude"];
  const tHead = sig["Vehicle.CurrentLocation.Heading"] || 0, speed = (sig["Vehicle.Speed"] || 0) * (S.world.playing ? 1 : 0);
  if (tLat == null) return;
  const moving = S.world.drive?.mode !== "static" && S.world.playing;
  if (!view.lat || dist(view.lat, view.lon, tLat, tLon) > 60) { view.lat = tLat; view.lon = tLon; view.heading = tHead; }
  // extrapolate since the last server tick, then ease toward it
  const since = Math.min(0.3, (now - rxAt) / 1000);
  const ahead = moving ? (speed / 3.6) * scale * since : 0;
  const eLat = tLat + (ahead * Math.cos(tHead * D2R)) / 111320;
  const eLon = tLon + (ahead * Math.sin(tHead * D2R)) / (111320 * Math.cos(tLat * D2R));
  view.lat += (eLat - view.lat) * 0.25; view.lon += (eLon - view.lon) * 0.25;
  view.heading = (view.heading + angleDiff(tHead, view.heading) * 0.15 + 360) % 360;
  if (moving) view.odo += (speed / 3.6) * scale * dt;
  const routeNow = (S.world.drive?.route_pos_m || 0) + ahead;
  view.route = Math.abs(routeNow - view.route) > 40 ? routeNow : view.route + (routeNow - view.route) * 0.25;
  drawScene(now);
  $("#wheel").style.transform = `rotate(${(S.world.drive?.steer || 0) * 70}deg)`;
}

// ---------------------------------------------------------------- Street View (optional)
let svReady = false, svKey = "";
function maybeStreetView() {
  if (!CFG.streetview || !S) return;
  const sig = S.world.signals;
  const lat = sig["Vehicle.CurrentLocation.Latitude"], lon = sig["Vehicle.CurrentLocation.Longitude"], h = sig["Vehicle.CurrentLocation.Heading"];
  if (lat == null) return;
  const key = `${lat.toFixed(4)},${lon.toFixed(4)},${Math.round(h / 10) * 10}`;
  if (key === svKey) return;
  svKey = key;
  const img = new Image();
  img.onload = () => { $("#sv").src = img.src; $("#sv").hidden = false; svReady = true; };
  img.onerror = () => { if (!svReady) $("#sv").hidden = true; };
  img.src = `/api/streetview?lat=${lat}&lon=${lon}&heading=${(h + 360) % 360}`;
}

// ---------------------------------------------------------------- panels
const MODE_BADGE = { SPORT: "sport", NORMAL: "", ECONOMY: "" };
const AMBIENT = { NEUTRAL: "#9fb7c4", WARM: "#ff9a3c", VIBRANT: "#d24dff" };
function renderPanels() {
  const w = S.world, sig = w.signals, d = w.drive || {};
  const speed = sig["Vehicle.Speed"];
  $("#c-speed").textContent = speed == null ? "--" : Math.round(speed);
  const badges = [];
  if (d.mode === "autopilot") badges.push(["AUTO", "auto"]);
  if (d.mode === "manual") badges.push(["MANUAL", ""]);
  const pm = sig["Vehicle.Powertrain.Transmission.PerformanceMode"];
  badges.push([pm, MODE_BADGE[pm] || ""]);
  if (sig["Vehicle.Chassis.Suspension.DampingMode"] === "SOFT") badges.push(["COMFORT", "comfort"]);
  if (sig["Vehicle.Chassis.Suspension.DampingMode"] === "FIRM") badges.push(["FIRM", "sport"]);
  if (sig["Vehicle.OBD.DTCList"].length) badges.push(["⚠ " + sig["Vehicle.OBD.DTCList"].join(" "), "fault"]);
  if (sig["Vehicle.Cabin.Infotainment.Meeting.Recording"]) badges.push(["● REC", "rec"]);
  if (!w.system.gps_ok) badges.push(["NO GPS", "fault"]);
  if (!w.system.cloud_online) badges.push(["OFFLINE", "fault"]);
  $("#c-badges").innerHTML = badges.map(([t, c]) => `<span class="badge ${c}">${esc(t)}</span>`).join("");
  $("#c-street").textContent = d.street || "Berlin";
  const stop = (d.stops || [])[0];
  $("#c-next").textContent = stop ? `${stop.kind === "fuel" ? "⛽" : "☕"} stop: ${stop.name} · ${fmtDist(Math.max(0, stop.at_m - d.route_pos_m))}`
    : d.mode === "parked" ? (S.experience?.id ? "Parked, ready when you are" : "Parked")
    : d.target ? `→ ${d.target}` : d.next_stop ? `next: ${d.next_stop.name} · ${fmtDist(d.next_stop.in_m)}` : "";
  const fuel = sig["Vehicle.Powertrain.FuelSystem.RelativeLevel"];
  $("#c-fuel b").textContent = fuel == null ? "--" : `${Math.round(fuel)}%`;
  $("#c-fuel .bar i").style.width = `${Math.max(0, Math.min(100, fuel || 0))}%`;
  $("#c-fuel").classList.toggle("low", fuel < 15);
  renderMusic();
  $("#c-clock").textContent = `${w.weekday.slice(0, 3)} ${w.clock.slice(11, 16)}`;
  $("#street-sign").textContent = d.street || "";
  document.documentElement.style.setProperty("--ambient", AMBIENT[sig["Vehicle.Cabin.Light.AmbientMood"]] || AMBIENT.NEUTRAL);
  $("#ambient").setAttribute("stroke", AMBIENT[sig["Vehicle.Cabin.Light.AmbientMood"]] || AMBIENT.NEUTRAL);
  $("#temp").textContent = `${Number(sig["Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature"]).toFixed(1)}°`;

  const rows = [
    ["Drive mode", pm], ["Suspension", sig["Vehicle.Chassis.Suspension.DampingMode"]],
    ["Seat recline", `${sig["Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline"]}°`],
    ["Music", sig["Vehicle.Cabin.Infotainment.Media.Playlist"]], ["Ambient light", sig["Vehicle.Cabin.Light.AmbientMood"]],
    ["Navigation stop", sig["Vehicle.Cabin.Infotainment.Navigation.Waypoint"]?.name || "—"],
    ["Call audio", sig["Vehicle.Cabin.Infotainment.Meeting.AudioActive"] ? "on" : "off"],
    ["Fault codes", sig["Vehicle.OBD.DTCList"].join(", ") || "none"],
    ["Fuel", `${Number(sig["Vehicle.Powertrain.FuelSystem.RelativeLevel"]).toFixed(0)}% · ${fmtDist(sig["Vehicle.Powertrain.FuelSystem.Range"] || 0)} range`],
    ["Service due in", `${Math.round((sig["Vehicle.Service.TimeToService"] || 0) / 86400)} days`],
    ["Driver mood", w.cabin.camera_on ? w.cabin.mood : "camera off"],
    ["Route", d.length_m ? `${fmtDist(d.route_pos_m)} of ${fmtDist(d.length_m)}` : "—"],
    ["Context", S.bucket],
  ];
  $("#car").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");

  $$("#scale button").forEach((b) => b.classList.toggle("on", Number(b.dataset.v) === d.time_scale));
  $("#pause").textContent = w.playing ? "Pause" : "Resume";
  $$("#scenarios button").forEach((b) => b.classList.toggle("on", b.dataset.id === S.scenario));
  const vals = { fatigue: w.driver.fatigue, hunger: w.driver.hunger, passengers: w.cabin.passengers, weather: w.environment.weather,
    gps_ok: w.system.gps_ok, cloud_online: w.system.cloud_online };
  $$("[data-k]").forEach((el) => { if (el !== document.activeElement) { const v = vals[el.dataset.k]; if (el.type === "checkbox") el.checked = !!v; else el.value = v; } });
  updateMap();
}

// ---------------------------------------------------------------- music
function renderMusic() {
  const t = S.music?.track, m = S.world.media;
  $("#nowplaying").classList.toggle("paused", !t);
  $("#np-text").textContent = t ? `${t.title} · ${t.artist}` : "Nothing playing";
  $("#np-big").innerHTML = t ? `<div class="g">${esc(t.genre)} · ${esc(S.world.signals["Vehicle.Cabin.Infotainment.Media.Playlist"])}</div>
    <div class="t">${esc(t.title)}</div><div class="a">${esc(t.artist)}</div>
    <div class="prog"><i style="width:${Math.min(100, (m.track_elapsed_s / m.track_seconds) * 100)}%"></i></div>` : "Nothing playing";
  $("#upnext").innerHTML = (S.music?.up_next || []).filter(Boolean)
    .map((x) => `<div>Up next: <b>${esc(x.title)}</b> · ${esc(x.artist)} <small>(${esc(x.genre)})</small></div>`).join("");
}

// ---------------------------------------------------------------- map
let map, carMarker, routeLayer, stopLayer, mapJourney = null;
function setupMap() {
  map = L.map("map", { zoomControl: false, attributionControl: true });
  L.tileLayer(CFG.map_tiles, { attribution: CFG.map_attribution, maxZoom: 19 }).addTo(map);
  routeLayer = L.layerGroup().addTo(map);
  stopLayer = L.layerGroup().addTo(map);
  carMarker = L.marker(J.points[0], { icon: L.divIcon({ className: "", html: '<div class="car-dot"></div>', iconSize: [14, 14], iconAnchor: [7, 7] }) }).addTo(map);
  drawJourney();
}
function drawJourney() {
  routeLayer.clearLayers();
  const line = L.polyline(J.points, { color: "#2f6bff", weight: 5, opacity: 0.8 }).addTo(routeLayer);
  J.stops.forEach((s) => {
    const p = POIS.find((x) => x.id === s.poi_id);
    if (p) L.circleMarker([p.lat, p.lon], { radius: 5, color: "#ffb547", weight: 1, fillOpacity: 0.8 }).bindTooltip(s.name)
      .on("click", () => { $("#dest").value = s.poi_id; }).addTo(routeLayer);
  });
  (J.amenities || []).forEach((a) => L.circleMarker([a.lat, a.lon], { radius: 4, color: a.kind === "fuel" ? "#ff5d5d" : "#3ee0c8", weight: 1, fillOpacity: 0.7 })
    .bindTooltip(`${a.kind === "fuel" ? "⛽" : "☕"} ${a.name}${a.address ? ", " + a.address : ""}`).addTo(routeLayer));
  map.fitBounds(line.getBounds(), { padding: [10, 10] });
  $("#dest").innerHTML = J.stops.filter((s) => s.at_m > 50).map((s) => `<option value="${s.poi_id}">${esc(s.name)}</option>`).join("")
    + `<option value="">${esc(J.end || "End of route")}</option>`;
  mapJourney = J.id;
}
async function checkJourney() {
  const jid = S.world.drive?.journey;
  if (jid && jid !== mapJourney && map) { J = await api("/api/journey"); mapJourney = jid; drawJourney(); }
}
function updateMap() {
  if (!map) return;
  const sig = S.world.signals, lat = sig["Vehicle.CurrentLocation.Latitude"], lon = sig["Vehicle.CurrentLocation.Longitude"];
  if (lat != null) carMarker.setLatLng([lat, lon]);
  checkJourney();
}

// ---------------------------------------------------------------- dialogue
let speakVoice = null, lastDriverText = "How do I look?";
function pickVoice() {
  const vs = speechSynthesis.getVoices();
  speakVoice = vs.find((v) => /en-GB/.test(v.lang) && /Male|Daniel|Arthur/.test(v.name)) || vs.find((v) => v.lang.startsWith("en")) || null;
}
if ("speechSynthesis" in window) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }
function speak(text) {
  if (!$("#tts").checked || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  if (speakVoice) u.voice = speakVoice;
  u.rate = 1.03; u.pitch = 1.05;
  speechSynthesis.speak(u);
}
let subTimer;
function subtitle(text, who = "Herbie") {
  const el = $("#subtitle");
  el.innerHTML = `<span class="who">${esc(who)}</span>${esc(text)}`;
  el.classList.add("show");
  clearTimeout(subTimer);
  subTimer = setTimeout(() => el.classList.remove("show"), Math.min(14000, 3500 + text.length * 55));
}

function addEntry(entry, live = true) {
  const chat = $("#chat"), div = document.createElement("div");
  if (entry.role === "driver") { div.className = "msg driver"; div.textContent = entry.text; lastDriverText = entry.text; }
  else if (entry.role === "event") {
    if (!/arrived|dtc/.test(entry.text)) return;
    div.className = "msg event"; div.textContent = `⚡ ${entry.text}`;
  } else {
    const r = entry.reply;
    if (!r.text) return;
    if (r.data?.poi?.id) known.add(r.data.poi.id);
    div.className = "msg herbie";
    let acts = "";
    if (r.suggestion) {
      const [y, n] = r.suggestion.applied ? ["Keep", "Undo"] : ["Yes", "No"];
      acts = `<div class="acts" data-sid="${r.suggestion.id}"><button class="primary" data-a="1">${y}</button><button data-a="0">${n}</button></div>`;
    }
    if (r.clarify?.length) acts += `<div class="acts">${r.clarify.map((c) => `<button data-say="${esc(c)}">${esc(c)}</button>`).join("")}</div>`;
    div.innerHTML = `${esc(r.text)}${acts}${r.trace?.length ? `<details class="why"><summary>why · ${esc(r.skill)} · ${r.data?.latency_ms ?? "?"} ms</summary><ol>${r.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></details>` : ""}`;
    if (live) { subtitle(r.text); speak(r.text); }
    if (live && r.data?.request_snapshot) sendSnapshot();
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}
$("#chat").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.say) return say(b.dataset.say);
  const box = b.closest("[data-sid]"); if (!box) return;
  box.querySelectorAll("button").forEach((x) => (x.disabled = true));
  api("/api/feedback", { id: box.dataset.sid, accept: b.dataset.a === "1" });
});
$("#say").addEventListener("submit", (e) => { e.preventDefault(); say($("#text").value); $("#text").value = ""; });
$$(".chips [data-say]").forEach((b) => (b.onclick = () => say(b.dataset.say)));

// ---------------------------------------------------------------- voice input
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false;
function setListening(on) { listening = on; $$(".mic").forEach((m) => m.classList.toggle("listening", on)); }
function startPTT() {
  if (!SR) { subtitle("Voice input needs Chrome, Edge or Safari. You can type instead.", "Tip"); return; }
  if (listening) return;
  rec = new SR(); rec.lang = "en-US"; rec.interimResults = false; rec.continuous = false;
  rec.onresult = (e) => say(e.results[0][0].transcript);
  rec.onend = () => { setListening(false); if ($("#handsfree").checked) startHandsfree(); };
  rec.onerror = () => setListening(false);
  speechSynthesis?.cancel();
  setListening(true); rec.start();
}
function stopPTT() { if (rec && listening && !$("#handsfree").checked) rec.stop(); }
function startHandsfree() {
  if (!SR || !$("#handsfree").checked || listening) return;
  rec = new SR(); rec.lang = "en-US"; rec.continuous = true; rec.interimResults = false;
  rec.onresult = (e) => {
    const t = e.results[e.results.length - 1][0].transcript;
    if (/\bherbie\b/i.test(t)) say(t);
  };
  rec.onend = () => { setListening(false); setTimeout(startHandsfree, 300); };
  setListening(true); rec.start();
}
$$(".mic").forEach((m) => (m.onclick = () => (listening ? rec?.stop() : startPTT())));
$("#handsfree").onchange = () => { if ($("#handsfree").checked) startHandsfree(); else rec?.stop(); };

// ---------------------------------------------------------------- keyboard + WASD
const KEYS = { w: "accelerate", s: "brake", a: "left", d: "right", arrowup: "accelerate", arrowdown: "brake", arrowleft: "left", arrowright: "right" };
const held = new Map();
function press(act, btn) {
  if (held.has(act)) return;
  api("/api/drive", { action: act });
  held.set(act, setInterval(() => api("/api/drive", { action: act }), 140));
  btn?.classList.add("down");
}
function release(act, btn) { clearInterval(held.get(act)); held.delete(act); btn?.classList.remove("down"); }
const typing = () => ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
window.addEventListener("keydown", (e) => {
  if (typing() || e.repeat && !KEYS[e.key.toLowerCase()]) return;
  const k = e.key.toLowerCase();
  if (KEYS[k]) { e.preventDefault(); press(KEYS[k], $(`#wasd [data-act="${KEYS[k]}"]`)); }
  else if (k === " ") { e.preventDefault(); if (!e.repeat) startPTT(); }
  else if (k === "q") point(-90);
  else if (k === "e") point(90);
  else if (k === "r") point(0);
  else if (k === "x") point(null);
});
window.addEventListener("keyup", (e) => {
  const k = e.key.toLowerCase();
  if (KEYS[k]) release(KEYS[k], $(`#wasd [data-act="${KEYS[k]}"]`));
  if (k === " " && !typing()) stopPTT();
});
$$("#wasd [data-act]").forEach((b) => {
  b.addEventListener("pointerdown", () => press(b.dataset.act, b));
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => b.addEventListener(ev, () => release(b.dataset.act, b)));
});

// ---------------------------------------------------------------- tabs + controls
$$("#tabs button").forEach((b) => (b.onclick = () => {
  $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === b.dataset.tab));
  if (b.dataset.tab === "nav") setTimeout(() => map?.invalidateSize(), 50);
}));
$("#go").onclick = () => api("/api/autopilot", { stop_id: $("#dest").value || null });
$("#stop").onclick = () => say("stop the car");
$("#restart").onclick = () => api("/api/autopilot", { restart: true, stop_id: null });
$$("#scale button").forEach((b) => (b.onclick = () => api("/api/sim", { time_scale: Number(b.dataset.v) })));
$("#pause").onclick = () => api("/api/sim", { playing: !S.world.playing });
$$("[data-k]").forEach((el) => el.addEventListener("change", () => {
  const v = el.type === "checkbox" ? el.checked : el.type === "range" || el.type === "number" ? Number(el.value) : el.value;
  api("/api/sim", { [el.dataset.k]: v });
}));
$("#inject").onclick = () => api("/api/dtc", { code: $("#dtc").value });
$("#scenarios").addEventListener("click", (e) => {
  const b = e.target.closest("[data-id]"); if (!b) return;
  $("#chat").innerHTML = ""; known.clear(); api(`/api/scenario/${b.dataset.id}`, {});
});

// ---------------------------------------------------------------- driver camera
// Face analysis runs locally (MediaPipe). Only ~12 expression scores are sent to the hub, every 1.5 s.
const BLEND = ["mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight", "browDownLeft", "browDownRight",
  "browInnerUp", "eyeBlinkLeft", "eyeBlinkRight", "jawOpen", "mouthPressLeft", "mouthPressRight"];
const MOOD_ICON = { happy: "😊 happy", neutral: "🙂 neutral", tense: "😣 tense", sad: "😔 down", tired: "🥱 tired", unknown: "… reading" };
let camStream = null, landmarker = null, camTimer = null, acc = [];
async function startCamera() {
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" }, audio: false });
  } catch (e) {
    subtitle("I couldn't open the camera. Check your browser's camera permission for localhost.", "Camera");
    return;
  }
  const v = $("#cam"); v.srcObject = camStream; await v.play();
  $("#drivercam").classList.add("on");
  await api("/api/camera", { on: true });
  $("#mood-label").textContent = "loading face model…";
  try {
    if (!landmarker) {
      const { FilesetResolver, FaceLandmarker } = await import("/vendor/mediapipe/vision_bundle.mjs");
      const fs = await FilesetResolver.forVisionTasks("/vendor/mediapipe/wasm");
      landmarker = await FaceLandmarker.createFromOptions(fs, {
        baseOptions: { modelAssetPath: "/vendor/mediapipe/face_landmarker.task", delegate: "GPU" },
        outputFaceBlendshapes: true, runningMode: "VIDEO", numFaces: 1 });
    }
  } catch (e) {
    console.error(e);
    $("#mood-label").textContent = "face model failed to load";
    return;
  }
  $("#mood-label").textContent = MOOD_ICON.unknown;
  let lastSend = performance.now();
  camTimer = setInterval(async () => {
    if (!camStream || v.readyState < 2) return;
    const res = landmarker.detectForVideo(v, performance.now());
    const cats = res.faceBlendshapes?.[0]?.categories;
    if (cats) acc.push(Object.fromEntries(cats.filter((c) => BLEND.includes(c.categoryName)).map((c) => [c.categoryName, c.score])));
    if (performance.now() - lastSend < 1500) return;
    lastSend = performance.now();
    if (!acc.length) { $("#mood-label").textContent = "no face in view"; return; }
    const avg = {};
    for (const k of BLEND) avg[k] = acc.reduce((t, a) => t + (a[k] || 0), 0) / acc.length;
    acc = [];
    const r = await api("/api/driver/face", { blendshapes: avg });
    if (r.mood) $("#mood-label").textContent = MOOD_ICON[r.mood] || r.mood;
  }, 200);
}
async function stopCamera() {
  clearInterval(camTimer); camTimer = null; acc = [];
  camStream?.getTracks().forEach((t) => t.stop()); camStream = null;
  $("#cam").srcObject = null; $("#drivercam").classList.remove("on"); $("#mood-label").textContent = "";
  await api("/api/camera", { on: false });
}
$("#cam-toggle").onclick = () => (camStream ? stopCamera() : startCamera());
async function sendSnapshot() {
  const v = $("#cam");
  if (!camStream || v.readyState < 2) return;
  const c = document.createElement("canvas"); c.width = 480; c.height = 360;
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  await api("/api/camera/snapshot", { image: c.toDataURL("image/jpeg", 0.75), question: lastDriverText });
}

// ---------------------------------------------------------------- experiences
const EXP_ICON = { drive_to_work: "🌅", drive_home: "🌆", friday_night: "🎉", free_drive: "🗺️" };
async function showPicker() {
  const list = await api("/api/experiences");
  list.push({ id: "free_drive", title: "Free drive: Berlin landmarks", blurb: "Sunday afternoon tour from the Victory Column to Alexanderplatz. Just point and ask." });
  $("#exp-list").innerHTML = list.map((e) => `<button class="exp" data-exp="${e.id}"><span class="icon">${EXP_ICON[e.id] || "🚗"}</span>
    <b>${esc(e.title)}</b><span>${esc(e.blurb)}</span></button>`).join("");
  $("#intro").hidden = false;
}
$("#exp-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-exp]"); if (!b) return;
  $("#intro").hidden = true;
  speechSynthesis?.speak(new SpeechSynthesisUtterance(""));  // unlock audio on this user gesture
  $("#chat").innerHTML = ""; known.clear();
  if (b.dataset.exp === "free_drive") await api("/api/scenario/berlin_drive", {});
  else await api(`/api/experience/${b.dataset.exp}`, {});
});
$("#menu").onclick = showPicker;

// ---------------------------------------------------------------- boot
function onState(s) {
  S = s; rxAt = performance.now();
  renderPanels(); maybeStreetView();
}
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "state") onState(msg.state);
    if (msg.type === "log") addEntry(msg.entry);
  };
  ws.onclose = () => setTimeout(connect, 1500);
}

(async function boot() {
  resize();
  [CFG, J, POIS] = await Promise.all([api("/api/config"), api("/api/journey"), api("/api/pois")]);
  $("#llm-info").textContent = CFG.llm ? `LLM: ${CFG.llm_provider} · ${CFG.model}` : "LLM off (rule-based)";
  $("#dest").innerHTML = J.stops.slice(1).map((s) => `<option value="${s.poi_id}">${esc(s.name)}</option>`).join("")
    + `<option value="">Alexanderplatz (end of route)</option>`;
  const scen = await api("/api/scenarios");
  $("#scenarios").innerHTML = scen.map((s) => `<button data-id="${s.id}" title="${esc(s.about)}">${esc(s.title)}</button>`).join("");
  setupMap();
  onState(await api("/api/state"));
  (await api("/api/log")).forEach((e) => addEntry(e, false));
  connect();
  requestAnimationFrame(frame);
  showPicker();
})();
