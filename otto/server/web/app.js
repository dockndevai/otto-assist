// Orrin dashboard: sidebar + hero/widgets + right Chat drawer + listening & status bars.
const $ = (id) => document.getElementById(id);
const appEl = $("app");
const canvasEl = $("canvas");
const transcriptEl = $("transcript");
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const postJSON = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

let ws, partialUser = null, assistantBubble = null;
let view = "home", replaying = false;
let lastPreflight = null, cfg = {}, lastWorkspaces = [], projName = "—", chatName = "New chat";
let focusedWs = null; const wins = {};
const curKey = () => focusedWs || "__main__";
function win(k) { return wins[k] || (wins[k] = { msgs: [], cur: null, canvas: null, unread: false }); }
function keyOf(ev) { return ev.ws || focusedWs || "__main__"; }

// ---- Theme ----
function currentTheme() { const f = document.documentElement.getAttribute("data-theme"); if (f === "light" || f === "dark") return f;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"; }
function applyThemeIcon() { const b = $("themeToggle"); if (!b) return; const t = currentTheme(); b.textContent = t === "light" ? "☀️" : "🌙"; b.title = t === "light" ? "Switch to dark" : "Switch to light"; }
applyThemeIcon();
if (window.matchMedia) { try { window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyThemeIcon); } catch (_) {} }
$("themeToggle").onclick = () => { const n = currentTheme() === "light" ? "dark" : "light"; document.documentElement.setAttribute("data-theme", n); try { localStorage.setItem("otto-theme", n); } catch (_) {} applyThemeIcon(); };

// ---- Mood ----
let moodOverride = null; const MOODS = ["good", "info", "warn", "critical"];
function baseMood() { return appEl.classList.contains("is-active") ? "info" : "good"; }
function applyMood() { const now = Date.now(); if (moodOverride && moodOverride.until < now) moodOverride = null;
  const m = (moodOverride && moodOverride.level) || baseMood(); MOODS.forEach((x) => appEl.classList.toggle("mood-" + x, x === m)); }
function setMood(level, ms) { if (!MOODS.includes(level)) level = "info"; moodOverride = { level, until: Date.now() + (ms || 8000) }; applyMood(); }
setInterval(applyMood, 1500);

// ---- Nav / views / chat drawer / hamburger ----
const MAIN_VIEWS = ["home", "visuals", "skills", "mcp", "settings"];
function showView(name) {
  if (name === "convo") { openChat(); return; }
  view = name;
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.dataset.view !== name));
  updateNavActive();
  const nav = document.querySelector(`.navbtn[data-nav="${name}"]`); if (nav) nav.classList.remove("unread");
  if (name === "home") renderAtlas();
  if (name === "settings") { loadConfig(); loadProjects(); loadVoices(); }
  if (window.innerWidth <= 820) appEl.classList.remove("nav-open");
}
function updateNavActive() {
  document.querySelectorAll(".navbtn").forEach((b) => b.classList.toggle("active",
    b.dataset.nav === view || (b.dataset.nav === "convo" && appEl.classList.contains("chat-open"))));
}
document.querySelectorAll(".navbtn").forEach((b) => (b.onclick = () => showView(b.dataset.nav)));
function markNavUnread(name) { const on = name === "convo" ? appEl.classList.contains("chat-open") : view === name;
  if (on) return; const nav = document.querySelector(`.navbtn[data-nav="${name}"]`); if (nav) nav.classList.add("unread"); }
