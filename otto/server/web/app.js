// Otto dashboard: sidebar + home widgets + conversation/visuals/settings views.
const $ = (id) => document.getElementById(id);
const appEl = $("app");
const canvasEl = $("canvas");
const transcriptEl = $("transcript");
const stageEl = document.querySelector(".stage");
const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const postJSON = (url, body) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });

let ws, partialUser = null, assistantBubble = null;
let view = "home", replaying = false;
let lastPreflight = null, cfg = {}, lastWorkspaces = [];

// Per-workspace chat buffers (each parallel workspace is its own window).
let focusedWs = null;
const wins = {};
const curKey = () => focusedWs || "__main__";
function win(k) { return wins[k] || (wins[k] = { msgs: [], cur: null, canvas: null, unread: false }); }
function keyOf(ev) { return ev.ws || focusedWs || "__main__"; }

// ---- Theme (light/dark, system-aware, remembered) ----
function currentTheme() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced === "light" || forced === "dark") return forced;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function applyThemeIcon() {
  const b = $("themeToggle"); if (!b) return;
  const t = currentTheme();
  b.textContent = t === "light" ? "☀️" : "🌙";
  b.title = t === "light" ? "Switch to dark" : "Switch to light";
}
applyThemeIcon();
if (window.matchMedia) { try { window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", applyThemeIcon); } catch (_) {} }
$("themeToggle").onclick = () => {
  const next = currentTheme() === "light" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", next);
  try { localStorage.setItem("otto-theme", next); } catch (_) {}
  applyThemeIcon();
};

// ---- View switching ----
function showView(name) {
  view = name;
  document.querySelectorAll(".view").forEach((s) => (s.hidden = s.dataset.view !== name));
  document.querySelectorAll(".navbtn").forEach((b) => b.classList.toggle("active", b.dataset.nav === name));
  const nav = document.querySelector(`.navbtn[data-nav="${name}"]`);
  if (nav) nav.classList.remove("unread");
  if (name === "home") { loadRecent(); renderStatus(); }
  if (name === "convo") scroll();
  if (name === "settings") { loadConfig(); loadProjects(); loadVoices(); }
}
document.querySelectorAll(".navbtn").forEach((b) => (b.onclick = () => showView(b.dataset.nav)));
function markNavUnread(name) {
  if (view === name) return;
  const nav = document.querySelector(`.navbtn[data-nav="${name}"]`);
  if (nav) nav.classList.add("unread");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setStatus("idle", "Ready");
  ws.onclose = () => { setStatus("idle", "reconnecting…"); setTimeout(connect, 1000); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function setStatus(state, text) {
  const M = { listening: ["Listening…", "speak now"], thinking: ["Thinking…", "working on it…"],
              working: ["Working…", "one moment…"], speaking: ["Speaking…", ""] };
  const m = M[state];
  $("heroState").textContent = m ? m[0] : "Ready";
  $("heroSub").textContent = m ? (m[1] || "…") : "Tap to talk or type your question";
  $("talkState").textContent = m ? m[0] : "Ready";
  const dot = $("talkDot");
  if (dot) dot.style.background = state === "listening" ? "var(--cyan)"
    : state === "speaking" ? "var(--purple)"
    : (state === "thinking" || state === "working") ? "var(--warn)" : "var(--ok)";
  ["state-listening", "state-thinking", "state-speaking", "is-active"].forEach((c) => appEl.classList.remove(c));
  if (state === "listening") appEl.classList.add("state-listening", "is-active");
  else if (state === "speaking") appEl.classList.add("state-speaking", "is-active");
  else if (state === "thinking" || state === "working") appEl.classList.add("state-thinking", "is-active");
  if (state === "listening") setCaption("🎙️ Listening…", "hint");
  else if (state === "idle") setCaption("", "");
  renderStatus();
}

// ---- Live caption ----
let captionHideTimer = null;
function setCaption(text, cls) {
  const el = $("caption"); if (!el) return;
  clearTimeout(captionHideTimer);
  if (!text) { el.hidden = true; el.textContent = ""; el.className = "caption"; return; }
  el.hidden = false; el.textContent = text; el.className = "caption" + (cls ? " " + cls : "");
}

function fmtTime() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function timeAgo(sec) {
  if (!sec) return "";
  const d = Math.max(0, Date.now() / 1000 - sec);
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}

// ---- Transcript ----
function mkMsg(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  const who = document.createElement("div"); who.className = "who";
  const b = document.createElement("b"); b.textContent = role.includes("user") ? "You" : "Otto";
  const t = document.createElement("span"); t.className = "t"; t.textContent = fmtTime();
  who.append(b, t);
  const body = document.createElement("div"); body.textContent = text;
  el.append(who, body); el._body = body;
  return el;
}
function mkTool(name, detail) {
  const el = document.createElement("div"); el.className = "tool";
  el.innerHTML = `<b>${escapeHtml(name)}</b>${detail ? " · " + escapeHtml(detail) : ""}`;
  return el;
}
function bubble(role, text) {
  const el = mkMsg(role, text);
  transcriptEl.appendChild(el); scroll();
  markNavUnread("convo");
  return el;
}
function scroll() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }

function renderWindow(k) {
  transcriptEl.innerHTML = ""; partialUser = null; assistantBubble = null;
  const w = win(k);
  for (const m of w.msgs) {
    if (m.role === "tool") transcriptEl.appendChild(mkTool(m.name, m.detail));
    else if (m.role === "notify") transcriptEl.appendChild(mkMsg("assistant", `🔔 ${m.title}${m.body ? " — " + m.body : ""}`));
    else transcriptEl.appendChild(mkMsg(m.role, m.text));
  }
  if (w.cur) assistantBubble = transcriptEl.appendChild(mkMsg("assistant", w.cur));
  scroll();
  if (w.canvas) showCanvas(w.canvas.content, w.canvas.format);
  else { canvasEl.srcdoc = ""; if (stageEl) stageEl.classList.remove("has-content"); }
}

function setFocused(path, name) {
  if (name) { const el = $("proj"); if (el) el.textContent = "📁 " + name; }
  if (!path || path === focusedWs) return;
  if (focusedWs === null && wins["__main__"] && !wins[path]) { wins[path] = wins["__main__"]; delete wins["__main__"]; }
  focusedWs = path;
  win(path).unread = false;
  renderWindow(path);
  renderWorkspaces(lastWorkspaces);
}
function markUnread(k) {
  const p = document.querySelector(`#wsbar [data-ws="${CSS.escape(k)}"]`);
  if (p) p.classList.add("unread");
}

function handle(ev) {
  switch (ev.type) {
    case "status": setStatus(ev.state, ev.text); return;
    case "history_start": { replaying = true; const w = win(curKey()); w.msgs = []; w.cur = null; transcriptEl.innerHTML = ""; partialUser = assistantBubble = null; return; }
    case "history_end": { replaying = false; const d = document.createElement("div"); d.className = "divider"; d.textContent = "— now —"; transcriptEl.appendChild(d); scroll(); return; }
    case "ask_user": showView("convo"); $("input").focus(); return;
    case "project": setFocused(ev.path, ev.name || ev.path); return;
    case "workspaces": setFocused(ev.focused); renderWorkspaces(ev.items || [], ev.focused, ev.max); return;
    case "log": ingestLog(ev); return;
    case "preflight": renderPreflight(ev); return;
    case "session": {
      const el = $("chat"); if (el) el.textContent = "💬 " + (ev.title || "New chat");
      if (view === "convo" && !$("chatListWrap").hidden) (chatScope === "all") ? loadAllSessions($("chatSearch").value) : loadSessions();
      if (view === "home") loadRecent();
      return;
    }
    case "canvas_render": {
      const k = keyOf(ev), w = win(k); w.canvas = { content: ev.content, format: ev.format };
      if (k === curKey()) { showCanvas(ev.content, ev.format); markNavUnread("visuals"); }
      else { w.unread = true; markUnread(k); }
      return;
    }
    case "notify": {
      win(curKey()).msgs.push({ role: "notify", title: ev.title, body: ev.body });
      bubble("assistant", `🔔 ${ev.title}${ev.body ? " — " + ev.body : ""}`); return;
    }
    case "transcript_partial": case "transcript_final":
    case "assistant_delta": case "tool_use": ingestMsg(ev); return;
  }
}

function ingestMsg(ev) {
  const k = keyOf(ev), w = win(k), live = k === curKey();
  switch (ev.type) {
    case "transcript_partial":
      if (ev.role === "user" && live) {
        if (!partialUser) partialUser = bubble("user partial", ""); partialUser._body.textContent = ev.text; scroll();
        setCaption(ev.text, "user");
      }
      return;
    case "transcript_final":
      if (ev.role === "user") {
        w.msgs.push({ role: "user", text: ev.text });
        if (live) {
          if (partialUser) { partialUser.className = "msg user"; partialUser._body.textContent = ev.text; partialUser = null; } else bubble("user", ev.text);
          setCaption(ev.text, "user"); captionHideTimer = setTimeout(() => setCaption("", ""), 2600);
          if (view === "home" && !replaying) showView("convo");
        }
      } else {
        const text = ev.text || w.cur || ""; w.cur = null;
        w.msgs.push({ role: "assistant", text });
        if (live) { if (assistantBubble) { assistantBubble._body.textContent = text; assistantBubble = null; } else bubble("assistant", text); }
      }
      break;
    case "assistant_delta":
      w.cur = (w.cur || "") + ev.text;
      if (live) { if (!assistantBubble) assistantBubble = bubble("assistant", ""); assistantBubble._body.textContent = w.cur; scroll(); }
      break;
    case "tool_use":
      w.msgs.push({ role: "tool", name: ev.name, detail: ev.detail });
      if (live) { transcriptEl.appendChild(mkTool(ev.name, ev.detail)); scroll(); markNavUnread("convo"); }
      break;
  }
  if (!live) { w.unread = true; markUnread(k); }
}

// ---- Workspaces widget ----
const WS_DOT = { idle: "idle", thinking: "busy", working: "busy", speaking: "busy" };
function renderWorkspaces(items, focused, max) {
  lastWorkspaces = items || lastWorkspaces;
  const bar = $("wsbar"); if (!bar) return;
  $("wspCount").textContent = lastWorkspaces.length ? `${lastWorkspaces.length}/${max || 3}` : "";
  if (lastWorkspaces.length <= 1) { bar.classList.add("empty"); bar.innerHTML = ""; renderStatus(); return; }
  bar.classList.remove("empty"); bar.innerHTML = "";
  lastWorkspaces.forEach((w) => {
    const unread = win(w.project).unread && !w.focused;
    const pill = document.createElement("div");
    pill.className = "wspill" + (w.focused ? " foc" : "") + (unread ? " unread" : "");
    pill.dataset.ws = w.project;
    const dot = document.createElement("i"); dot.className = "wsdot " + (WS_DOT[w.state] || "idle");
    const nm = document.createElement("span"); nm.className = "wsname"; nm.textContent = w.name;
    pill.append(dot, nm);
    if (w.state !== "idle") { const s = document.createElement("span"); s.className = "wsact"; s.textContent = w.state; pill.appendChild(s); }
    if (!w.focused) {
      pill.title = "Switch to " + w.name;
      pill.onclick = () => ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "focus_workspace", path: w.project }));
      const x = document.createElement("button"); x.className = "wsx"; x.textContent = "✕"; x.title = "Close " + w.name;
      x.onclick = (e) => { e.stopPropagation(); ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "close_workspace", path: w.project })); };
      pill.appendChild(x);
    } else { pill.title = w.name + " · focused"; }
    bar.appendChild(pill);
  });
  renderStatus();
}

