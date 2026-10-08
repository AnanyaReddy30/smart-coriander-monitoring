"use strict";

/* Smart Coriander Cultivation and Quality Monitoring System - dashboard.
   Talks only to the Flask API. In this prototype the readings come from a replayed
   dataset; with hardware, the ESP32 posts to /api/readings and this page is unchanged. */

const PARAMS = ["n", "p", "k", "moisture", "temperature", "ph", "ec", "light", "pressure", "wind"];
const READINGS_PER_DAY = 8;
const ROW_LABEL = { n: "Nitrogen", p: "Phosphorus", k: "Potassium", moisture: "Soil moisture", temperature: "Temperature", ph: "Soil pH", ec: "Salinity", light: "Light", pressure: "Air pressure", wind: "Wind" };
const STAGE_TINT = { germination: "#dfe9d0", vegetative: "#c9e0b4", flowering: "#ecdca4", seed: "#dcc383" };
const COLOUR = { ok: "#8fc27a", warn: "#e3b13f", bad: "#cc5340", na: "#dfe5d7", future: "#eef1e8", ink: "#14261a", soft: "#4b5f52", line: "#ccd6c3" };

const state = {
  cfg: null, farms: [], farmId: null, rows: [], latest: null, cursor: 0, total: 0, days: 110,
  playing: false, timer: null, pollTimer: null, live: false, busy: false, chartParam: "n", presetId: null, hoverDay: null,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ API */
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function showError(err) {
  const note = $("modeNote");
  note.textContent = "Something went wrong: " + (err && err.message ? err.message : err);
  note.style.background = "#fbe9e5"; note.style.color = "#8a2414"; note.style.borderColor = "#ecb8ac";
  pause();
}

/* ------------------------------------------------------------ formatting */
function fmt(pid, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  switch (pid) {
    case "light": return Math.round(v).toLocaleString("en-IN");
    case "ph": case "ec": return Number(v).toFixed(2);
    case "n": case "k": return Number(v).toFixed(0);
    default: return Number(v).toFixed(1);
  }
}
function rangeText(e) {
  const u = e.unit ? " " + e.unit : "";
  if (e.min != null && e.max != null) return fmt(e.id, e.min) + "–" + fmt(e.id, e.max) + u;
  if (e.min != null) return "≥ " + fmt(e.id, e.min) + u + (e.id === "light" ? " (daytime)" : "");
  if (e.max != null) return "≤ " + fmt(e.id, e.max) + u;
  return "Informational";
}
function statusWord(pid, status) {
  const nutrient = state.cfg.parameters[pid].kind === "nutrient";
  if (status === "low") return nutrient ? "deficient" : "low";
  if (status === "high") return nutrient ? "in excess" : "high";
  if (status === "watch") return "falling fast";
  if (status === "ok") return "within the ideal range";
  return "not evaluated";
}
function level(status, sev) {
  if (status === undefined) return -1;
  if (status === "ok") return 1;
  if (status === "na" || status === "missing") return 0;
  return sev === "severe" ? 3 : 2;
}
function levelColour(l) { return l === 3 ? COLOUR.bad : l === 2 ? COLOUR.warn : l === 1 ? COLOUR.ok : l === 0 ? COLOUR.na : COLOUR.future; }
function stageForDay(day) {
  const st = state.cfg.stages;
  for (const s of st) if (day >= s.start_day && day <= s.end_day) return s;
  return day < st[0].start_day ? st[0] : st[st.length - 1];
}
function parseTs(ts) { return new Date(ts.replace(" ", "T")); }

/* ------------------------------------------------------- shared analysis UI */
function needHTML(f) {
  const covered = f.kg_per_ha === 0;
  const qty = covered
    ? "No extra urea needed <small>(covered by the DAP above)</small>"
    : "Apply " + esc(f.product) + ": " + f.kg_per_acre + " kg per acre <small>(" + f.kg_per_ha + " kg per hectare)</small>";
  const reading = "Reading " + fmt(f.nutrient.toLowerCase(), f.reading) + " " + esc(f.unit) + "; this stage needs at least " + fmt(f.nutrient.toLowerCase(), f.min) + ". ";
  return '<div class="need"><div class="badge ' + esc(f.severity) + '" aria-hidden="true">' + esc(f.nutrient) + '</div><div>' +
    '<div class="what">' + esc(f.nutrient_label) + " is deficient (" + esc(f.severity) + ")</div>" +
    '<div class="qty">' + qty + "</div>" +
    '<div class="note">' + reading + esc(f.note) + " " + (covered ? "" : esc(f.how)) + "</div></div></div>";
}

function analysisHTML(a) {
  const nutrients = a.parameters.filter((p) => p.kind === "nutrient");
  const lacking = a.fertilizer.length
    ? a.fertilizer.map(needHTML).join("")
    : '<div class="allgood">' + (nutrients.every((n) => n.status === "ok")
      ? "Nitrogen, phosphorus and potassium are all adequate for this stage. No fertilizer needed."
      : "No fertilizer needed right now. See the notes below.") + "</div>";

  const win = a.application_window;
  const winHTML = a.fertilizer.length
    ? '<h3 class="block-title">Can I apply it now?</h3><div class="window ' + esc(win.state) + '">' + esc(win.message) + "</div>" : "";

  const actions = a.actions.length
    ? '<h3 class="block-title">Other things to do in the field</h3><ul class="actions">' +
      a.actions.map((x) => '<li class="' + (x.severity === "severe" ? "severe" : "") + '"><b>' + esc(x.label) + "</b><span>" + esc(x.text) + "</span></li>").join("") + "</ul>"
    : "";

  return '<div class="stagehead"><div><h3 class="stagename">' + esc(a.stage.label) + '</h3><p class="headline">' + esc(a.overall.headline) + "</p></div></div>" +
    '<h3 class="block-title">What is lacking, and what to apply</h3>' + lacking + winHTML + actions;
}

function readingsHTML(a) {
  const rows = a.parameters.map((e) => {
    const cls = e.severity === "severe" ? "severe" : e.status;
    return "<tr><td>" + esc(e.label) + '</td><td class="val">' + fmt(e.id, e.value) + " <small>" + esc(e.unit) + '</small><span class="rng-inline">' + (e.min == null && e.max == null ? "" : "Ideal ") + esc(rangeText(e)) + '</span></td><td class="rng">' + esc(rangeText(e)) +
      '</td><td><span class="chip ' + cls + '">' + esc(e.status_label) + "</span></td></tr>";
  }).join("");
  return '<table class="rd"><thead><tr><th>Parameter</th><th>Value</th><th>Ideal now</th><th>Status</th></tr></thead><tbody>' + rows + "</tbody></table>";
}

function onsiteHTML(a) {
  const leds = a.display.leds.map((c) => '<i class="led ' + c + '"></i>').join("");
  return "<h3>On-site display preview</h3>" +
    '<div class="row"><i class="led big ' + a.display.led + '" title="Overall status"></i><div class="strip" aria-label="Status of each parameter">' + leds + "</div>" +
    '<div class="lcd"><div>' + esc(a.display.lcd[0].padEnd(16)) + "</div><div>" + esc(a.display.lcd[1].padEnd(16)) + "</div></div></div>" +
    '<p class="cap">Mirrors the LED strip and 16×2 LCD in the hardware design.</p>';
}

/* --------------------------------------------------------------- live view */
function renderLive() {
  const rows = state.rows;
  const last = rows.length ? rows[rows.length - 1] : null;
  const a = state.latest ? state.latest.analysis : null;

  if (last) {
    $("dayText").textContent = "Day " + last.day + " of " + state.days;
    $("stageText").textContent = stageForDay(last.day).label;
    $("clockText").textContent = parseTs(last.ts).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    $("readTime").textContent = "Latest reading";
  } else {
    $("dayText").textContent = "Day 0 of " + state.days;
    $("stageText").textContent = state.live ? "No readings yet" : "Nothing replayed yet";
    $("clockText").textContent = state.live ? "Waiting for the first reading from the sensor node." : "Press Play to start, or load the full season.";
    $("readTime").textContent = "";
  }

  if (a) {
    $("nowBody").innerHTML = analysisHTML(a);
    $("readBody").innerHTML = readingsHTML(a);
    $("onsite").innerHTML = onsiteHTML(a);
  } else {
    $("nowBody").innerHTML = '<p class="empty">The recommendation for the current stage will appear here as soon as readings arrive.</p>';
    $("readBody").innerHTML = '<p class="empty">No readings yet.</p>';
    $("onsite").innerHTML = "";
  }

  const btn = $("btnPlay");
  btn.textContent = state.playing ? "Pause" : (state.cursor >= state.total && state.total > 0 ? "Play again" : "Play");
  drawSeason();
  drawTrend();
}

/* ------------------------------------------------------------ season grid */
function buildDayMap() {
  const map = Array.from({ length: state.days }, () => ({}));
  for (const r of state.rows) {
    if (r.day >= state.days) continue;
    for (const pid of PARAMS) {
      const l = level(r.s[pid], r.sv ? r.sv[pid] : null);
      const cur = map[r.day][pid];
      if (!cur || l > cur.level) map[r.day][pid] = { level: l, status: r.s[pid] };
    }
  }
  return map;
}

let seasonGeom = null;
function drawSeason() {
  const canvas = $("season");
  const cssW = canvas.parentElement.clientWidth;
  if (cssW < 50 || !state.cfg) return;
  const dpr = window.devicePixelRatio || 1;
  const wide = cssW >= 560, labelW = wide ? 96 : 54, top = 30, rowH = 22, axisH = 22;
  const H = top + PARAMS.length * rowH + axisH;
  canvas.style.height = H + "px";
  canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, H);

  const days = state.days, cw = (cssW - labelW - 4) / days;
  seasonGeom = { labelW, top, rowH, cw, days };
  const map = buildDayMap();
  ctx.font = "600 12px 'Source Sans 3', system-ui, sans-serif";
  ctx.textBaseline = "middle";

  // stage ribbon
  for (const s of state.cfg.stages) {
    const x0 = labelW + s.start_day * cw, x1 = labelW + Math.min(s.end_day + 1, days) * cw;
    ctx.fillStyle = STAGE_TINT[s.id]; ctx.fillRect(x0, 2, x1 - x0 - 1, top - 8);
    ctx.fillStyle = COLOUR.ink; ctx.textAlign = "center";
    const text = (x1 - x0) > 150 ? s.label : (x1 - x0) > 60 ? s.short : "";
    if (text) ctx.fillText(text, (x0 + x1) / 2, 2 + (top - 8) / 2);
  }

  // cells
  PARAMS.forEach((pid, i) => {
    const y = top + i * rowH;
    ctx.fillStyle = COLOUR.ink; ctx.textAlign = "right";
    ctx.fillText(wide ? ROW_LABEL[pid] : state.cfg.parameters[pid].short, labelW - 8, y + rowH / 2);
    for (let d = 0; d < days; d++) {
      const cell = map[d][pid];
      ctx.fillStyle = levelColour(cell ? cell.level : -1);
      ctx.fillRect(labelW + d * cw, y + 1, Math.max(1, cw - (cw > 4 ? 1 : 0)), rowH - 2);
    }
  });

  // stage boundaries
  ctx.strokeStyle = "rgba(20,38,26,.35)"; ctx.lineWidth = 1;
  for (const s of state.cfg.stages.slice(1)) {
    const x = Math.round(labelW + s.start_day * cw) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, top - 4); ctx.lineTo(x, top + PARAMS.length * rowH); ctx.stroke();
  }

  // axis
  ctx.fillStyle = COLOUR.soft; ctx.textAlign = "center";
  for (let d = 0; d <= days; d += 10) {
    const x = labelW + Math.min(d, days) * cw;
    ctx.fillText(String(d), Math.min(x, cssW - 10), top + PARAMS.length * rowH + axisH / 2);
  }

  // hover column
  if (state.hoverDay !== null) {
    ctx.strokeStyle = COLOUR.ink; ctx.lineWidth = 1.5;
    ctx.strokeRect(labelW + state.hoverDay * cw, top - 2, Math.max(cw, 2), PARAMS.length * rowH + 4);
  }

  // playhead
  if (state.rows.length) {
    const day = state.rows[state.rows.length - 1].day;
    const x = labelW + (day + 0.5) * cw;
    ctx.strokeStyle = COLOUR.ink; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, top - 3); ctx.lineTo(x, top + PARAMS.length * rowH + 2); ctx.stroke();
    ctx.fillStyle = COLOUR.ink;
    ctx.beginPath(); ctx.moveTo(x - 5, top - 6); ctx.lineTo(x + 5, top - 6); ctx.lineTo(x, top); ctx.closePath(); ctx.fill();
  }
  seasonGeom.map = map;
}

