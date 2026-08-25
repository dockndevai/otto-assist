// JARVIS-style dashboard: the canvas is the stage; the conversation lives in a drawer.
const $ = (id) => document.getElementById(id);
const appEl = $("app");
const stageEl = document.querySelector(".stage");
const canvasEl = $("canvas");
const transcriptEl = $("transcript");

let ws, partialUser = null, assistantBubble = null;

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

function bubble(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  const who = document.createElement("div"); who.className = "who";
  const b = document.createElement("b"); b.textContent = role.includes("user") ? "You" : "Otto";
  const t = document.createElement("span"); t.className = "t"; t.textContent = fmtTime();
  who.append(b, t);
  const body = document.createElement("div"); body.textContent = text;
  el.append(who, body); el._body = body;
  transcriptEl.appendChild(el); scroll();
  if (!appEl.classList.contains("chat-open")) $("chatToggle").classList.add("unread");
  return el;
}
function scroll() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }

function handle(ev) {
  switch (ev.type) {
    case "status": setStatus(ev.state, ev.text); break;
    case "history_start": transcriptEl.innerHTML = ""; break;
    case "history_end": {
      const d = document.createElement("div"); d.className = "divider"; d.textContent = "— now —";
      transcriptEl.appendChild(d); scroll(); break;
    }
    case "transcript_partial":
      if (!partialUser) partialUser = bubble("user partial", "");
      partialUser._body.textContent = ev.text; scroll(); break;
    case "transcript_final":
      if (ev.role === "user") {
        if (partialUser) { partialUser.className = "msg user"; partialUser._body.textContent = ev.text; partialUser = null; }
        else bubble("user", ev.text);
      } else {
        if (assistantBubble) { assistantBubble._body.textContent = ev.text; assistantBubble = null; }
        else bubble("assistant", ev.text);
      }
      break;
    case "assistant_delta":
      if (!assistantBubble) assistantBubble = bubble("assistant", "");
      assistantBubble._body.textContent += ev.text; scroll(); break;
    case "tool_use": {
      const el = document.createElement("div"); el.className = "tool";
      el.innerHTML = `<b>${escapeHtml(ev.name)}</b>${ev.detail ? " · " + escapeHtml(ev.detail) : ""}`;
      transcriptEl.appendChild(el); scroll(); break;
    }
    case "canvas_render": showCanvas(ev.content, ev.format); break;
    case "ask_user": $("input").focus(); break;
    case "notify": bubble("assistant", `🔔 ${ev.title}${ev.body ? " — " + ev.body : ""}`); break;
    case "project": { const el = $("proj"); if (el) el.textContent = "📁 " + (ev.name || ev.path || "—"); break; }
  }
}

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

// chat drawer
$("chatToggle").onclick = () => { appEl.classList.toggle("chat-open"); $("chatToggle").classList.remove("unread"); scroll(); };
$("chatClose").onclick = () => appEl.classList.remove("chat-open");

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