function openChat() { appEl.classList.add("chat-open"); updateNavActive(); document.querySelector('.navbtn[data-nav="convo"]').classList.remove("unread"); scroll(); }
function closeChat() { appEl.classList.remove("chat-open"); updateNavActive(); }
$("chatToggle").onclick = () => appEl.classList.contains("chat-open") ? closeChat() : openChat();
$("chatClose").onclick = closeChat;
$("hamburger").onclick = () => appEl.classList.toggle("nav-open");
$("scrim").onclick = () => appEl.classList.remove("nav-open");

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setStatus("idle", "Ready");
  ws.onclose = () => { setStatus("idle", "reconnecting…"); setTimeout(connect, 1000); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function setStatus(state, text) {
  const M = { listening: "Listening…", thinking: "Thinking…", working: "Working…", speaking: "Speaking…" };
  const label = M[state] || "Ready";
  const setT = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  setT("heroState", label); setT("talkState", label); setT("lbState", label);
  setT("lbSub", state === "listening" ? "tap to stop" : state ? "one moment…" : "say “Otto” or click Talk");
  const dot = $("talkDot"); if (dot) dot.style.background = state === "listening" ? "var(--cyan)" : state === "speaking" ? "var(--purple)" : (state === "thinking" || state === "working") ? "var(--warn)" : "var(--ok)";
  ["state-listening", "state-thinking", "state-speaking", "is-active"].forEach((c) => appEl.classList.remove(c));
  if (state === "listening") appEl.classList.add("state-listening", "is-active");
  else if (state === "speaking") appEl.classList.add("state-speaking", "is-active");
  else if (state === "thinking" || state === "working") appEl.classList.add("state-thinking", "is-active");
  if (state === "listening") setCaption("🎙️ Listening…", "hint"); else if (state === "idle") setCaption("", "");
  applyMood(); renderStats();
}

// ---- Caption ----
let captionHideTimer = null;
function setCaption(text, cls) { const el = $("caption"); if (!el) return; clearTimeout(captionHideTimer);
  if (!text) { el.hidden = true; el.textContent = ""; el.className = "caption"; return; }
  el.hidden = false; el.textContent = text; el.className = "caption" + (cls ? " " + cls : ""); }

function fmtTime() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function timeAgo(sec) { if (!sec) return ""; const d = Math.max(0, Date.now() / 1000 - sec);
  if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + "m ago"; if (d < 86400) return Math.floor(d / 3600) + "h ago"; return Math.floor(d / 86400) + "d ago"; }
function setTopic(t) { const el = $("homeTopic"); if (el) el.textContent = t ? "“" + t + "”" : "How can I help you today?"; }

// ---- Transcript (chat drawer) ----
function mkMsg(role, text) { const el = document.createElement("div"); el.className = "msg " + role;
  const who = document.createElement("div"); who.className = "who"; const b = document.createElement("b"); b.textContent = role.includes("user") ? "You" : "Orrin";
  const t = document.createElement("span"); t.className = "t"; t.textContent = fmtTime(); who.append(b, t);
  const body = document.createElement("div"); body.textContent = text; el.append(who, body); el._body = body; return el; }
function mkTool(name, detail) { const el = document.createElement("div"); el.className = "tool"; el.innerHTML = `<b>${escapeHtml(name)}</b>${detail ? " · " + escapeHtml(detail) : ""}`; return el; }
function bubble(role, text) { const el = mkMsg(role, text); transcriptEl.appendChild(el); scroll(); markNavUnread("convo"); return el; }
function scroll() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }
function renderWindow(k) {
  transcriptEl.innerHTML = ""; partialUser = null; assistantBubble = null; const w = win(k);
  for (const m of w.msgs) { if (m.role === "tool") transcriptEl.appendChild(mkTool(m.name, m.detail));
    else if (m.role === "notify") transcriptEl.appendChild(mkMsg("assistant", `🔔 ${m.title}${m.body ? " — " + m.body : ""}`));
    else transcriptEl.appendChild(mkMsg(m.role, m.text)); }
  if (w.cur) assistantBubble = transcriptEl.appendChild(mkMsg("assistant", w.cur)); scroll();
  if (w.canvas) showCanvas(w.canvas.content, w.canvas.format); else hideCanvas();
}
function setFocused(path, name) {
  if (name) { projName = name; const el = $("heroProject"); if (el) el.textContent = "📁 " + name; renderStats(); }
  if (!path || path === focusedWs) return;
  if (focusedWs === null && wins["__main__"] && !wins[path]) { wins[path] = wins["__main__"]; delete wins["__main__"]; }
  focusedWs = path; win(path).unread = false; renderWindow(path);
  hydrate(path);   // pull this project's stored history if we haven't loaded it this session
}

// Fill a window from the per-project stored history the first time it's focused (e.g. a project
// worked on in a previous session, whose messages aren't in memory yet). Idempotent per window.
async function hydrate(k) {
  const w = win(k);
  if (w.loaded || w.msgs.length || k === "__main__") { w.loaded = true; return; }
  w.loaded = true;
  try {
    const { events } = await (await fetch("/api/history?project=" + encodeURIComponent(k))).json();
    for (const ev of (events || [])) {
      if (ev.type === "transcript_final") w.msgs.push({ role: ev.role, text: ev.text });
      else if (ev.type === "canvas_render") w.canvas = { content: ev.content, format: ev.format };
    }
    if (k === curKey()) renderWindow(k);   // repaint only if it's still the focused window
  } catch (_) {}
}

