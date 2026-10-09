// Herbie Hub cockpit: renders world state and Herbie's dialogue, sends driver input.
const $ = (s) => document.querySelector(s);
const api = (path, body) =>
  fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.json());
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let state = null;
const answered = new Set();

// ---------- map ----------
const map = L.map("map", { zoomControl: false, attributionControl: true }).setView([52.51, 13.38], 14);
L.tileLayer("https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png", {
  attribution: '&copy; Stadia Maps &copy; OpenMapTiles &copy; OpenStreetMap contributors', maxZoom: 19,
}).addTo(map);
const carIcon = L.divIcon({ className: "car-icon", html: "🚗", iconSize: [24, 24], iconAnchor: [12, 12] });
const car = L.marker([52.5, 13.4], { icon: carIcon }).addTo(map);
const cone = L.polygon([], { color: "#3ee0c8", weight: 1, fillOpacity: 0.15 }).addTo(map);
const headingLine = L.polyline([], { color: "#a6f23a", weight: 2, dashArray: "4 4" }).addTo(map);
const poiLayer = L.layerGroup().addTo(map);
let lastCenter = null;

function offsetPoint(lat, lon, bearing, meters) {
  const R = 6371000, d = meters / R, b = (bearing * Math.PI) / 180, p1 = (lat * Math.PI) / 180, l1 = (lon * Math.PI) / 180;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(p2 * 180) / Math.PI, (l2 * 180) / Math.PI];
}

api("/api/pois").then((pois) =>
  pois.forEach((p) =>
    L.circleMarker([p.lat, p.lon], { radius: 4 + 4 * p.prominence, color: "#ffb547", weight: 1, fillOpacity: 0.6 })
      .bindTooltip(p.name).addTo(poiLayer)));

function renderMap(s) {
  const sig = s.world.signals;
  const lat = sig["Vehicle.CurrentLocation.Latitude"], lon = sig["Vehicle.CurrentLocation.Longitude"];
  const heading = sig["Vehicle.CurrentLocation.Heading"] || 0;
  if (lat == null || lon == null) return;
  car.setLatLng([lat, lon]);
  car.getElement() && (car.getElement().style.opacity = s.world.system.gps_ok ? 1 : 0.35);
  headingLine.setLatLngs([[lat, lon], offsetPoint(lat, lon, heading, 150)]);
  const off = s.world.cabin.pointing_offset_deg;
  if (off === null || off === undefined) cone.setLatLngs([]);
  else {
    const b = heading + off, pts = [[lat, lon]];
    for (let a = -30; a <= 30; a += 5) pts.push(offsetPoint(lat, lon, b + a, 600));
    cone.setLatLngs(pts);
  }
  const key = `${lat.toFixed(4)},${lon.toFixed(4)}`;
  if (key !== lastCenter && !map.getBounds().pad(-0.2).contains([lat, lon])) map.panTo([lat, lon]);
  lastCenter = key;
}

// ---------- panels ----------
function renderPills(s) {
  const w = s.world, llm = s.llm;
  const pills = [
    [`${w.weekday} ${w.clock.slice(11, 16)}`, ""],
    [`Cloud ${w.system.cloud_online ? "online" : "offline"}`, w.system.cloud_online ? "on" : "off"],
    [`GPS ${w.system.gps_ok ? "ok" : "lost"}`, w.system.gps_ok ? "on" : "off"],
    [llm.enabled ? `LLM ${llm.model}` : "LLM off (rule-based)", llm.enabled ? "on" : ""],
    [`context: ${s.bucket}`, ""],
  ];
  $("#pills").innerHTML = pills.map(([t, c]) => `<span class="pill ${c}">${esc(t)}</span>`).join("");
}

