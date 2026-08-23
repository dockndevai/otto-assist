// Dashboard client: streams events from the server over WebSocket and renders the
// transcript, status orb, and canvas. Sends typed input back.

const $ = (id) => document.getElementById(id);
const transcriptEl = $("transcript");
const statusEl = $("status");
const orbEl = $("orb");
const canvasEl = $("canvas");
const canvasEmpty = $("canvasEmpty");

let ws;
let partialUser = null;      // live partial-transcript bubble
let assistantBubble = null;  // growing assistant bubble during streaming

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => setStatus("idle", "Ready");
  ws.onclose = () => { setStatus("idle", "disconnected — reconnecting…"); setTimeout(connect, 1000); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function setStatus(state, text) {
  statusEl.textContent = text || state;
  orbEl.className = "orb " + (state || "");
}

function bubble(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.textContent = text;
  transcriptEl.appendChild(el);
  scroll();
  return el;
}

function scroll() { transcriptEl.scrollTop = transcriptEl.scrollHeight; }

function handle(ev) {
  switch (ev.type) {
    case "status":
      setStatus(ev.state, ev.text);
      break;

    case "transcript_partial":
      if (!partialUser) { partialUser = bubble("user partial", ""); }
      partialUser.textContent = ev.text;
      scroll();
      break;

    case "transcript_final":
      if (ev.role === "user") {
        if (partialUser) { partialUser.className = "msg user"; partialUser.textContent = ev.text; partialUser = null; }
        else { bubble("user", ev.text); }
      } else {
        // Assistant final: close out the streaming bubble.
        if (assistantBubble) { assistantBubble.textContent = ev.text; assistantBubble = null; }
        else { bubble("assistant", ev.text); }
      }
      break;

    case "assistant_delta":
      if (!assistantBubble) { assistantBubble = bubble("assistant", ""); }
      assistantBubble.textContent += ev.text;
      scroll();
      break;

    case "tool_use": {
      const el = document.createElement("div");
      el.className = "tool";
      el.innerHTML = `<b>${ev.name}</b>${ev.detail ? " · " + escapeHtml(ev.detail) : ""}`;
      transcriptEl.appendChild(el);
      scroll();
      break;
    }

    case "canvas_render":
      showCanvas(ev.content, ev.format);
      break;

    case "ask_user":
      // The question also arrives as an assistant transcript; nothing extra to do,
      // but we nudge focus to the input for a quick typed reply.
      $("input").focus();
      break;

    case "notify":
      bubble("assistant", `🔔 ${ev.title}${ev.body ? " — " + ev.body : ""}`);
      break;
  }
}

function showCanvas(content, format) {
  const html = format === "markdown" ? mdToHtml(content) : content;
  canvasEl.srcdoc = html;
  canvasEl.classList.remove("empty");
  canvasEmpty.classList.add("hidden");
}

function mdToHtml(md) {
  // Minimal, safe-ish markdown for when the agent sends markdown instead of HTML.
  const esc = escapeHtml(md);
  const body = esc
    .replace(/^### (.*)$/gm, "<h3>$1</h3>")
    .replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\*(.+?)\*/g, "<i>$1</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
  return `<style>body{font-family:-apple-system,sans-serif;padding:24px;color:#111;line-height:1.5}</style>${body}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("input");
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "user_text", text }));
  input.value = "";
});

connect();