function handle(ev) {
  switch (ev.type) {
    case "status": setStatus(ev.state, ev.text); return;
    case "history_start": { replaying = true; const w = win(curKey()); w.msgs = []; w.cur = null; transcriptEl.innerHTML = ""; partialUser = assistantBubble = null; return; }
    case "history_end": { replaying = false; const d = document.createElement("div"); d.className = "divider"; d.textContent = "— now —"; transcriptEl.appendChild(d); scroll(); return; }
    case "ask_user": openChat(); $("input").focus(); setMood("warn", 12000); return;
    case "project": setFocused(ev.path, ev.name || ev.path); if (view === "home") renderAtlas(); return;
    case "workspaces": setFocused(ev.focused); lastWorkspaces = ev.items || lastWorkspaces; renderStats(); if (view === "home") renderAtlas(); return;
    case "log": ingestLog(ev); return;
    case "preflight": renderPreflight(ev); return;
    case "mood": setMood(ev.level, 25000); return;
    case "session": { chatName = ev.title || "New chat"; renderStats(); if (view === "home") renderAtlas();
      if (appEl.classList.contains("chat-open") && !$("chatMgr").hidden) (chatScope === "all") ? loadAllSessions($("chatSearch").value) : loadSessions(); return; }
    case "canvas_render": { const k = keyOf(ev), w = win(k); w.canvas = { content: ev.content, format: ev.format };
      if (k === curKey()) { showCanvas(ev.content, ev.format); markNavUnread("visuals"); } else { w.unread = true; } return; }
    case "notify": { win(curKey()).msgs.push({ role: "notify", title: ev.title, body: ev.body });
      bubble("assistant", `🔔 ${ev.title}${ev.body ? " — " + ev.body : ""}`); if (ALERT_RE.test((ev.title || "") + " " + (ev.body || ""))) setMood("critical", 8000); return; }
    case "transcript_partial": case "transcript_final": case "assistant_delta": case "tool_use": ingestMsg(ev); return;
  }
}
const ALERT_RE = /\b(error|errors|failed|failure|fail|crash|crashed|critical|broken|denied|urgent|cannot|can't|down|exception|traceback)\b/i;

function ingestMsg(ev) {
  const k = keyOf(ev), w = win(k), live = k === curKey();
  switch (ev.type) {
    case "transcript_partial":
      if (ev.role === "user" && live) { if (!partialUser) partialUser = bubble("user partial", ""); partialUser._body.textContent = ev.text; scroll(); setCaption(ev.text, "user"); setTopic(ev.text); }
      return;
    case "transcript_final":
      if (ev.role === "user") { w.msgs.push({ role: "user", text: ev.text });
        if (live) { if (partialUser) { partialUser.className = "msg user"; partialUser._body.textContent = ev.text; partialUser = null; } else bubble("user", ev.text);
          setCaption(ev.text, "user"); captionHideTimer = setTimeout(() => setCaption("", ""), 2600); setTopic(ev.text); } }
      else { const text = ev.text || w.cur || ""; w.cur = null; w.msgs.push({ role: "assistant", text });
        if (live) { if (assistantBubble) { assistantBubble._body.textContent = text; assistantBubble = null; } else bubble("assistant", text); if (ALERT_RE.test(text)) setMood("warn", 7000); setCompSay(text); } }
      break;
    case "assistant_delta": w.cur = (w.cur || "") + ev.text;
      if (live) { if (!assistantBubble) assistantBubble = bubble("assistant", ""); assistantBubble._body.textContent = w.cur; scroll(); } break;
    case "tool_use": w.msgs.push({ role: "tool", name: ev.name, detail: ev.detail });
      if (live) { transcriptEl.appendChild(mkTool(ev.name, ev.detail)); scroll(); markNavUnread("convo"); } break;
  }
  if (!live) { w.unread = true; }
}

// ---- Home grid (only cards that have data) ----
const QUICK = [
  { i: "✉️", t: "Summarize unread emails", p: "Summarize my unread emails." },
  { i: "🗓", t: "Show today's agenda", p: "Show me my agenda for today." },
  { i: "📁", t: "Open my recent files", p: "List the files I've recently worked on." },
  { i: "📣", t: "Run my daily briefing", p: "Give me my daily briefing." },
];
async function renderWeather() {
  const hero = $("hero"); if (!hero) return;
  let w = null; try { w = (await (await fetch("/api/weather")).json()).weather; } catch (_) {}
  if (!w) {   // fallback: greeting/state hero (no empty box)
    hero.className = "hero hero-fallback";
    hero.innerHTML = `<div class="hero-body"><div class="hero-state-big">${escapeHtml(greeting())}</div>` +
      `<div class="hero-topic">How can I help you today?</div></div>`;
    return;
  }
  hero.className = "hero weather cond-" + (w.condition || "").toLowerCase().split(" ")[0];
  const aqi = (w.aqi != null) ? ` · AQI ${w.aqi} (${escapeHtml(w.aqi_label)})` : "";
  const now = new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  hero.innerHTML =
    `<div class="wx-body">
       <div class="wx-loc">${escapeHtml(w.city)} <span class="wx-date">${now}</span></div>
       <div class="wx-main"><div class="wx-temp">${w.temp}°</div><div class="wx-ic">${w.icon}</div></div>
       <div class="wx-cond">${escapeHtml(w.condition)}</div>
       <div class="wx-meta"><span>↑ ${w.hi}°</span><span>↓ ${w.lo}°</span><span>💧 ${w.precip}%</span></div>
       <div class="wx-foot">🍃 ${w.wind} km/h${aqi}</div>
     </div>`;
}

// ---- Companion + Atlas: Orrin as a living presence inside a pannable map of your projects ----
let deckData = null;
let atlasWorld = [];                 // project nodes in world coordinates (spatial memory)
let atlasPan = { x: 0, y: 0, s: 1 }; // pan offset (px) + zoom scale

async function renderAtlas() {       // fetch live projects, lay them out, draw + refresh companion
  try { deckData = await (await fetch("/api/deck")).json(); } catch (_) {}
  layoutAtlas(); drawAtlas(); buildChips(); initHomeFX();
  const say = $("compSay"), d = deckData || {}, pj = d.project || {};
  if (say && !say.dataset.live) say.textContent = pj.claude
    ? `Everything's connected — working in ${pj.name || "your project"}.`
    : "Ready when you are.";
}

function layoutAtlas() {
  const projs = (deckData && deckData.projects) || [];
  // Cluster by parent directory so related projects sit together (spatial memory).
  const groups = {};
  projs.forEach((p) => { const par = (p.path || "").replace(/\/[^/]*$/, "") || "~"; (groups[par] = groups[par] || []).push(p); });
  const keys = Object.keys(groups), G = keys.length || 1;
  atlasWorld = [];
  keys.forEach((k, gi) => {
    const base = -Math.PI / 2 + gi / G * Math.PI * 2, members = groups[k];
    members.forEach((p, mi) => {
      const a = base + (mi - (members.length - 1) / 2) * 0.34, r = 210 + mi * 74;
      atlasWorld.push({ name: p.name, current: !!p.current, active: !!p.active && !p.current,
        stat: p.current ? "current" : (p.active ? "open" : "idle"),
        wx: Math.cos(a) * r, wy: Math.sin(a) * r });
    });
  });
}

function drawAtlas() {
  const stage = $("atlas"), nodesEl = $("atlasNodes"), links = $("atlasLinks");
  if (!stage || !nodesEl || !links) return;
  const W = stage.clientWidth, H = stage.clientHeight; if (!W || !H) return;
  const cx = W / 2, cy = H / 2, s = atlasPan.s;
  let nhtml = "", lhtml =
    '<defs><linearGradient id="al" x1="0" y1="0" x2="1" y2="0">' +
    '<stop offset="0" stop-color="var(--purple)" stop-opacity="0"/>' +
    '<stop offset="1" stop-color="var(--purple)" stop-opacity=".55"/></linearGradient></defs>';
  atlasWorld.forEach((nd) => {
    const x = cx + nd.wx * s + atlasPan.x, y = cy + nd.wy * s + atlasPan.y;
    if (x < -50 || x > W + 50 || y < -50 || y > H + 50) return;      // cull off-screen
    const sc = Math.max(0.7, Math.min(1.15, s)).toFixed(2);
    const cls = "anode" + (nd.current ? " current" : "") + (nd.active ? " active" : "");
    nhtml += `<div class="${cls}" style="left:${x.toFixed(0)}px;top:${y.toFixed(0)}px;transform:translate(-50%,-50%) scale(${sc})" title="${escapeHtml(nd.name + " · " + nd.stat)}">` +
      `<span class="ap"></span><span class="al2">${escapeHtml(nd.name)}</span>` +
      `<span class="as ${nd.current || nd.active ? "" : "i"}">${escapeHtml(nd.stat)}</span></div>`;
    lhtml += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(0)}" y2="${y.toFixed(0)}" stroke="url(#al)" stroke-width="${(1.1 * s).toFixed(2)}"/>`;
  });
  links.setAttribute("viewBox", `0 0 ${W} ${H}`); links.setAttribute("width", W); links.setAttribute("height", H);
  links.innerHTML = lhtml; nodesEl.innerHTML = nhtml;
}

function buildChips() {
  const c = $("compChips"); if (!c) return;
  const chips = [
    { t: "🎙 Talk", f: () => { const m = $("mic"); if (m) m.click(); } },
    { t: "📋 Brief me", f: () => { if (ws && ws.readyState === 1) { ws.send(JSON.stringify({ type: "user_text", text: "Brief me — where are we?" })); openChat(); } } },
    { t: "＋ New chat", f: () => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "new_chat" })); } },
  ];
  c.innerHTML = "";
  chips.forEach((ch) => { const b = document.createElement("button"); b.className = "chip"; b.textContent = ch.t; b.onclick = ch.f; c.appendChild(b); });
}