function seasonPoint(evt) {
  if (!seasonGeom) return null;
  const rect = $("season").getBoundingClientRect();
  const x = evt.clientX - rect.left, y = evt.clientY - rect.top;
  const day = Math.floor((x - seasonGeom.labelW) / seasonGeom.cw);
  const row = Math.floor((y - seasonGeom.top) / seasonGeom.rowH);
  if (day < 0 || day >= seasonGeom.days) return null;
  return { day, row: row >= 0 && row < PARAMS.length ? row : null };
}

function seasonHover(evt) {
  const pt = seasonPoint(evt);
  const el = $("seasonHover");
  if (!pt) { state.hoverDay = null; el.textContent = "Point at the grid for details."; drawSeason(); return; }
  state.hoverDay = pt.day;
  const stage = stageForDay(pt.day).label;
  const cells = seasonGeom.map[pt.day];
  let text;
  if (pt.row !== null) {
    const pid = PARAMS[pt.row], cell = cells[pid];
    text = "Day " + pt.day + " (" + stage + "), " + state.cfg.parameters[pid].label + ": " + (cell ? statusWord(pid, cell.status) : "not replayed yet");
  } else {
    const bad = PARAMS.filter((p) => cells[p] && cells[p].level >= 2).map((p) => state.cfg.parameters[p].short + " " + statusWord(p, cells[p].status));
    const seen = PARAMS.some((p) => cells[p]);
    text = "Day " + pt.day + " (" + stage + "): " + (!seen ? "not replayed yet" : bad.length ? bad.join(", ") : "everything within the ideal range");
  }
  el.textContent = text;
  drawSeason();
}

