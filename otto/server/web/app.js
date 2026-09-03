// JARVIS-style dashboard: the canvas is the stage; the conversation lives in a drawer.
const $ = (id) => document.getElementById(id);
const appEl = $("app");
const stageEl = document.querySelector(".stage");
const canvasEl = $("canvas");
const transcriptEl = $("transcript");

let ws, partialUser = null, assistantBubble = null;

// Each parallel workspace has its OWN chat window. We keep a per-workspace buffer so you can
// switch between them like tabs; only the focused one renders live + is spoken. `focusedWs`
// is the project path of the window currently shown; `wins[path]` is that window's buffer.
let focusedWs = null;
const wins = {};
let lastWorkspaces = [];
const curKey = () => focusedWs || "__main__";
function win(k) { return wins[k] || (wins[k] = { msgs: [], cur: null, canvas: null, unread: false }); }
function keyOf(ev) { return ev.ws || focusedWs || "__main__"; }

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
  $("heroSub").textContent = m ? m[1] : (text && text !== "Ready" ? text : "click the mic or just talk");
  ["state-listening", "state-thinking", "state-speaking", "is-active"].forEach((c) => appEl.classList.remove(c));
  if (state === "listening") appEl.classList.add("state-listening", "is-active");
  else if (state === "speaking") appEl.classList.add("state-speaking", "is-active");
  else if (state === "thinking" || state === "working") appEl.classList.add("state-thinking", "is-active");
}

function fmtTime() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

// Build a message node (no appending / no side effects) so we can both stream live and
// rebuild a window from its buffer when you switch to it.
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
  if (!appEl.classList.contains("chat-open")) $("chatToggle").classList.add("unread");
  return el;
}
function scroll() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }

// Rebuild the visible transcript + canvas from a window's buffer (used when switching windows).
function renderWindow(k) {
  transcriptEl.innerHTML = ""; partialUser = null; assistantBubble = null;
  const w = win(k);
  for (const m of w.msgs) {
    if (m.role === "tool") transcriptEl.appendChild(mkTool(m.name, m.detail));
    else if (m.role === "notify") transcriptEl.appendChild(mkMsg("assistant", `🔔 ${m.title}${m.body ? " — " + m.body : ""}`));
    else transcriptEl.appendChild(mkMsg(m.role, m.text));
  }
  if (w.cur) assistantBubble = (transcriptEl.appendChild(mkMsg("assistant", w.cur)));
  scroll();
  if (w.canvas) showCanvas(w.canvas.content, w.canvas.format);
  else { canvasEl.srcdoc = ""; stageEl.classList.remove("has-content"); }
}

function setFocused(path, name) {
  if (name) { const el = $("proj"); if (el) el.textContent = "📁 " + name; }
  if (!path || path === focusedWs) return;
  // First time we learn the focus: adopt the pre-focus/history buffer as this window.
  if (focusedWs === null && wins["__main__"] && !wins[path]) {
    wins[path] = wins["__main__"]; delete wins["__main__"];
  }
  focusedWs = path;
  win(path).unread = false;
  renderWindow(path);
  renderWorkspaces(lastWorkspaces);   // refresh unread markers
}

function markUnread(k) {
  const bar = $("wsbar"); if (!bar) return;
  const p = bar.querySelector(`[data-ws="${CSS.escape(k)}"]`);
  if (p) p.classList.add("unread");
}