// Orrin "speaks" on the companion — its latest reply, shown by the presence.
function setCompSay(text) {
  const el = $("compSay"); if (!el || !text) return;
  el.dataset.live = "1"; el.textContent = text.length > 160 ? text.slice(0, 158) + "…" : text;
}

// Jarvis-style dotted particle sphere for the companion + a subtle vector backdrop.
function initHomeFX() { initCompanionCanvas(); initAtlasBg(); }

function initCompanionCanvas() {
  const cv = $("compCanvas"); if (!cv || cv._on) return; cv._on = true;
  const ctx = cv.getContext("2d"); const DPR = Math.min(2, window.devicePixelRatio || 1);
  // Points on a fibonacci sphere, joined into a network (each to its nearest neighbours) —
  // a rotating "vision graph" globe rather than loose dots.
  const N = 240, P = [];
  for (let i = 0; i < N; i++) { const y = 1 - (i / (N - 1)) * 2; const r = Math.sqrt(1 - y * y); const t = i * 2.399963; P.push([Math.cos(t) * r, y, Math.sin(t) * r]); }
  const edges = [], seen = new Set();
  for (let i = 0; i < N; i++) {
    const d = [];
    for (let j = 0; j < N; j++) if (j !== i) d.push([P[i][0] * P[j][0] + P[i][1] * P[j][1] + P[i][2] * P[j][2], j]);
    d.sort((a, b) => b[0] - a[0]);
    for (let k = 0; k < 3; k++) { const j = d[k][1], key = i < j ? i + "_" + j : j + "_" + i; if (!seen.has(key)) { seen.add(key); edges.push([i, j]); } }
  }
  let ang = 0;
  function frame() {
    const reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;
    const size = cv.clientWidth || 168; if (cv.width !== Math.round(size * DPR)) cv.width = cv.height = Math.round(size * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0); ctx.clearRect(0, 0, size, size);
    const cx = size / 2, cy = size / 2, R = size * 0.44;
    const active = appEl.classList.contains("is-active");
    ang += active ? 0.011 : 0.004;
    const ca = Math.cos(ang), sa = Math.sin(ang), ct = Math.cos(0.42), stt = Math.sin(0.42);
    const pr = P.map((p) => { const x = p[0] * ca - p[2] * sa, z0 = p[0] * sa + p[2] * ca, y = p[1]; return [x, y * ct - z0 * stt, y * stt + z0 * ct]; });
    // network edges (depth-faded)
    ctx.lineWidth = 1;
    for (const [i, j] of edges) {
      const a = pr[i], b = pr[j], depth = ((a[2] + b[2]) / 2 + 1) / 2;
      ctx.beginPath(); ctx.moveTo(cx + a[0] * R, cy + a[1] * R); ctx.lineTo(cx + b[0] * R, cy + b[1] * R);
      ctx.strokeStyle = `rgba(96,150,240,${0.03 + depth * 0.22})`; ctx.stroke();
    }
    // nodes on top, back-to-front
    const order = pr.map((_, i) => i).sort((i, j) => pr[i][2] - pr[j][2]);
    for (const i of order) {
      const p = pr[i], depth = (p[2] + 1) / 2, x = cx + p[0] * R, y = cy + p[1] * R;
      ctx.beginPath(); ctx.arc(x, y, 0.6 + depth * 1.5, 0, 7);
      ctx.fillStyle = `rgba(${Math.round(125 + depth * 125)},${Math.round(205 + depth * 45)},255,${0.18 + depth * 0.78})`; ctx.fill();
    }
    const gl = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    gl.addColorStop(0, `rgba(150,200,255,${active ? 0.26 : 0.15})`); gl.addColorStop(1, "rgba(150,200,255,0)");
    ctx.fillStyle = gl; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fill();
    if (!reduce) requestAnimationFrame(frame);
  }
  frame();
}