/* ------------------------------------------------------------ trend chart */
function drawTrend() {
  const canvas = $("trendChart");
  const cssW = canvas.parentElement.clientWidth;
  if (cssW < 50 || !state.cfg) return;
  const dpr = window.devicePixelRatio || 1, H = 240;
  canvas.style.height = H + "px";
  canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, H);

  const pid = state.chartParam, pcfg = state.cfg.parameters[pid];
  const padL = 56, padR = 12, padT = 10, padB = 26;
  const w = cssW - padL - padR, h = H - padT - padB, days = state.days;

  const pts = state.rows.filter((r) => r.v[pid] !== null && r.v[pid] !== undefined)
    .map((r) => ({ x: r.day + parseTs(r.ts).getHours() / 24, y: r.v[pid], s: r.s[pid], sv: r.sv ? r.sv[pid] : null }));
  const mins = [], maxs = [];
  for (const s of state.cfg.stages) { const rg = pcfg.ranges[s.id]; if (rg.min != null) mins.push(rg.min); if (rg.max != null) maxs.push(rg.max); }
  const dataVals = pts.map((p) => p.y);
  const base = dataVals.concat(mins);
  let lo = base.length ? Math.min(...base) : (maxs.length ? Math.min(...maxs) * 0.5 : 0);
  let hi = base.length ? Math.max(...base) : (maxs.length ? Math.max(...maxs) : 1);
  for (const m of maxs) if (m <= hi * 1.35 || !dataVals.length) hi = Math.max(hi, m);
  if (hi === lo) hi = lo + 1;
  const span = hi - lo; lo -= span * 0.08; hi += span * 0.08;
  if (lo < 0 && pid !== "temperature") lo = 0;

  const X = (d) => padL + (d / days) * w;
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * h;

  // ideal bands per stage
  for (const s of state.cfg.stages) {
    const rg = pcfg.ranges[s.id];
    if (rg.min == null && rg.max == null) continue;
    const yTop = rg.max != null ? Math.max(padT, Y(rg.max)) : padT;
    const yBot = rg.min != null ? Math.min(padT + h, Y(rg.min)) : padT + h;
    const x0 = X(s.start_day), x1 = X(Math.min(s.end_day + 1, days));
    ctx.fillStyle = "rgba(107,170,84,.20)"; ctx.fillRect(x0, yTop, x1 - x0, Math.max(0, yBot - yTop));
    ctx.strokeStyle = "rgba(44,122,63,.55)"; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
    ctx.beginPath();
    if (rg.max != null && yTop > padT) { ctx.moveTo(x0, yTop); ctx.lineTo(x1, yTop); }
    if (rg.min != null && yBot < padT + h) { ctx.moveTo(x0, yBot); ctx.lineTo(x1, yBot); }
    ctx.stroke(); ctx.setLineDash([]);
  }

  // grid and axes
  ctx.font = "12px 'Source Sans 3', system-ui, sans-serif"; ctx.fillStyle = COLOUR.soft; ctx.strokeStyle = "rgba(20,38,26,.10)"; ctx.lineWidth = 1;
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + w, y); ctx.stroke();
    ctx.fillText(fmt(pid, v), padL - 6, y);
  }
  ctx.textAlign = "center";
  for (let d = 0; d <= days; d += 10) ctx.fillText(String(d), X(d), padT + h + 14);
  ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillText(pcfg.label + (pcfg.unit ? " (" + pcfg.unit + ")" : "") + " by day of crop", padL + 6, padT + 4);
  ctx.textBaseline = "middle";

  // stage boundaries
  ctx.strokeStyle = "rgba(20,38,26,.28)";
  for (const s of state.cfg.stages.slice(1)) { const x = X(s.start_day); ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + h); ctx.stroke(); }

  if (!pts.length) {
    ctx.fillStyle = COLOUR.soft; ctx.textAlign = "center"; ctx.fillText("No readings yet. Press Play.", padL + w / 2, padT + h / 2);
    return;
  }

  // line then out-of-range points
  ctx.strokeStyle = COLOUR.ink; ctx.lineWidth = 1.2; ctx.beginPath();
  pts.forEach((p, i) => { const x = X(p.x), y = Y(p.y); if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
  ctx.stroke();
  for (const p of pts) {
    const l = level(p.s, p.sv);
    if (l >= 2) { ctx.fillStyle = l === 3 ? COLOUR.bad : COLOUR.warn; ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 2.6, 0, Math.PI * 2); ctx.fill(); }
  }
  const lastPt = pts[pts.length - 1];
  ctx.strokeStyle = COLOUR.ink; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(X(lastPt.x), padT); ctx.lineTo(X(lastPt.x), padT + h); ctx.stroke();
}