function handle(ev) {
  switch (ev.type) {
    case "status": setStatus(ev.state, ev.text); return;
    case "history_start": { const w = win(curKey()); w.msgs = []; w.cur = null; transcriptEl.innerHTML = ""; partialUser = assistantBubble = null; return; }
    case "history_end": { const d = document.createElement("div"); d.className = "divider"; d.textContent = "— now —"; transcriptEl.appendChild(d); scroll(); return; }
    case "ask_user": $("input").focus(); return;
    case "project": setFocused(ev.path, ev.name || ev.path); return;
    case "workspaces": setFocused(ev.focused); renderWorkspaces(ev.items || [], ev.focused, ev.max); return;
    case "log": ingestLog(ev); return;
    case "preflight": renderPreflight(ev); return;
    case "session": {
      const el = $("chat"); if (el) el.textContent = "💬 " + (ev.title || "New chat");
      if (appEl.classList.contains("chats-open")) loadSessions();
      return;
    }
    case "canvas_render": {
      const k = keyOf(ev), w = win(k); w.canvas = { content: ev.content, format: ev.format };
      if (k === curKey()) showCanvas(ev.content, ev.format); else { w.unread = true; markUnread(k); }
      return;
    }
    case "notify": { // completion pings show in the current window
      win(curKey()).msgs.push({ role: "notify", title: ev.title, body: ev.body });
      bubble("assistant", `🔔 ${ev.title}${ev.body ? " — " + ev.body : ""}`); return;
    }
    case "transcript_partial": case "transcript_final":
    case "assistant_delta": case "tool_use": ingestMsg(ev); return;
  }
}

// Route a chat event into its workspace window; render live only if that window is focused.
function ingestMsg(ev) {
  const k = keyOf(ev), w = win(k), live = k === curKey();
  switch (ev.type) {
    case "transcript_partial":
      if (ev.role === "user" && live) { if (!partialUser) partialUser = bubble("user partial", ""); partialUser._body.textContent = ev.text; scroll(); }
      return;   // partials aren't buffered
    case "transcript_final":
      if (ev.role === "user") {
        w.msgs.push({ role: "user", text: ev.text });
        if (live) { if (partialUser) { partialUser.className = "msg user"; partialUser._body.textContent = ev.text; partialUser = null; } else bubble("user", ev.text); }
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
      if (live) { transcriptEl.appendChild(mkTool(ev.name, ev.detail)); scroll(); if (!appEl.classList.contains("chat-open")) $("chatToggle").classList.add("unread"); }
      break;
  }
  if (!live) { w.unread = true; markUnread(k); }
}

// ---- Parallel workspaces strip (each a project running its own Claude) ----
const WS_DOT = { idle: "idle", thinking: "busy", working: "busy", speaking: "busy" };
function renderWorkspaces(items, focused, max) {
  lastWorkspaces = items || lastWorkspaces;
  const bar = $("wsbar"); if (!bar) return;
  // Only clutter the HUD once there's more than one workspace (i.e. actual parallelism).
  if (lastWorkspaces.length <= 1) { bar.classList.add("empty"); bar.innerHTML = ""; return; }
  bar.classList.remove("empty"); bar.innerHTML = "";
  const lbl = document.createElement("span"); lbl.className = "wslbl";
  lbl.textContent = `WORKSPACES ${lastWorkspaces.length}/${max || 3}`;
  bar.appendChild(lbl);
  lastWorkspaces.forEach((w) => {
    const unread = win(w.project).unread && !w.focused;
    const pill = document.createElement("div");
    pill.className = "wspill" + (w.focused ? " foc" : "") + (w.state !== "idle" ? " active" : "") + (unread ? " unread" : "");
    pill.dataset.ws = w.project;
    const dot = document.createElement("i"); dot.className = "wsdot " + (WS_DOT[w.state] || "idle");
    const nm = document.createElement("span"); nm.className = "wsname"; nm.textContent = w.name;
    pill.append(dot, nm);
    if (w.state !== "idle") { const s = document.createElement("span"); s.className = "wsact"; s.textContent = w.state; pill.appendChild(s); }
    if (!w.focused) {
      pill.title = "Switch to " + w.name;
      pill.onclick = () => ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "focus_workspace", path: w.project }));
      const x = document.createElement("button"); x.className = "wsx"; x.textContent = "✕";
      x.title = "Close " + w.name;
      x.onclick = (e) => { e.stopPropagation(); ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "close_workspace", path: w.project })); };
      pill.appendChild(x);
    } else {
      pill.title = w.name + " · focused (spoken + live here)";
    }
    bar.appendChild(pill);
  });
}