function initAtlasBg() {
  const cv = $("atlasBg"); if (!cv || cv._on) return; cv._on = true;
  const ctx = cv.getContext("2d"); const DPR = Math.min(2, window.devicePixelRatio || 1);
  const parts = [];
  for (let i = 0; i < 42; i++) parts.push({ x: Math.random(), y: Math.random(), s: 0.4 + Math.random() * 1.3, vx: (Math.random() - 0.5) * 0.0006, vy: (Math.random() - 0.5) * 0.0006 });
  let t = 0;
  function frame() {
    const reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;
    const W = cv.clientWidth, H = cv.clientHeight; if (!W || !H) { requestAnimationFrame(frame); return; }
    if (cv.width !== Math.round(W * DPR)) { cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR); }
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0); ctx.clearRect(0, 0, W, H);
    const cx = W / 2, cy = H * 0.46, gap = 36;
    ctx.fillStyle = "rgba(140,152,220,0.05)";
    for (let x = cx % gap; x < W; x += gap) for (let y = cy % gap; y < H; y += gap) ctx.fillRect(x, y, 1.4, 1.4);
    const maxR = Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy));
    for (let i = 0; i < 3; i++) {
      const ph = ((t / 260) + i / 3) % 1, R = ph * maxR;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7);
      ctx.strokeStyle = `rgba(124,108,246,${0.14 * (1 - ph)})`; ctx.lineWidth = 1; ctx.stroke();
    }
    ctx.fillStyle = "rgba(150,170,240,0.22)";
    for (const p of parts) { p.x += p.vx; p.y += p.vy; if (p.x < 0) p.x = 1; if (p.x > 1) p.x = 0; if (p.y < 0) p.y = 1; if (p.y > 1) p.y = 0; ctx.beginPath(); ctx.arc(p.x * W, p.y * H, p.s, 0, 7); ctx.fill(); }
    t++; if (!reduce) requestAnimationFrame(frame);
  }
  frame();
}

// Pan (drag) + zoom (wheel) on the atlas; the companion stays fixed at center.
(function initAtlasInput() {
  const wire = () => {
    const stage = $("atlas"); if (!stage || stage._wired) return; stage._wired = true;
    let drag = null;
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".companion") || e.target.closest(".anode") || e.target.closest(".atlas-recenter")) return;
      drag = { x: e.clientX, y: e.clientY }; try { stage.setPointerCapture(e.pointerId); } catch (_) {} stage.classList.add("grabbing");
    });
    stage.addEventListener("pointermove", (e) => {
      if (!drag) return; atlasPan.x += e.clientX - drag.x; atlasPan.y += e.clientY - drag.y;
      drag = { x: e.clientX, y: e.clientY }; drawAtlas();
    });
    const end = () => { drag = null; stage.classList.remove("grabbing"); };
    stage.addEventListener("pointerup", end); stage.addEventListener("pointercancel", end);
    stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      const mx = e.clientX - r.left - r.width / 2, my = e.clientY - r.top - r.height / 2;
      const wx = (mx - atlasPan.x) / atlasPan.s, wy = (my - atlasPan.y) / atlasPan.s;
      const ns = Math.max(0.45, Math.min(2.6, atlasPan.s * (e.deltaY < 0 ? 1.12 : 0.89)));
      atlasPan.x = mx - wx * ns; atlasPan.y = my - wy * ns; atlasPan.s = ns; drawAtlas();
    }, { passive: false });
    const rc = $("atlasRecenter"); if (rc) rc.onclick = () => { atlasPan = { x: 0, y: 0, s: 1 }; drawAtlas(); };
  };
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();

async function renderSystem() {
  try { const s = await (await fetch("/api/system")).json();
    const n = s.mcp_count || 0; $("sbMcp").textContent = n ? `${n} connectors active` : (s.register_mcp ? "Orrin MCP on" : "off");
    const am = $("actMcp"); if (am) am.textContent = n ? `${n} connectors` : "—";
  } catch (_) {}
}

// ---- Stats (hero pills + status rows + status bar) ----
function statusRows() {
  const v = cfg.voice || {}, checks = (lastPreflight && lastPreflight.checks) || [];
  const chk = (k) => checks.find((c) => c.key === k) || {};
  const claude = chk("claude_test").status, mic = chk("microphone").status;
  const inOn = v.input_enabled !== false, outOn = v.output_enabled !== false;
  const mode = inOn && outOn ? "Voice + Screen" : outOn ? "Screen" : inOn ? "Voice" : "—";
  const st = appEl.classList.contains("state-listening") ? "Listening" : appEl.classList.contains("state-speaking") ? "Speaking" : appEl.classList.contains("state-thinking") ? "Thinking" : "Ready";
  const row = (l, on, val, cls) => `<div class="srow"><span class="sk"><i class="${on ? "on" : ""}"></i>${l}</span><span class="sv ${cls || ""}">${val}</span></div>`;
  return { html: row("Assistant", st !== "Ready", st, st !== "Ready" ? "ok" : "") +
      row("Claude Code", claude === "ok", claude === "ok" ? "Connected" : (claude ? "Checking…" : "…"), claude === "ok" ? "ok" : "bad") +
      row("Microphone", mic === "ok", mic === "ok" ? "On" : (mic === "warn" ? "Off" : "Blocked"), mic === "ok" ? "ok" : "bad") +
      row("Mode", true, mode, "") + row("Voice (TTS)", outOn, outOn ? "On" : "Off", outOn ? "ok" : "") +
      row("Workspaces", lastWorkspaces.length > 1, String(lastWorkspaces.length || 1), ""),
    claude, mic, mode, outOn, inOn };
}
function renderStatus() { const s = statusRows(); ["statusList", "statusListHome"].forEach((id) => { const el = $(id); if (el) el.innerHTML = s.html; }); }
function renderStats() {
  renderStatus();
  const s = statusRows();
  const hs = $("homeStats");
  if (hs) hs.innerHTML = `<span class="stat"><i class="${s.claude === "ok" ? "on" : ""}"></i>Claude ${s.claude === "ok" ? "connected" : "…"}</span>` +
    `<span class="stat">📁 <b>${escapeHtml(projName)}</b></span>` + `<span class="stat">💬 <b>${escapeHtml(chatName)}</b></span>` +
    (lastWorkspaces.length > 1 ? `<span class="stat">⚡ <b>${lastWorkspaces.length}</b> workspaces</span>` : "");
  const hp = $("heroProject"); if (hp) hp.textContent = "📁 " + projName;
  // status bar
  $("sbClaude").textContent = s.claude === "ok" ? "Connected" : (s.claude ? "Checking…" : "…");
  $("sbClaudeDot").classList.toggle("on", s.claude === "ok");
  $("sbMode").textContent = s.mode;
  $("sbTts").textContent = s.outOn ? "On" : "Off";
}