/* --------------------------------------------------------------- replay */
function applyReplay(data) {
  if (data.replace) state.rows = data.added || data.rows || [];
  else state.rows = state.rows.concat(data.added || []);
  state.latest = data.latest || null;
  state.cursor = data.cursor; state.total = data.total || state.total;
  if (state.total) state.days = Math.max(1, Math.round(state.total / READINGS_PER_DAY));
  renderLive();
}

async function loadHistory() {
  const data = await api("/api/history?farm_id=" + encodeURIComponent(state.farmId));
  data.replace = true;
  applyReplay(data);
}

async function step() {
  if (state.busy) return;
  state.busy = true;
  try {
    const n = parseInt($("speed").value, 10) || 1;
    const data = await api("/api/replay/step", { farm_id: state.farmId, steps: n });
    applyReplay(data);
    if (data.finished) pause();
  } catch (e) { showError(e); }
  state.busy = false;
}
function play() {
  if (state.playing) return;
  if (state.cursor >= state.total && state.total > 0) { restart().then(play); return; }
  state.playing = true;
  state.timer = setInterval(step, 300);
  renderLive();
}
function pause() {
  state.playing = false;
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.cfg) renderLive();
}
async function seek(day) {
  pause();
  try { applyReplay(await api("/api/replay/seek", { farm_id: state.farmId, day })); } catch (e) { showError(e); }
}
async function restart() {
  pause();
  try { applyReplay(await api("/api/replay/reset", { farm_id: state.farmId })); } catch (e) { showError(e); }
}