// ---- Otto Status widget ----
function statusRow(label, on, value, cls) {
  return `<div class="srow"><span class="sk"><i class="${on ? "on" : ""}"></i>${escapeHtml(label)}</span>` +
    `<span class="sv ${cls || ""}">${escapeHtml(value)}</span></div>`;
}
function renderStatus() {
  const el = $("statusList"); if (!el) return;
  const v = cfg.voice || {}, checks = (lastPreflight && lastPreflight.checks) || [];
  const chk = (k) => checks.find((c) => c.key === k) || {};
  const claude = chk("claude_test").status, mic = chk("microphone").status;
  const inOn = v.input_enabled !== false, outOn = v.output_enabled !== false;
  const mode = inOn && outOn ? "Voice + Screen" : outOn ? "Screen + Voice" : inOn ? "Voice" : "Screen";
  const st = appEl.classList.contains("state-listening") ? "Listening"
    : appEl.classList.contains("state-speaking") ? "Speaking"
    : appEl.classList.contains("state-thinking") ? "Thinking" : "Ready";
  el.innerHTML =
    statusRow("Assistant", st !== "Ready", st, st !== "Ready" ? "ok" : "") +
    statusRow("Claude Code", claude === "ok", claude === "ok" ? "Connected" : (claude ? "Checking…" : "…"), claude === "ok" ? "ok" : "bad") +
    statusRow("Microphone", mic === "ok", mic === "ok" ? "On" : (mic === "warn" ? "Off" : "Blocked"), mic === "ok" ? "ok" : "bad") +
    statusRow("Mode", true, mode, "") +
    statusRow("Voice (TTS)", outOn, outOn ? "On" : "Off", outOn ? "ok" : "") +
    statusRow("Workspaces", lastWorkspaces.length > 1, String(lastWorkspaces.length || 1), "");
}