// ---- Logs ----
function fmtLogTime(ts) { const d = ts ? new Date(ts * 1000) : new Date(); return d.toLocaleTimeString([], { hour12: false }); }
function ingestLog(ev) {
  const list = $("logList"); if (!list) return;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const row = document.createElement("div"); row.className = "logline " + (ev.level || "info");
  const t = document.createElement("span"); t.className = "lt"; t.textContent = fmtLogTime(ev.ts);
  const lv = document.createElement("span"); lv.className = "lv"; lv.textContent = (ev.level || "info").toUpperCase();
  const m = document.createElement("span"); m.className = "lm"; m.textContent = (ev.logger && ev.logger !== "otto" ? ev.logger + ": " : "") + (ev.msg || "");
  row.append(t, lv, m); list.appendChild(row);
  while (list.childElementCount > 600) list.removeChild(list.firstChild);
  if (atBottom) list.scrollTop = list.scrollHeight;
  if (ev.level === "error" || ev.level === "critical") setMood("critical", 6000); else if (ev.level === "warning") setMood("warn", 5000);
  if (!appEl.classList.contains("logs-open") && (ev.level === "error" || ev.level === "warning" || ev.level === "critical")) $("logsToggle").classList.add("unread");
}
$("logsToggle").onclick = () => { const open = appEl.classList.toggle("logs-open"); if (open) { $("logsToggle").classList.remove("unread"); const l = $("logList"); if (l) l.scrollTop = l.scrollHeight; } };
$("logsClose").onclick = () => appEl.classList.remove("logs-open");
$("logsClear").onclick = () => { const l = $("logList"); if (l) l.innerHTML = ""; };

// ---- Setup / readiness ----
const CHK_ICON = { ok: "✓", fail: "✕", warn: "!", checking: "…", pending: "·" };
let seenReady = false;
function renderPreflight(ev) {
  lastPreflight = ev; renderStats();
  const list = $("checks");
  if (list) { list.innerHTML = "";
    (ev.checks || []).forEach((c) => {
      const row = document.createElement("div"); row.className = "chk " + (c.status || "checking") + (c.required ? "" : " opt");
      const ic = document.createElement("span"); ic.className = "chk-ic"; ic.textContent = CHK_ICON[c.status] || "·";
      const meta = document.createElement("div"); meta.className = "chk-meta";
      meta.innerHTML = `<b>${escapeHtml(c.label)}</b><span>${escapeHtml(c.detail || "")}</span>` + (c.fix && c.status === "fail" ? `<span class="fix">${escapeHtml(c.fix)}</span>` : "");
      row.append(ic, meta);
      if (c.pane && (c.status === "fail" || c.status === "warn")) { const b = document.createElement("button"); b.className = "ghost"; b.textContent = "Open Settings";
        b.onclick = () => ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "open_pane", pane: c.pane })); row.appendChild(b); }
      list.appendChild(row);
    }); }
  const pill = $("readyPill"); if (pill) { pill.classList.toggle("ok", !!ev.ready); pill.classList.toggle("bad", !ev.ready); pill.textContent = ev.ready ? "✓ Ready" : (ev.done ? "⚠ Setup" : "… Checking"); }
  if (ev.ready) { $("setupEmoji").textContent = "✅"; $("setupTitle").textContent = "Orrin is ready"; $("setupSub").textContent = "Everything checks out — say “Otto” or click Talk."; }
  else if (!ev.done) { $("setupEmoji").textContent = "🛠️"; $("setupTitle").textContent = "Setting up Orrin…"; $("setupSub").textContent = "Checking permissions and your Claude connection."; }
  else { $("setupEmoji").textContent = "⚠️"; $("setupTitle").textContent = "Orrin needs one more step"; $("setupSub").textContent = "Fix the items below, then click Recheck."; }
  $("recheckBtn").disabled = !ev.done; $("setupNote").textContent = ev.done ? "" : "Working…";
  if (!ev.done) { if (ev.first_run) showSetup(); } else if (!ev.ready) showSetup();
  else if (!seenReady && (ev.first_run || appEl.classList.contains("setup-open"))) { seenReady = true; setTimeout(hideSetup, 1800); }
}
function showSetup() { appEl.classList.add("setup-open"); $("setup").hidden = false; }
function hideSetup() { appEl.classList.remove("setup-open"); $("setup").hidden = true; }
$("readyPill").onclick = () => ($("setup").hidden ? showSetup() : hideSetup());
$("setupClose").onclick = hideSetup;
$("recheckBtn").onclick = () => { $("setupNote").textContent = "Rechecking…"; ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "recheck" })); };