const VEHICLE_ROWS = [
  ["Speed", "Vehicle.Speed", (v) => (v == null ? "invalid" : `${Math.round(v)} km/h`)],
  ["Heading", "Vehicle.CurrentLocation.Heading", (v) => `${Math.round(v ?? 0)}°`],
  ["Drive mode", "Vehicle.Powertrain.Transmission.PerformanceMode"],
  ["Suspension", "Vehicle.Chassis.Suspension.DampingMode"],
  ["Seat recline", "Vehicle.Cabin.Seat.Row1.DriverSide.Backrest.Recline", (v) => `${v}°`],
  ["Cabin temp", "Vehicle.Cabin.HVAC.Station.Row1.Driver.Temperature", (v) => `${v} °C`],
  ["Music", "Vehicle.Cabin.Infotainment.Media.Playlist"],
  ["Ambient light", "Vehicle.Cabin.Light.AmbientMood"],
  ["Navigation stop", "Vehicle.Cabin.Infotainment.Navigation.Waypoint", (v) => (v ? v.name : "—")],
  ["Call audio", "Vehicle.Cabin.Infotainment.Meeting.AudioActive", (v) => (v ? "on" : "off")],
  ["Recording", "Vehicle.Cabin.Infotainment.Meeting.Recording", (v) => (v ? "● REC" : "off")],
  ["Fault codes", "Vehicle.OBD.DTCList", (v) => (v.length ? v.join(", ") : "none")],
];
const prevSignals = {};
function renderVehicle(s) {
  const sig = s.world.signals;
  $("#vehicle").innerHTML = VEHICLE_ROWS.map(([label, path, fmt]) => {
    const v = sig[path], shown = fmt ? fmt(v) : v;
    const changed = JSON.stringify(prevSignals[path]) !== JSON.stringify(v) && prevSignals[path] !== undefined;
    const bad = (path === "Vehicle.OBD.DTCList" && v.length) || shown === "invalid" || shown === "● REC";
    prevSignals[path] = v;
    return `<dt>${label}</dt><dd class="${bad ? "bad" : changed ? "hot" : ""}">${esc(shown)}</dd>`;
  }).join("");
}

function renderControls(s) {
  const w = s.world, values = {
    fatigue: w.driver.fatigue, hunger: w.driver.hunger, speed: w.signals["Vehicle.Speed"] ?? 0,
    passengers: w.cabin.passengers, weather: w.environment.weather, zone: w.environment.zone,
    gps_ok: w.system.gps_ok, cloud_online: w.system.cloud_online, playing: w.playing,
  };
  document.querySelectorAll("[data-k]").forEach((el) => {
    if (el === document.activeElement) return;
    const v = values[el.dataset.k];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    const out = el.parentElement.querySelector("output");
    if (out) out.textContent = el.value;
  });
  const off = w.cabin.pointing_offset_deg;
  document.querySelectorAll("#pad button").forEach((b) =>
    b.classList.toggle("active", b.dataset.o === (off === null || off === undefined ? "none" : String(off))));
  document.querySelectorAll(".scenario").forEach((el) => el.classList.toggle("active", el.dataset.id === s.scenario));
}

function render(s) {
  state = s;
  renderPills(s); renderMap(s); renderVehicle(s); renderControls(s);
}

async function refreshMemory() {
  const m = await api("/api/memory");
  $("#memory").innerHTML = m.episodes.slice(0, 15).map((e) =>
    `<li><b>${esc(e.kind)}</b> ${esc(e.subject ?? "")} ${e.outcome ? "→ " + esc(e.outcome) : ""}${e.payload.seeded ? " (seed)" : ""}</li>`).join("");
}