// ---- Recent widget ----
async function loadRecent() {
  const el = $("recentList"); if (!el) return;
  try {
    const r = await (await fetch("/api/sessions")).json();
    const rows = (r.sessions || []).slice(0, 5);
    el.innerHTML = "";
    if (!rows.length) { el.innerHTML = '<p class="tiny">No chats yet — say hello.</p>'; return; }
    rows.forEach((s) => {
      const it = document.createElement("div"); it.className = "ritem";
      it.innerHTML = `<span class="ri-ic">💬</span><div class="ri-t"><b>${escapeHtml(s.title)}</b>` +
        `<span>${escapeHtml(s.git_branch || "chat")}</span></div><span class="ri-time">${escapeHtml(timeAgo(s.updated_at))}</span>`;
      it.onclick = async () => {
        if (!s.current) await postJSON("/api/session/switch", { session_id: s.session_id });
        showView("convo");
      };
      el.appendChild(it);
    });
  } catch (_) { el.innerHTML = '<p class="tiny">Couldn\'t load recent chats.</p>'; }
}
$("recentAll").onclick = () => { showView("convo"); openChatList(); };

// ---- Quick actions ----
const QUICK = [
  { i: "🗓", t: "Daily briefing", p: "Give me a short briefing of what's on today." },
  { i: "📁", t: "Recent files", p: "List the files I've recently worked on." },
  { i: "🩺", t: "System status", p: "Show me a quick system status." },
  { i: "🔎", t: "Search docs", focus: true },
];
(function buildQuick() {
  const el = $("qactions"); if (!el) return;
  QUICK.forEach((q) => {
    const b = document.createElement("button"); b.className = "qbtn";
    b.innerHTML = `<span class="qi">${q.i}</span>${escapeHtml(q.t)}`;
    b.onclick = () => {
      if (q.focus) { showView("convo"); $("input").focus(); return; }
      if (ws && ws.readyState === 1) { ws.send(JSON.stringify({ type: "user_text", text: q.p })); showView("convo"); }
    };
    el.appendChild(b);
  });
})();