// ---- Canvas (Visuals) ----
function showCanvas(content, format) { canvasEl.srcdoc = format === "markdown" ? mdToHtml(content) : content; const st = document.querySelector(".stage"); if (st) st.classList.add("has-content"); }
function hideCanvas() { canvasEl.srcdoc = ""; const st = document.querySelector(".stage"); if (st) st.classList.remove("has-content"); }
function mdToHtml(md) { const body = escapeHtml(md).replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  return `<style>body{font-family:-apple-system,sans-serif;background:#0a0f1c;color:#e6ecf6;padding:24px;line-height:1.6}code{background:#131a2b;padding:2px 5px;border-radius:4px}</style>${body}`; }

// ---- Chats (in drawer) ----
async function loadSessions() { try { const r = await (await fetch("/api/sessions")).json(); renderChatRows(r.sessions || [], false); } catch (_) {} }
async function loadAllSessions(q) { try { const r = await (await fetch("/api/all-sessions?q=" + encodeURIComponent(q || ""))).json(); renderChatRows(r.sessions || [], true); } catch (_) {} }
function renderChatRows(rows, all) {
  const el = $("chatList"); if (!el) return; el.innerHTML = "";
  rows.forEach((s) => { const row = document.createElement("div"); row.className = "prow" + (s.current ? " cur" : "");
    const meta = document.createElement("div"); meta.className = "pmeta"; const tag = all ? (s.cowork ? "🧩 " : "📁 ") + escapeHtml(s.project_name) + " · " : "";
    meta.innerHTML = `<b>${escapeHtml(s.title)}</b><span>${tag}${escapeHtml(timeAgo(s.updated_at))}${s.git_branch ? " · " + escapeHtml(s.git_branch) : ""}</span>`;
    const actions = document.createElement("div"); actions.className = "crow-actions";
    if (s.current) { if (!all) { const ren = document.createElement("button"); ren.className = "rename"; ren.textContent = "Rename";
        ren.onclick = async () => { const t = prompt("Rename this chat to:", s.title); if (!t) return; await postJSON("/api/session/rename", { title: t }); setTimeout(loadSessions, 800); }; actions.append(ren); }
      const cur = document.createElement("button"); cur.className = "ghost"; cur.textContent = "Current"; cur.disabled = true; actions.append(cur);
    } else { const btn = document.createElement("button"); btn.textContent = "Open";
      btn.onclick = async () => { if (all) await postJSON("/api/session/open-anywhere", { project: s.project, session_id: s.session_id }); else await postJSON("/api/session/switch", { session_id: s.session_id });
        setTimeout(all ? () => loadAllSessions($("chatSearch").value) : loadSessions, 1200); }; actions.append(btn); }
    row.append(meta, actions); el.appendChild(row); });
  if (!rows.length) el.innerHTML = '<p class="tiny">No chats found.</p>';
}
let chatScope = "project", searchTimer = null;
function setChatScope(scope) { chatScope = scope; $("scopeProject").classList.toggle("on", scope === "project"); $("scopeAll").classList.toggle("on", scope === "all");
  $("chatSearch").hidden = scope !== "all"; $("chatsHint").textContent = scope === "all" ? "Every chat across all projects & cowork." : "Chats in this project — switch, or start a new one.";
  if (scope === "all") loadAllSessions($("chatSearch").value); else loadSessions(); }