// ---------- dialogue ----------
function addEntry(entry) {
  const log = $("#log");
  const div = document.createElement("div");
  if (entry.role === "driver") {
    div.className = "msg driver"; div.textContent = entry.text;
  } else if (entry.role === "event") {
    div.className = "msg event"; div.textContent = `⚡ ${entry.text}`;
  } else {
    const r = entry.reply;
    if (!r.text) {
      div.className = "msg silent";
      div.innerHTML = `Herbie stayed quiet <details><summary>why</summary><ol>${r.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></details>`;
    } else {
      div.className = `msg herbie ${r.proactive ? "proactive" : ""}`;
      const badges = [
        ...r.applied.map((a) => `<span class="badge">✓ ${esc(a.label)}</span>`),
        ...r.blocked.map((b) => `<span class="badge blocked">✕ ${esc(b.label)}: ${esc((b.reasons || []).join(", "))}</span>`),
      ].join("");
      let actions = "";
      if (r.suggestion) {
        const s = r.suggestion, [yes, no] = s.applied ? ["Keep", "Undo"] : ["Yes", "No"];
        actions = `<div class="actions" data-sid="${s.id}"><button class="primary" data-a="1">${yes}</button><button data-a="0">${no}</button></div>`;
      }
      if (r.clarify?.length) actions += `<div class="actions">${r.clarify.map((c) => `<button data-say="${esc(c)}">${esc(c)}</button>`).join("")}</div>`;
      div.innerHTML = `<div class="meta">Herbie · ${esc(r.skill)}${r.intent ? " · " + esc(r.intent) : ""}${r.proactive ? " · proactive" : ""} · ${r.data.latency_ms ?? "?"} ms</div>
        <div>${esc(r.text)}</div>${badges ? `<div class="badges">${badges}</div>` : ""}${actions}
        ${r.trace.length ? `<details><summary>why</summary><ol>${r.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></details>` : ""}`;
      if ($("#tts").checked && "speechSynthesis" in window) speechSynthesis.speak(new SpeechSynthesisUtterance(r.text));
    }
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

$("#log").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.say) return say(b.dataset.say);
  const box = b.closest("[data-sid]");
  if (!box || answered.has(box.dataset.sid)) return;
  answered.add(box.dataset.sid);
  box.querySelectorAll("button").forEach((x) => (x.disabled = true));
  await api("/api/feedback", { id: box.dataset.sid, accept: b.dataset.a === "1" });
  refreshMemory();
});

async function say(text) {
  if (!text.trim()) return;
  await api("/api/say", { text });
  refreshMemory();
}
$("#say").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#text").value; $("#text").value = ""; say(t); });

// Voice input (Chrome/Edge/Safari): "Hey Herbie, what's that building over there?"
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SR) {
  const rec = new SR(); rec.lang = "en-US"; rec.interimResults = false;
  rec.onresult = (e) => say(e.results[0][0].transcript);
  rec.onend = () => $("#mic").classList.remove("listening");
  $("#mic").onclick = () => { $("#mic").classList.add("listening"); rec.start(); };
} else {
  $("#mic").disabled = true; $("#mic").title = "Speech recognition not supported in this browser";
}

// ---------- controls ----------
document.querySelectorAll("[data-k]").forEach((el) => el.addEventListener("change", () => {
  const k = el.dataset.k;
  const v = el.type === "checkbox" ? el.checked : el.type === "range" || el.type === "number" ? Number(el.value) : el.value;
  api("/api/sim", { [k]: v });
}));
document.querySelectorAll("[data-k][type=range]").forEach((el) =>
  el.addEventListener("input", () => (el.parentElement.querySelector("output").textContent = el.value)));
$("#pad").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  api("/api/gesture", { offset_deg: b.dataset.o === "none" ? null : Number(b.dataset.o) });
});
$("#inject").onclick = async () => { await api("/api/dtc", { code: $("#dtc").value }); refreshMemory(); };
$("#cleardtc").onclick = () => api("/api/dtc/clear", {});
$("#trip").onclick = async () => { await api("/api/trip/start", {}); refreshMemory(); };
$("#reset").onclick = async () => {
  if (!confirm("Reset Herbie's memory to the demo seed?")) return;
  await api("/api/reset", {}); $("#log").innerHTML = ""; refreshMemory();
};

api("/api/scenarios").then((list) => {
  $("#scenarios").innerHTML = list.map((s) => `
    <div class="scenario" data-id="${s.id}">
      <b>${esc(s.title)}</b><p>${esc(s.about)}</p><div class="ac">${esc(s.acceptance)}</div>
      ${s.script.length ? `<div class="chips">${s.script.map((l) => `<button class="chip" data-line="${esc(l)}">“${esc(l)}”</button>`).join("")}</div>` : ""}
    </div>`).join("");
});
$("#scenarios").addEventListener("click", async (e) => {
  const chip = e.target.closest("[data-line]");
  if (chip) return say(chip.dataset.line);
  const card = e.target.closest(".scenario");
  if (!card) return;
  $("#log").innerHTML = "";
  await api(`/api/scenario/${card.dataset.id}`, {});
  refreshMemory();
});

// ---------- live updates ----------
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "state") render(msg.state);
    if (msg.type === "log") addEntry(msg.entry);
  };
  ws.onclose = () => setTimeout(connect, 1500);
}
api("/api/log").then((entries) => { entries.forEach(addEntry); connect(); });
refreshMemory();