// ---- Logs ----
function fmtLogTime(ts) { const d = ts ? new Date(ts * 1000) : new Date(); return d.toLocaleTimeString([], { hour12: false }); }
function ingestLog(ev) {
  const list = $("logList"); if (!list) return;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const row = document.createElement("div"); row.className = "logline " + (ev.level || "info");
  const t = document.createElement("span"); t.className = "lt"; t.textContent = fmtLogTime(ev.ts);
  const lv = document.createElement("span"); lv.className = "lv"; lv.textContent = (ev.level || "info").toUpperCase();
  const m = document.createElement("span"); m.className = "lm";
  m.textContent = (ev.logger && ev.logger !== "otto" ? ev.logger + ": " : "") + (ev.msg || "");
  row.append(t, lv, m); list.appendChild(row);
  while (list.childElementCount > 600) list.removeChild(list.firstChild);
  if (atBottom) list.scrollTop = list.scrollHeight;
  if (!appEl.classList.contains("logs-open") && (ev.level === "error" || ev.level === "warning" || ev.level === "critical"))
    $("logsToggle").classList.add("unread");
}
$("logsToggle").onclick = () => {
  const open = appEl.classList.toggle("logs-open");
  if (open) { $("logsToggle").classList.remove("unread"); const l = $("logList"); if (l) l.scrollTop = l.scrollHeight; }
};
$("logsClose").onclick = () => appEl.classList.remove("logs-open");
$("logsClear").onclick = () => { const l = $("logList"); if (l) l.innerHTML = ""; };