// ---- Logs drawer (warnings/errors + Otto diagnostics, for debugging failures) ----
function fmtLogTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString([], { hour12: false });
}
function ingestLog(ev) {
  const list = $("logList"); if (!list) return;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const row = document.createElement("div");
  row.className = "logline " + (ev.level || "info");
  const t = document.createElement("span"); t.className = "lt"; t.textContent = fmtLogTime(ev.ts);
  const lv = document.createElement("span"); lv.className = "lv"; lv.textContent = (ev.level || "info").toUpperCase();
  const m = document.createElement("span"); m.className = "lm";
  m.textContent = (ev.logger && ev.logger !== "otto" ? ev.logger + ": " : "") + (ev.msg || "");
  row.append(t, lv, m);
  list.appendChild(row);
  while (list.childElementCount > 600) list.removeChild(list.firstChild);
  if (atBottom) list.scrollTop = list.scrollHeight;
  // Nudge the toggle when a warning/error arrives and the drawer is closed.
  if (!appEl.classList.contains("logs-open") && (ev.level === "error" || ev.level === "warning" || ev.level === "critical"))
    $("logsToggle").classList.add("unread");
}
function openLogs() {
  appEl.classList.remove("chat-open", "chats-open", "settings-open");
  appEl.classList.add("logs-open");
  $("logsToggle").classList.remove("unread");
  const list = $("logList"); if (list) list.scrollTop = list.scrollHeight;
}
$("logsToggle").onclick = () =>
  appEl.classList.contains("logs-open") ? appEl.classList.remove("logs-open") : openLogs();
$("logsClose").onclick = () => appEl.classList.remove("logs-open");
$("logsClear").onclick = () => { const l = $("logList"); if (l) l.innerHTML = ""; };

// ---- Setup / readiness panel (first-run permissions + a real Claude test run) ----
const CHK_ICON = { ok: "✓", fail: "✕", warn: "!", checking: "…", pending: "·" };
let seenReady = false;

function renderPreflight(ev) {
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
  // HUD pill
  const pill = $("readyPill");
  if (pill) { pill.classList.toggle("ok", !!ev.ready); pill.classList.toggle("bad", !ev.ready);
    pill.textContent = ev.ready ? "✓ Ready" : (ev.done ? "⚠ Setup" : "… Checking"); }
  // Title / subtitle
  if (ev.ready) {
    $("setupEmoji").textContent = "✅"; $("setupTitle").textContent = "Otto is ready";
    $("setupSub").textContent = "Everything checks out — say “hey Otto” or click the mic.";
  } else if (!ev.done) {
    $("setupEmoji").textContent = "🛠️"; $("setupTitle").textContent = "Setting up Otto…";
    $("setupSub").textContent = "Checking permissions and your Claude connection.";
  } else {
    $("setupEmoji").textContent = "⚠️"; $("setupTitle").textContent = "Otto needs one more step";
    $("setupSub").textContent = "Fix the items below, then click Recheck.";
  }
  $("recheckBtn").disabled = !ev.done;
  $("setupNote").textContent = ev.done ? "" : "Working…";
  // Auto-show while unfinished/blocked; auto-dismiss once it first goes green.
  if (!ev.done) { if (ev.first_run) showSetup(); }
  else if (!ev.ready) showSetup();
  else if (!seenReady && (ev.first_run || appEl.classList.contains("setup-open"))) {
    seenReady = true; setTimeout(hideSetup, 1800);
  }
}
function showSetup() { appEl.classList.add("setup-open"); $("setup").hidden = false; }
function hideSetup() { appEl.classList.remove("setup-open"); $("setup").hidden = true; }
$("readyPill").onclick = () => ($("setup").hidden ? showSetup() : hideSetup());
$("setupClose").onclick = hideSetup;
$("recheckBtn").onclick = () => {
  $("setupNote").textContent = "Rechecking…";
  ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: "recheck" }));
};