function defaultDay(farm) {
  const ev = farm.events && farm.events[0];
  return ev ? Math.min(ev.from_day + 14, state.days - 1) : 44;
}

async function selectFarm(id, preload) {
  pause();
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.farmId = id;
  const farm = state.farms.find((f) => f.id === id);
  // A farm with no dataset rows is a real sensor node: no replay controls, poll for new readings instead.
  state.live = farm.total_readings === 0;
  state.total = farm.total_readings;
  state.days = state.live ? state.cfg.stages[state.cfg.stages.length - 1].end_day : Math.round(farm.total_readings / READINGS_PER_DAY);
  $("transport").hidden = state.live;
  $("modeNote").textContent = state.live ? "Live: showing readings sent by the sensor node." : "Replaying a dataset. No hardware connected.";
  document.querySelectorAll("#farmPick button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.id === id)));
  $("farmDesc").textContent = farm.description;
  await loadHistory();
  if (state.live) state.pollTimer = setInterval(() => loadHistory().catch(() => {}), 5000);
  else if (preload && !state.rows.length) await seek(defaultDay(farm));
}

function buildFarmPicker() {
  const box = $("farmPick");
  box.innerHTML = "";
  for (const f of state.farms) {
    const b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "radio"); b.setAttribute("aria-checked", "false"); b.dataset.id = f.id;
    b.textContent = f.name.replace(" - ", ": ");
    b.addEventListener("click", () => selectFarm(f.id, true).catch(showError));
    box.appendChild(b);
  }
}