// ---- Setup / readiness ----
const CHK_ICON = { ok: "✓", fail: "✕", warn: "!", checking: "…", pending: "·" };
let seenReady = false;
function renderPreflight(ev) {
  lastPreflight = ev; renderStatus();
  const list = $("checks");
  if (list) {
    list.innerHTML = "";
    (ev.checks || []).forEach((c) => {
      const row = document.createElement("div");
      row.className = "chk " + (c.status || "checking") + (c.required ? "" : " opt");
      const ic = document.createElement("span"); ic.className = "chk-ic"; ic.textContent = CHK_ICON[c.status] || "·";
      const meta = document.createElement("div"); meta.className = "chk-meta";
      meta.innerHTML = `<b>${escapeHtml(c.label)}</b><span>${escapeHtml(c.detail || "")}</span>` +
        (c.fix && c.status === "fail" ? `<span class="fix">${escapeHtml(c.fix)}</span>` : "");
      row.append(ic, meta);
      if (c.pane && (c.status === "fail" || c.status === "warn")) {
        const b = document.createElement("button"); b.className = "ghost"; b.textContent = "Open Settings";
        b.onclick = () => ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "open_pane", pane: c.pane }));
        row.appendChild(b);
      }
      list.appendChild(row);
    });
  }
  const pill = $("readyPill");
  if (pill) { pill.classList.toggle("ok", !!ev.ready); pill.classList.toggle("bad", !ev.ready);
    pill.textContent = ev.ready ? "✓ Ready" : (ev.done ? "⚠ Setup" : "… Checking"); }
  if (ev.ready) { $("setupEmoji").textContent = "✅"; $("setupTitle").textContent = "Otto is ready";
    $("setupSub").textContent = "Everything checks out — say “hey Otto” or click Talk."; }
  else if (!ev.done) { $("setupEmoji").textContent = "🛠️"; $("setupTitle").textContent = "Setting up Otto…";
    $("setupSub").textContent = "Checking permissions and your Claude connection."; }
  else { $("setupEmoji").textContent = "⚠️"; $("setupTitle").textContent = "Otto needs one more step";
    $("setupSub").textContent = "Fix the items below, then click Recheck."; }
  $("recheckBtn").disabled = !ev.done;
  $("setupNote").textContent = ev.done ? "" : "Working…";
  if (!ev.done) { if (ev.first_run) showSetup(); }
  else if (!ev.ready) showSetup();
  else if (!seenReady && (ev.first_run || appEl.classList.contains("setup-open"))) { seenReady = true; setTimeout(hideSetup, 1800); }
}
function showSetup() { appEl.classList.add("setup-open"); $("setup").hidden = false; }
function hideSetup() { appEl.classList.remove("setup-open"); $("setup").hidden = true; }
$("readyPill").onclick = () => ($("setup").hidden ? showSetup() : hideSetup());
$("setupClose").onclick = hideSetup;
$("recheckBtn").onclick = () => { $("setupNote").textContent = "Rechecking…"; ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "recheck" })); };

// ---- Canvas ----
function showCanvas(content, format) {
  canvasEl.srcdoc = format === "markdown" ? mdToHtml(content) : content;
  if (stageEl) stageEl.classList.add("has-content");
}
function mdToHtml(md) {
  const body = escapeHtml(md)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  return `<style>body{font-family:-apple-system,sans-serif;background:#0a0f1c;color:#e6ecf6;padding:28px;line-height:1.6}code{background:#131a2b;padding:2px 5px;border-radius:4px}</style>${body}`;
}