function showCanvas(content, format) {
  canvasEl.srcdoc = format === "markdown" ? mdToHtml(content) : content;
  stageEl.classList.add("has-content");
}

function mdToHtml(md) {
  const body = escapeHtml(md)
    .replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\*(.+?)\*/g, "<i>$1</i>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
  return `<style>body{font-family:-apple-system,sans-serif;background:#0a0f1c;color:#e6ecf6;padding:28px;line-height:1.6}code{background:#131a2b;padding:2px 5px;border-radius:4px}</style>${body}`;
}
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// conversation drawer
$("chatToggle").onclick = () => {
  appEl.classList.remove("settings-open", "chats-open", "logs-open");
  appEl.classList.toggle("chat-open"); $("chatToggle").classList.remove("unread"); scroll();
};
$("chatClose").onclick = () => appEl.classList.remove("chat-open");

// ---- Chats drawer (switch/create Claude chats in this project) ----
function timeAgo(sec) {
  if (!sec) return "";
  const d = Math.max(0, Date.now() / 1000 - sec);
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
async function loadSessions() {
  try {
    const r = await (await fetch("/api/sessions")).json();
    const el = $("chatList"); if (!el) return; el.innerHTML = "";
    (r.sessions || []).forEach((s) => {
      const row = document.createElement("div");
      row.className = "prow" + (s.current ? " cur" : "");
      const meta = document.createElement("div"); meta.className = "pmeta";
      meta.innerHTML = `<b>${escapeHtml(s.title)}</b><span>${escapeHtml(timeAgo(s.updated_at))}` +
        `${s.git_branch ? " · " + escapeHtml(s.git_branch) : ""}</span>`;
      const actions = document.createElement("div"); actions.className = "crow-actions";
      if (s.current) {
        const ren = document.createElement("button"); ren.className = "rename"; ren.textContent = "Rename";
        ren.onclick = async () => {
          const t = prompt("Rename this chat to:", s.title); if (!t) return;
          await fetch("/api/session/rename", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title: t }) });
          setTimeout(loadSessions, 800);
        };
        const cur = document.createElement("button"); cur.className = "ghost"; cur.textContent = "Current"; cur.disabled = true;
        actions.append(ren, cur);
      } else {
        const btn = document.createElement("button"); btn.textContent = "Open";
        btn.onclick = async () => {
          await fetch("/api/session/switch", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: s.session_id }) });
          setTimeout(loadSessions, 1400);
        };
        actions.append(btn);
      }
      row.append(meta, actions); el.appendChild(row);
    });
    if (!(r.sessions || []).length) el.innerHTML = '<p class="tiny">No chats yet — start one.</p>';
  } catch (_) {}
}
function openChats() {
  appEl.classList.remove("chat-open", "settings-open", "logs-open");
  appEl.classList.add("chats-open");
  loadSessions();
}
$("chatsToggle").onclick = () =>
  appEl.classList.contains("chats-open") ? appEl.classList.remove("chats-open") : openChats();
$("chatsClose").onclick = () => appEl.classList.remove("chats-open");
$("newChat").onclick = async () => {
  await fetch("/api/session/new", { method: "POST" });
  setTimeout(loadSessions, 1200);
};