function buildParamTabs() {
  const box = $("paramTabs");
  box.innerHTML = "";
  for (const pid of PARAMS) {
    const b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(pid === state.chartParam));
    b.textContent = state.cfg.parameters[pid].short === "Moist" ? "Moisture" : state.cfg.parameters[pid].short;
    b.addEventListener("click", () => {
      state.chartParam = pid;
      box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      drawTrend();
    });
    box.appendChild(b);
  }
}

/* ------------------------------------------------------------ test cases */
const BASE = { n: 150, p: 9, k: 80, moisture: 30, temperature: 22, ph: 6.9, ec: 0.6, light: 70000, pressure: 955, wind: 2 };
const findN = (a, nut) => a.fertilizer.find((f) => f.nutrient === nut);
const PRESETS = [
  { id: "ok", label: "All optimal", day: 40, hour: 12, pd: 0, set: {}, expect: "No fertilizer needed and a green LED.",
    check: (a) => a.fertilizer.length === 0 && a.display.led === "green" },
  { id: "n_low", label: "Nitrogen low (vegetative)", day: 40, hour: 12, pd: 0, set: { n: 95 }, expect: "Urea recommended for nitrogen only.",
    check: (a) => a.fertilizer.length === 1 && findN(a, "N") && findN(a, "N").product_id === "urea" && findN(a, "N").kg_per_ha > 0 },
  { id: "n_severe", label: "Severe nitrogen shortage", day: 40, hour: 12, pd: 0, set: { n: 55 }, expect: "Larger urea dose and a red LED.",
    check: (a) => findN(a, "N") && findN(a, "N").severity === "severe" && a.display.led === "red" },
  { id: "p_low", label: "Phosphorus low (germination)", day: 10, hour: 12, pd: 0, set: { p: 5 }, expect: "DAP recommended for phosphorus.",
    check: (a) => a.fertilizer.length === 1 && findN(a, "P") && findN(a, "P").product_id === "dap" },
  { id: "k_low", label: "Potassium low (flowering)", day: 60, hour: 12, pd: 0, set: { k: 45 }, expect: "Muriate of potash recommended for potassium.",
    check: (a) => a.fertilizer.length === 1 && findN(a, "K") && findN(a, "K").product_id === "mop" },
  { id: "np_low", label: "Nitrogen and phosphorus low", day: 40, hour: 12, pd: 0, set: { n: 95, p: 4 }, expect: "DAP for phosphorus, plus a smaller urea dose because DAP also supplies nitrogen.",
    check: (a) => findN(a, "P") && findN(a, "N") && findN(a, "P").product_id === "dap" && findN(a, "N").kg_per_ha < 65 },
  { id: "n_high", label: "Nitrogen in excess", day: 40, hour: 12, pd: 0, set: { n: 300 }, expect: "No nitrogen fertilizer, with advice to skip nitrogen.",
    check: (a) => !findN(a, "N") && a.parameters[0].status === "high" && a.actions.some((x) => x.parameter === "n") },
  { id: "k_salt", label: "Potassium low in salty soil", day: 60, hour: 12, pd: 0, set: { k: 45, ec: 1.8 }, expect: "Sulphate of potash instead of muriate of potash, plus a salinity warning.",
    check: (a) => findN(a, "K") && findN(a, "K").product_id === "sop" && a.actions.some((x) => x.parameter === "ec") },
  { id: "heat", label: "Heat stress at flowering", day: 70, hour: 14, pd: 0, set: { temperature: 36 }, expect: "A heat stress action and an amber or red LED.",
    check: (a) => a.parameters[4].status === "high" && a.display.led !== "green" },
  { id: "dry", label: "Dry soil, nitrogen low", day: 40, hour: 12, pd: 0, set: { n: 95, moisture: 15 }, expect: "Apply urea, then irrigate lightly afterwards.",
    check: (a) => a.application_window.state === "irrigate" },
  { id: "wet", label: "Waterlogged, nitrogen low", day: 40, hour: 12, pd: 0, set: { n: 95, moisture: 46 }, expect: "Urea is still recommended, but application is delayed.",
    check: (a) => a.application_window.state === "delay" && a.fertilizer.length === 1 },
  { id: "storm", label: "Storm approaching", day: 40, hour: 15, pd: -3.5, set: { n: 95, wind: 9.5, pressure: 950 }, expect: "Application is delayed because of strong wind and falling pressure.",
    check: (a) => a.application_window.state === "delay" && a.parameters[8].status === "watch" },
  { id: "night", label: "Night-time reading", day: 40, hour: 0, pd: 0, set: { light: 0 }, expect: "Light is not judged at night, so nothing is flagged.",
    check: (a) => a.parameters[7].status === "na" && a.display.led === "green" },
];