// ---- Chats (list within Conversations view) ----
async function loadSessions() {
  try {
    const r = await (await fetch("/api/sessions")).json();
    renderChatRows(r.sessions || [], false);
  } catch (_) {}
}
async function loadAllSessions(q) {
  try {
    const r = await (await fetch("/api/all-sessions?q=" + encodeURIComponent(q || ""))).json();
    renderChatRows(r.sessions || [], true);
  } catch (_) {}
}
function renderChatRows(rows, all) {
  const el = $("chatList"); if (!el) return; el.innerHTML = "";
  rows.forEach((s) => {
    const row = document.createElement("div"); row.className = "prow" + (s.current ? " cur" : "");
    const meta = document.createElement("div"); meta.className = "pmeta";
    const tag = all ? (s.cowork ? "🧩 " + escapeHtml(s.project_name) + " · " : "📁 " + escapeHtml(s.project_name) + " · ") : "";
    meta.innerHTML = `<b>${escapeHtml(s.title)}</b><span>${tag}${escapeHtml(timeAgo(s.updated_at))}` +
      `${s.git_branch ? " · " + escapeHtml(s.git_branch) : ""}</span>`;
    const actions = document.createElement("div"); actions.className = "crow-actions";
    if (s.current) {
      if (!all) {
        const ren = document.createElement("button"); ren.className = "rename"; ren.textContent = "Rename";
        ren.onclick = async () => { const t = prompt("Rename this chat to:", s.title); if (!t) return;
          await postJSON("/api/session/rename", { title: t }); setTimeout(loadSessions, 800); };
        actions.append(ren);
      }
      const cur = document.createElement("button"); cur.className = "ghost"; cur.textContent = "Current"; cur.disabled = true;
      actions.append(cur);
    } else {
      const btn = document.createElement("button"); btn.textContent = "Open";
      btn.onclick = async () => {
        if (all) await postJSON("/api/session/open-anywhere", { project: s.project, session_id: s.session_id });
        else await postJSON("/api/session/switch", { session_id: s.session_id });
        setTimeout(all ? () => loadAllSessions($("chatSearch").value) : loadSessions, 1200);
      };
      actions.append(btn);
    }
    row.append(meta, actions); el.appendChild(row);
  });
  if (!rows.length) el.innerHTML = '<p class="tiny">No chats found.</p>';
}
let chatScope = "project", searchTimer = null;
function setChatScope(scope) {
  chatScope = scope;
  $("scopeProject").classList.toggle("on", scope === "project");
  $("scopeAll").classList.toggle("on", scope === "all");
  $("chatSearch").hidden = scope !== "all";
  $("chatsHint").textContent = scope === "all"
    ? "Every chat across all projects & cowork — open any one."
    : "Chats in this project — switch, or start a new one.";
  if (scope === "all") loadAllSessions($("chatSearch").value); else loadSessions();
}
function openChatList() { $("chatListWrap").hidden = false; setChatScope(chatScope); }
$("chatsToggle").onclick = () => { $("chatListWrap").hidden = !$("chatListWrap").hidden; if (!$("chatListWrap").hidden) setChatScope(chatScope); };
$("scopeProject").onclick = () => setChatScope("project");
$("scopeAll").onclick = () => setChatScope("all");
$("chatSearch").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadAllSessions($("chatSearch").value), 220); };
$("newChat").onclick = async () => { await postJSON("/api/session/new"); setTimeout(loadSessions, 1200); };