// ---- Settings (inline, same dashboard) ----
let cfg = {};
function wakeLabel(v) {
  const s = v.stt || {}, on = v.input_enabled !== false;
  if (!on) return "MIC OFF";
  if (s.always_listen) return "ALWAYS ON";
  const w = (s.wake_words || "hey otto").trim();
  return "“" + w.toUpperCase() + "”";
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
  } catch (_) {}
}
async function loadProjects() {
  try {
    const r = await (await fetch("/api/projects")).json();
    const el = $("setProjects"); if (!el) return; el.innerHTML = "";
    (r.projects || []).forEach((p) => {
      const row = document.createElement("div");
      row.className = "prow" + (p.current ? " cur" : "");
      const meta = document.createElement("div"); meta.className = "pmeta";
      meta.innerHTML = `<b>${escapeHtml(p.name)}</b><span>${escapeHtml(p.path)}</span>`;
      const actions = document.createElement("div"); actions.className = "crow-actions";
      const btn = document.createElement("button");
      btn.textContent = p.current ? "Current" : "Switch";
      if (p.current) { btn.className = "ghost"; btn.disabled = true; }
      btn.onclick = async () => {
        await fetch("/api/project/switch", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: p.path }) });
        $("setMsg").textContent = "Switching…"; setTimeout(loadProjects, 1400);
      };
      actions.append(btn);
      if (!p.current) {   // open as a parallel (background) workspace, keeping this one
        const par = document.createElement("button"); par.className = "ghost"; par.textContent = "⇄ Parallel";
        par.title = "Run alongside — it works in the background and pings you when done";
        par.onclick = async () => {
          await fetch("/api/workspace/open", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: p.path, focus: false }) });
          $("setMsg").textContent = "Opened in parallel."; setTimeout(loadProjects, 1000);
        };
        actions.append(par);
      }
      row.append(meta, actions); el.appendChild(row);
    });
  } catch (_) {}
}
function openSettings() {
  appEl.classList.remove("chat-open", "chats-open", "logs-open");
  appEl.classList.add("settings-open");
  loadConfig(); loadProjects();
}
$("settingsToggle").onclick = () =>
  appEl.classList.contains("settings-open") ? appEl.classList.remove("settings-open") : openSettings();
$("setClose").onclick = () => appEl.classList.remove("settings-open");
$("setSave").onclick = async () => {
  cfg.brain = Object.assign(cfg.brain || {}, {
    model: $("setBrainModel").value.trim(),
    permission: $("setPerm").value,
    continue_conversation: $("setContinue").checked,
    register_mcp: $("setMcp").checked,
  });
  const v = (cfg.voice = cfg.voice || {});
  v.stt = v.stt || {}; v.tts = v.tts || {};
  v.input_enabled = $("setMic").checked;
  v.output_enabled = $("setSpeak").checked;
  v.stt.wake_words = $("setWake").value.trim();
  v.stt.model = $("setModel").value.trim();
  v.tts.engine = $("setTts").value;
  $("setMsg").textContent = "Saving…";
  try {
    const r = await (await fetch("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: cfg }) })).json();
    $("wakeBadge").textContent = wakeLabel(v);
    $("setMsg").textContent = r.note || "Saved.";
  } catch (_) { $("setMsg").textContent = "Save failed."; }
};
loadConfig();
// Menu bar "Settings…" deep-links here with #settings — open the drawer on arrival.
if (location.hash === "#settings") openSettings();
window.addEventListener("hashchange", () => { if (location.hash === "#settings") openSettings(); });

// mic (push-to-talk / arm capture)
$("mic").onclick = () => {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ type: "ptt" }));
    $("mic").classList.add("armed"); setTimeout(() => $("mic").classList.remove("armed"), 3000);
  }
};

// composer
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const i = $("input"), t = i.value.trim();
  if (!t || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "user_text", text: t })); i.value = "";
};

// HUD waveform bars (cyan -> purple gradient)
(function () {
  const w = $("wave");
  for (let i = 0; i < 26; i++) {
    const b = document.createElement("i");
    const t = i / 25;
    const c = t < 0.5 ? lerp([34, 211, 238], [59, 130, 246], t * 2) : lerp([59, 130, 246], [168, 85, 247], (t - 0.5) * 2);
    b.style.setProperty("--bar", `rgb(${c.join(",")})`);
    b.style.animationDelay = i * 35 + "ms";
    w.appendChild(b);
  }
})();
function lerp(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }

// idle standby clock
function tick() {
  const d = new Date();
  $("clock").textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("date").textContent = d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}
tick(); setInterval(tick, 15000);

connect();