$("chatsToggle").onclick = () => { const h = $("chatMgr").hidden = !$("chatMgr").hidden; if (!$("chatMgr").hidden) setChatScope(chatScope); };
$("scopeProject").onclick = () => setChatScope("project");
$("scopeAll").onclick = () => setChatScope("all");
$("chatSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadAllSessions($("chatSearch").value), 220); };
$("newChat").onclick = async () => { await postJSON("/api/session/new"); setTimeout(loadSessions, 1200); };

// ---- Settings ----
function wakeLabel(v) { const s = v.stt || {}, on = v.input_enabled !== false; if (!on) return "MIC OFF"; if (s.always_listen) return "ALWAYS ON"; return "“" + (s.wake_words || "otto").trim().toUpperCase() + "”"; }
async function loadConfig() {
  try { const r = await (await fetch("/api/config")).json(); cfg = r.config || {};
    const v = cfg.voice || {}, s = v.stt || {}, t = v.tts || {}, b = cfg.brain || {};
    $("wakeBadge").textContent = wakeLabel(v);
    $("setWake").value = s.wake_words ?? "otto"; $("setModel").value = s.model ?? "small.en";
    $("setMic").checked = v.input_enabled !== false; $("setSpeak").checked = v.output_enabled !== false;
    $("setTts").value = t.engine ?? "system"; $("setBrainModel").value = b.model ?? "";
    $("setPerm").value = (b.permission === "auto" ? "bypass" : (b.permission ?? "bypass")); $("setContinue").checked = b.continue_conversation === true;
    $("setMcp").checked = b.register_mcp !== false; $("setPath").textContent = r.path || "";
    $("muteMic").classList.toggle("off", v.input_enabled === false); $("muteVoice").classList.toggle("off", v.output_enabled === false);
    renderStats();
  } catch (_) {}
}
async function loadProjects() {
  try { const r = await (await fetch("/api/projects")).json(); const el = $("setProjects"); if (!el) return; el.innerHTML = "";
    (r.projects || []).forEach((p) => { const row = document.createElement("div"); row.className = "prow" + (p.current ? " cur" : "");
      const meta = document.createElement("div"); meta.className = "pmeta"; meta.innerHTML = `<b>${escapeHtml(p.name)}</b><span>${escapeHtml(p.path)}</span>`;
      const actions = document.createElement("div"); actions.className = "crow-actions"; const btn = document.createElement("button"); btn.textContent = p.current ? "Current" : "Switch";
      if (p.current) { btn.className = "ghost"; btn.disabled = true; } btn.onclick = async () => { await postJSON("/api/project/switch", { path: p.path }); $("setMsg").textContent = "Switching…"; setTimeout(loadProjects, 1400); };
      actions.append(btn);
      if (!p.current) { const par = document.createElement("button"); par.className = "ghost"; par.textContent = "⇄ Parallel"; par.onclick = async () => { await postJSON("/api/workspace/open", { path: p.path, focus: false }); $("setMsg").textContent = "Opened in parallel."; setTimeout(loadProjects, 1000); }; actions.append(par); }
      row.append(meta, actions); el.appendChild(row); }); } catch (_) {}
}
async function loadVoices() { try { const r = await (await fetch("/api/voices")).json(); const sel = $("setVoice"); if (!sel) return; const cur = r.current || "";
    sel.innerHTML = '<option value="">System default</option>'; (r.voices || []).forEach((v) => { const o = document.createElement("option"); o.value = v.name; o.textContent = `${v.name} · ${v.locale}`; if (v.name === cur) o.selected = true; sel.appendChild(o); });
    const row = $("voiceRow"); if (row) row.style.opacity = r.engine === "elevenlabs" ? "0.5" : "1"; } catch (_) {} }
$("setVoice").onchange = async () => { const v = $("setVoice").value; await postJSON("/api/voice/select", { voice: v }); postJSON("/api/voice/preview", { voice: v }); };
$("voicePreview").onclick = () => postJSON("/api/voice/preview", { voice: $("setVoice").value });
$("setSave").onclick = async () => {
  cfg.brain = Object.assign(cfg.brain || {}, { model: $("setBrainModel").value.trim(), permission: $("setPerm").value, continue_conversation: $("setContinue").checked, register_mcp: $("setMcp").checked });
  const v = (cfg.voice = cfg.voice || {}); v.stt = v.stt || {}; v.tts = v.tts || {}; v.input_enabled = $("setMic").checked; v.output_enabled = $("setSpeak").checked;
  v.stt.wake_words = $("setWake").value.trim(); v.stt.model = $("setModel").value.trim(); v.tts.engine = $("setTts").value; v.tts.voice = $("setVoice").value;
  $("setMsg").textContent = "Saving…";
  try { const r = await (await fetch("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: cfg }) })).json();
    $("wakeBadge").textContent = wakeLabel(v); $("setMsg").textContent = r.note || "Saved."; renderStats(); } catch (_) { $("setMsg").textContent = "Save failed."; }
};

// ---- Mic / mutes / composer ----
function armTalk() { if (ws && ws.readyState === 1) { ws.send(JSON.stringify({ type: "ptt" })); $("mic").classList.add("armed"); setTimeout(() => $("mic").classList.remove("armed"), 3000); } }
$("mic").onclick = armTalk;
$("lbStop").onclick = armTalk;
$("muteMic").onclick = () => { const off = !$("muteMic").classList.contains("off"); $("muteMic").classList.toggle("off", off); ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "mute_mic", value: off })); };
$("muteVoice").onclick = () => { const off = !$("muteVoice").classList.contains("off"); $("muteVoice").classList.toggle("off", off); ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "mute_voice", value: off })); };
$("composer").onsubmit = (e) => { e.preventDefault(); const i = $("input"), t = i.value.trim(); if (!t || !ws || ws.readyState !== 1) return; ws.send(JSON.stringify({ type: "user_text", text: t })); i.value = ""; setTopic(t); };

// ---- Waveforms ----
function buildWave(el, n) { if (!el) return; for (let i = 0; i < n; i++) { const b = document.createElement("i"); const t = i / (n - 1);
  const c = t < 0.5 ? lerp([56, 189, 248], [59, 130, 246], t * 2) : lerp([59, 130, 246], [168, 85, 247], (t - 0.5) * 2);
  b.style.setProperty("--bar", `rgb(${c.join(",")})`); b.style.animationDelay = i * 35 + "ms"; el.appendChild(b); } }
["wave", "wave2"].forEach((id) => buildWave($(id), 26)); buildWave($("waveMini"), 12);
function lerp(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }

// ---- Greeting + clock ----
function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
function tick() { $("greet").textContent = greeting(); }
tick(); setInterval(tick, 30000);

// ---- Init ----
loadConfig();
renderSystem();
showView("home");
setInterval(renderWeather, 600000);   // refresh weather every 10 min
setInterval(renderSystem, 120000);
if (window.innerWidth <= 1080) closeChat();     // don't overlay chat on smaller screens by default
let _conTimer; addEventListener("resize", () => { clearTimeout(_conTimer); _conTimer = setTimeout(() => { if (view === "home") drawAtlas(); }, 150); });
setInterval(() => { if (view === "home") renderAtlas(); }, 15000);   // refresh live state
{ const ht = $("homeTalk"); if (ht) ht.onclick = () => { const m = $("mic"); if (m) m.click(); }; }   // Talk pill → mic

// Foldable sidebar (persisted). Collapsing changes the stage width, so redraw the constellation.
try { if (localStorage.getItem("otto-side") === "1") appEl.classList.add("side-collapsed"); } catch (e) {}
{ const sf = $("sideFold"); if (sf) sf.onclick = () => {
    const on = appEl.classList.toggle("side-collapsed");
    try { localStorage.setItem("otto-side", on ? "1" : "0"); } catch (e) {}
    if (view === "home") setTimeout(drawAtlas, 230);
  }; }
window.addEventListener("hashchange", () => { if (location.hash === "#settings") showView("settings"); });
if (location.hash === "#settings") showView("settings");
connect();