function buildTestForm() {
  const box = $("f_params");
  box.innerHTML = "";
  for (const pid of PARAMS) {
    const p = state.cfg.parameters[pid];
    const step = { ph: 0.1, ec: 0.1, p: 0.5, wind: 0.5, moisture: 0.5, temperature: 0.5, pressure: 0.5, light: 1000, n: 1, k: 1 }[pid];
    const label = document.createElement("label");
    label.innerHTML = esc(p.label) + (p.unit ? " (" + esc(p.unit) + ")" : "") + '<input type="number" id="f_' + pid + '" step="' + step + '" value="' + BASE[pid] + '">';
    box.appendChild(label);
  }
  const pbox = $("presets");
  pbox.innerHTML = "";
  for (const pr of PRESETS) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = pr.label; b.dataset.id = pr.id; b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => applyPreset(pr));
    pbox.appendChild(b);
  }
  $("testForm").addEventListener("input", () => {
    state.presetId = null;
    pbox.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", "false"));
  });
  $("testForm").addEventListener("submit", (e) => { e.preventDefault(); runTest().catch(showError); });
}

function applyPreset(pr) {
  const vals = Object.assign({}, BASE, pr.set);
  for (const pid of PARAMS) $("f_" + pid).value = vals[pid];
  $("f_day").value = pr.day; $("f_hour").value = pr.hour; $("f_pd").value = pr.pd;
  runTest(pr).catch(showError);
  document.querySelectorAll("#presets button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.id === pr.id)));
  state.presetId = pr.id;
}

async function runTest(preset) {
  const reading = {};
  for (const pid of PARAMS) { const v = $("f_" + pid).value; reading[pid] = v === "" ? null : Number(v); }
  const a = await api("/api/analyze", { reading, day: Number($("f_day").value), hour: Number($("f_hour").value), pressure_delta: Number($("f_pd").value) });
  const expectBox = $("testExpect");
  if (preset) {
    const pass = !!preset.check(a);
    expectBox.innerHTML = '<div class="expect ' + (pass ? "pass" : "fail") + '">' + (pass ? "Matches the expected outcome" : "Does not match the expected outcome") +
      "<small>Expected: " + esc(preset.expect) + "</small></div>";
  } else { expectBox.innerHTML = ""; }
  $("testBody").innerHTML = analysisHTML(a) + '<div class="block-title">Sensor readings</div>' + readingsHTML(a) + '<div class="onsite">' + onsiteHTML(a) + "</div>";
}

/* ---------------------------------------------------------- ranges page */
function buildRanges() {
  const cfg = state.cfg;
  $("rangesNote").textContent = cfg.meta.status_note;
  let html = "<thead><tr><th>Parameter</th>" + cfg.stages.map((s) => "<th>" + esc(s.label) + "<br><small>days " + s.start_day + "–" + s.end_day + "</small></th>").join("") + "</tr></thead><tbody>";
  for (const pid of PARAMS) {
    const p = cfg.parameters[pid];
    html += '<tr><td class="p">' + esc(p.label) + (p.unit ? " (" + esc(p.unit) + ")" : "") + (p.verify ? '<span class="verify">verify</span>' : "") + "</td>";
    for (const s of cfg.stages) {
      const rg = p.ranges[s.id];
      html += "<td>" + esc(rangeText({ id: pid, unit: "", min: rg.min, max: rg.max })) + "</td>";
    }
    html += "</tr>";
  }
  $("rangesTable").innerHTML = html + "</tbody>";

  const plan = cfg.fertilizer_plan, sev = cfg.severity;
  const dose = (nut, m) => (plan.recommended_total_kg_ha[nut] * plan.corrective_fraction_of_total[nut] * plan.severity_multiplier[m]).toFixed(1).replace(/\.0$/, "");
  const nutRows = ["N", "P2O5", "K2O"].map((n) => "<tr><td>" + n + "</td><td>" + plan.recommended_total_kg_ha[n] + "</td><td>" + dose(n, "mild") + "</td><td>" + dose(n, "moderate") + "</td><td>" + dose(n, "severe") + "</td></tr>").join("");
  const prodRows = Object.values(cfg.products).map((p) => "<tr><td>" + esc(p.name) + "</td><td>" + [p.N ? Math.round(p.N * 100) + "% N" : "", p.P2O5 ? Math.round(p.P2O5 * 100) + "% P₂O₅" : "", p.K2O ? Math.round(p.K2O * 100) + "% K₂O" : ""].filter(Boolean).join(", ") + "</td></tr>").join("");
  $("methodBody").innerHTML =
    "<ol>" +
    "<li>The growth stage comes from the sowing date and the day count.</li>" +
    "<li>Each reading is compared with that stage's ideal range and marked low, optimal or high.</li>" +
    "<li>How far a reading is outside its range sets the severity: mild up to " + Math.round(sev.mild_max_fraction * 100) + "%, moderate up to " + Math.round(sev.moderate_max_fraction * 100) + "%, severe beyond that.</li>" +
    "<li>For a deficient nutrient, one correction = recommended total dose × corrective fraction × severity multiplier.</li>" +
    "<li>The nutrient dose is converted to a product quantity using the fertilizer's nutrient content.</li>" +
    "<li>Application is delayed when the soil is waterlogged, the wind is strong or pressure is falling fast.</li></ol>" +
    "<h3>Correction doses (kg of nutrient per hectare)</h3>" +
    "<table><thead><tr><th>Nutrient</th><th>Recommended total</th><th>Mild</th><th>Moderate</th><th>Severe</th></tr></thead><tbody>" + nutRows + "</tbody></table>" +
    "<p>" + esc(plan.recommended_total_source) + " " + esc(plan.method_note) + "</p>" +
    "<h3>Fertilizer products</h3><table><tbody>" + prodRows + "</tbody></table>" +
    "<p>Rules used when choosing: DAP also supplies nitrogen, so the urea dose is reduced; single super phosphate replaces DAP when nitrogen is already high; sulphate of potash replaces muriate of potash when salinity is high.</p>" +
    "<h3>Sources</h3><ul>" + cfg.sources.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ul>";
}

/* ------------------------------------------------------------------ tabs */
function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => { t.classList.toggle("is-active", t === tab); t.setAttribute("aria-selected", String(t === tab)); });
      for (const id of ["tab-live", "tab-tests", "tab-ranges"]) $(id).hidden = tab.getAttribute("aria-controls") !== id;
      if (tab.id === "t-live") renderLive(); else pause();
    });
  });
}

/* ------------------------------------------------------------------ init */
async function init() {
  setupTabs();
  $("btnPlay").addEventListener("click", () => (state.playing ? pause() : play()));
  $("btnEnd").addEventListener("click", () => seek(state.days - 1));
  $("btnRestart").addEventListener("click", () => restart());
  const season = $("season");
  season.addEventListener("mousemove", seasonHover);
  season.addEventListener("mouseleave", () => { state.hoverDay = null; $("seasonHover").textContent = "Point at the grid for details."; drawSeason(); });
  season.addEventListener("click", (e) => { const pt = seasonPoint(e); if (pt) seek(pt.day); });
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (!$("tab-live").hidden) renderLive(); }, 120); });

  try {
    state.cfg = await api("/api/config");
    state.farms = await api("/api/farms");
    buildFarmPicker(); buildParamTabs(); buildTestForm(); buildRanges();
    const start = state.farms.find((f) => f.id === "farm_b") || state.farms[0];
    await selectFarm(start.id, true);
  } catch (e) { showError(e); }
}
init();