// ---- Settings ----
function wakeLabel(v) {
  const s = v.stt || {}, on = v.input_enabled !== false;
  if (!on) return "MIC OFF";
  if (s.always_listen) return "ALWAYS ON";
  return "“" + (s.wake_words || "hey otto").trim().toUpperCase() + "”";
}
async function loadConfig() {
  try {
    const r = await (await fetch("/api/config")).json();
    cfg = r.config || {};
    const v = cfg.voice || {}, s = v.stt || {}, t = v.tts || {}, b = cfg.brain || {};
    $("wakeBadge").textContent = wakeLabel(v);
    $("setWake").value = s.wake_words ?? "hey otto";
    $("setModel").value = s.model ?? "small.en";
    $("setMic").checked = v.input_enabled !== false;
    $("setSpeak").checked = v.output_enabled !== false;
    $("setTts").value = t.engine ?? "system";
    $("setBrainModel").value = b.model ?? "";
    $("setPerm").value = b.permission ?? "voice";
    $("setContinue").checked = b.continue_conversation === true;
    $("setMcp").checked = b.register_mcp !== false;
    $("setPath").textContent = r.path || "";
    renderStatus();
    // reflect mute buttons
    $("muteMic").classList.toggle("off", v.input_enabled === false);
    $("muteVoice").classList.toggle("off", v.output_enabled === false);
  } catch (_) {}
}
async function loadProjects() {
  try {
    const r = await (await fetch("/api/projects")).json();
    const el = $("setProjects"); if (!el) return; el.innerHTML = "";
    (r.projects || []).forEach((p) => {
      const row = document.createElement("div"); row.className = "prow" + (p.current ? " cur" : "");
      const meta = document.createElement("div"); meta.className = "pmeta";
      meta.innerHTML = `<b>${escapeHtml(p.name)}</b><span>${escapeHtml(p.path)}</span>`;
      const actions = document.createElement("div"); actions.className = "crow-actions";
      const btn = document.createElement("button"); btn.textContent = p.current ? "Current" : "Switch";
      if (p.current) { btn.className = "ghost"; btn.disabled = true; }
      btn.onclick = async () => { await postJSON("/api/project/switch", { path: p.path }); $("setMsg").textContent = "Switching…"; setTimeout(loadProjects, 1400); };
      actions.append(btn);
      if (!p.current) {
        const par = document.createElement("button"); par.className = "ghost"; par.textContent = "⇄ Parallel";
        par.title = "Run alongside — works in the background and pings you when done";
        par.onclick = async () => { await postJSON("/api/workspace/open", { path: p.path, focus: false }); $("setMsg").textContent = "Opened in parallel."; setTimeout(loadProjects, 1000); };
        actions.append(par);
      }
      row.append(meta, actions); el.appendChild(row);
    });
  } catch (_) {}
}
async function loadVoices() {
  try {
    const r = await (await fetch("/api/voices")).json();
    const sel = $("setVoice"); if (!sel) return;
    const cur = r.current || "";
    sel.innerHTML = '<option value="">System default</option>';
    (r.voices || []).forEach((v) => { const o = document.createElement("option");
      o.value = v.name; o.textContent = `${v.name} · ${v.locale}`; if (v.name === cur) o.selected = true; sel.appendChild(o); });
    const row = $("voiceRow"); if (row) row.style.opacity = r.engine === "elevenlabs" ? "0.5" : "1";
  } catch (_) {}
}
$("setVoice").onchange = async () => { const v = $("setVoice").value; await postJSON("/api/voice/select", { voice: v }); postJSON("/api/voice/preview", { voice: v }); };
$("voicePreview").onclick = () => postJSON("/api/voice/preview", { voice: $("setVoice").value });
$("setSave").onclick = async () => {
  cfg.brain = Object.assign(cfg.brain || {}, { model: $("setBrainModel").value.trim(), permission: $("setPerm").value,
    continue_conversation: $("setContinue").checked, register_mcp: $("setMcp").checked });
  const v = (cfg.voice = cfg.voice || {}); v.stt = v.stt || {}; v.tts = v.tts || {};
  v.input_enabled = $("setMic").checked; v.output_enabled = $("setSpeak").checked;
  v.stt.wake_words = $("setWake").value.trim(); v.stt.model = $("setModel").value.trim();
  v.tts.engine = $("setTts").value; v.tts.voice = $("setVoice").value;
  $("setMsg").textContent = "Saving…";
  try { const r = await (await fetch("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: cfg }) })).json();
    $("wakeBadge").textContent = wakeLabel(v); $("setMsg").textContent = r.note || "Saved."; renderStatus();
  } catch (_) { $("setMsg").textContent = "Save failed."; }
};

// ---- Mic / mutes / composer ----
$("mic").onclick = () => {
  if (ws && ws.readyState === 1) { ws.send(JSON.stringify({ type: "ptt" }));
    $("mic").classList.add("armed"); setTimeout(() => $("mic").classList.remove("armed"), 3000); }
};
$("muteMic").onclick = () => {
  const off = !$("muteMic").classList.contains("off");
  $("muteMic").classList.toggle("off", off);
  ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "mute_mic", value: off }));
};
$("muteVoice").onclick = () => {
  const off = !$("muteVoice").classList.contains("off");
  $("muteVoice").classList.toggle("off", off);
  ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "mute_voice", value: off }));
};
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const i = $("input"), t = i.value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "user_text", text: t })); i.value = ""; showView("convo");
};

// ---- HUD waveform bars (cyan -> purple gradient) ----
(function () {
  const w = $("wave"); if (!w) return;
  for (let i = 0; i < 22; i++) {
    const b = document.createElement("i"); const t = i / 21;
    const c = t < 0.5 ? lerp([34, 211, 238], [59, 130, 246], t * 2) : lerp([59, 130, 246], [168, 85, 247], (t - 0.5) * 2);
    b.style.setProperty("--bar", `rgb(${c.join(",")})`); b.style.animationDelay = i * 35 + "ms"; w.appendChild(b);
  }
})();
function lerp(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }

// ---- Greeting + clock ----
function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}
function tick() {
  $("greet").textContent = greeting();
  const d = new Date();
  const c = $("clock"); if (c) c.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dt = $("date"); if (dt) dt.textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}
tick(); setInterval(tick, 15000);

// ---- Init ----
loadConfig();
loadRecent();
showView(location.hash === "#settings" ? "settings" : "home");
window.addEventListener("hashchange", () => { if (location.hash === "#settings") showView("settings"); });
connect();
