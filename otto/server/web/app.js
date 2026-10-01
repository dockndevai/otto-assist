/* Orrin — Liquid Glass dashboard front-end.
   Consumes the /ws event stream and renders an ambient, context-driven surface:
   a reactive orb (presence), floating task clusters (one per turn/workspace) whose
   sub-steps cascade with depth, faint floating research when idle, and tiny top
   system icons that only flare on an alert. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;
  const css = (v) => getComputedStyle(root).getPropertyValue(v).trim();
  const el = (h) => { const d = document.createElement("div"); d.innerHTML = h.trim(); return d.firstChild; };
  const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---------------- reactive orb ---------------- */
  const cv = $("orb"), ctx = cv.getContext("2d"), N = cv.width;
  let mode = "idle", t = 0, energy = 0;
  const trip = (n) => css("--b" + n).split(",").map(Number);
  const pal = () => { const a = trip(1), b = trip(2), c = trip(3); return { idle: [a, b], listening: [a, c], thinking: [b, c], working: [b, a], speaking: [a, b] }; };
  function draw() {
    t += 0.016;
    const tg = mode === "idle" ? 0.26 : mode === "listening" ? 0.7 : (mode === "thinking" || mode === "working") ? 0.92 : 0.85;
    energy += (tg - energy) * 0.06;
    ctx.clearRect(0, 0, N, N);
    const cx = N / 2, cy = N / 2, base = N * 0.3, C = pal()[mode] || pal().idle;
    let g = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, base * 1.9);
    g.addColorStop(0, `rgba(${C[0]},.55)`); g.addColorStop(0.5, `rgba(${C[1]},.22)`); g.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, base * 1.9, 0, 7); ctx.fill();
    for (let r = 0; r < 3; r++) {
      ctx.beginPath(); const rr = base * (0.72 + r * 0.16);
      for (let a = 0; a <= 6.4; a += 0.12) {
        const w = reduce ? 0 : (Math.sin(a * 3 + t * (1.1 + r * 0.4)) * 0.06 + Math.sin(a * 5 - t * 0.8) * 0.04) * (0.4 + energy);
        const rad = rr * (1 + w), x = cx + Math.cos(a) * rad, y = cy + Math.sin(a) * rad;
        a === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.closePath(); ctx.strokeStyle = `rgba(${C[r % 2]},${0.16 + 0.12 * (3 - r)})`; ctx.lineWidth = 2; ctx.stroke();
    }
    let cg = ctx.createRadialGradient(cx - base * 0.2, cy - base * 0.2, base * 0.05, cx, cy, base * 0.95);
    cg.addColorStop(0, "rgba(255,255,255,.95)"); cg.addColorStop(0.35, `rgba(${C[0]},.85)`); cg.addColorStop(1, `rgba(${C[1]},.3)`);
    ctx.fillStyle = cg; ctx.beginPath(); ctx.arc(cx, cy, base * (0.9 + energy * 0.05), 0, 7); ctx.fill();
    if (mode === "speaking" && !reduce) {
      ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, base * 0.9, 0, 7); ctx.clip();
      ctx.strokeStyle = "rgba(255,255,255,.7)"; ctx.lineWidth = 2; ctx.beginPath();
      for (let x = 0; x <= N; x += 6) { const y = cy + Math.sin(x * 0.05 + t * 6) * base * 0.16 * Math.sin(t * 2 + x * 0.01); x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }
      ctx.stroke(); ctx.restore();
    }
    requestAnimationFrame(draw);
  }
  draw();

  const stateEl = $("state"), statePillTxt = $("statePillTxt"), stateDot = $("stateDot");
  const LABEL = { listening: "Listening", thinking: "Thinking", working: "Working", speaking: "Speaking", starting: "Starting", idle: "Ready", sleeping: "Sleeping" };
  function setState(state, text) {
    mode = state === "starting" ? "idle" : (state || "idle");
    const live = state && state !== "idle";
    const label = text || LABEL[state] || "Ready";
    stateEl.innerHTML = live
      ? `<span class="wavedots"><span></span><span></span><span></span><span></span></span> <b>${esc(label)}</b>`
      : `<span>${esc(label)}</span>`;
    statePillTxt.textContent = LABEL[state] || "Ready";
    stateDot.style.background = state === "listening" ? "var(--accent-1)" : state === "speaking" ? "var(--accent-2)"
      : (state === "thinking" || state === "working") ? "var(--warn)" : "var(--good)";
  }

  /* ---------------- greeting ---------------- */
  function timeGreeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
  let userName = "";
  function setGreet() { $("greet").textContent = timeGreeting() + (userName ? ", " + userName : ""); }
  setGreet(); setInterval(setGreet, 5 * 60 * 1000);

  /* ---------------- floating clusters ---------------- */
  const layer = $("float");
  const ANCHORS = [
    { css: { top: "11%", left: "2%" }, fx: "9px", fy: "-11px", fd: "12s" },
    { css: { top: "9%", right: "3%" }, fx: "-8px", fy: "10px", fd: "14s" },
    { css: { bottom: "16%", left: "4%" }, fx: "8px", fy: "9px", fd: "13s" },
    { css: { bottom: "14%", right: "4%" }, fx: "-9px", fy: "-9px", fd: "15s" },
    { css: { top: "40%", left: "0%" }, fx: "7px", fy: "8px", fd: "12.5s" },
    { css: { top: "44%", right: "0%" }, fx: "-7px", fy: "-8px", fd: "13.5s" },
  ];
  const usedAnchors = new Set();
  function takeAnchor() { for (let i = 0; i < ANCHORS.length; i++) if (!usedAnchors.has(i)) { usedAnchors.add(i); return i; } return Math.floor(Math.random() * ANCHORS.length); }

  const clusters = new Map(); // key -> {el, deck, reply, timer, anchorIdx, done}
  const MAIN = "__main__";
  let focusedWs = MAIN;

  function iconFor(key, title) {
    return key !== MAIN ? "🧩" : "🐚";
  }
  function ensureCluster(key, title) {
    let c = clusters.get(key);
    if (c) { if (title) c.el.querySelector(".nm").textContent = title; return c; }
    const ai = takeAnchor(), an = ANCHORS[ai];
    const node = el(`<div class="cluster"><div class="ch"><span class="ic">${iconFor(key, title)}</span><span class="nm">${esc(title || "Working")}</span><span class="st">running</span></div><div class="deck"></div></div>`);
    Object.assign(node.style, an.css);
    node.style.setProperty("--fx", an.fx); node.style.setProperty("--fy", an.fy); node.style.setProperty("--fd", an.fd);
    layer.appendChild(node);
    c = { el: node, deck: node.querySelector(".deck"), reply: null, timer: null, anchorIdx: ai, done: false };
    clusters.set(key, c);
    return c;
  }
  function pushBehind(deck) {
    const kids = [...deck.children], n = kids.length;
    kids.forEach((ch, i) => { const lvl = n - 1 - i; if (lvl >= 1) ch.setAttribute("data-lvl", Math.min(lvl, 4)); else ch.removeAttribute("data-lvl"); });
    kids.forEach((ch) => { if (+ch.getAttribute("data-lvl") >= 4) setTimeout(() => ch.remove(), 500); });
  }
  function addBlk(c, cls, html) {
    const b = el(`<div class="blk ${cls}">${html || ""}</div>`);
    c.deck.appendChild(b); pushBehind(c.deck);
    requestAnimationFrame(() => b.classList.add("in"));
    bump(c);
    return b;
  }
  function bump(c) { // reset inactivity timer; auto-float-away after quiet
    if (c.timer) clearTimeout(c.timer);
    c.timer = setTimeout(() => closeCluster(clusterKey(c)), 6500);
  }
  function clusterKey(c) { for (const [k, v] of clusters) if (v === c) return k; return MAIN; }
  function markDone(key) { const c = clusters.get(key); if (!c || c.done) return; c.done = true; const st = c.el.querySelector(".st"); st.textContent = "done"; st.classList.add("done"); if (c.timer) clearTimeout(c.timer); c.timer = setTimeout(() => closeCluster(key), 4000); }
  function closeCluster(key) {
    const c = clusters.get(key); if (!c) return;
    if (c.timer) clearTimeout(c.timer);
    c.el.classList.add("out"); usedAnchors.delete(c.anchorIdx);
    clusters.delete(key);
    setTimeout(() => c.el.remove(), 560);
  }

  /* ---------------- ambient research ghosts (idle) ---------------- */
  const ghosts = new Map(); // name -> node
  function researchOn(name) {
    if (ghosts.has(name)) return;
    const free = ANCHORS.filter((_, i) => !usedAnchors.has(i));
    const an = free[ghosts.size % (free.length || 1)] || ANCHORS[ghosts.size % ANCHORS.length];
    const node = el(`<div class="ghost"><div class="gh"><span class="rd"></span>Researching · ${esc(name)}</div><div class="gt">Reading files, git log & searching — read-only. Findings saved to memory.</div></div>`);
    Object.assign(node.style, an.css);
    node.style.setProperty("--fx", an.fx); node.style.setProperty("--fy", an.fy); node.style.setProperty("--fd", an.fd);
    layer.appendChild(node); ghosts.set(name, node);
    requestAnimationFrame(() => node.classList.add("in"));
  }
  function researchOff(name) {
    if (name && ghosts.has(name)) { const g = ghosts.get(name); g.classList.remove("in"); setTimeout(() => g.remove(), 1000); ghosts.delete(name); return; }
    if (!name) ghosts.forEach((g, k) => { g.classList.remove("in"); setTimeout(() => g.remove(), 1000); ghosts.delete(k); });
  }

  /* ---------------- memory pulse ---------------- */
  let memTimer = null;
  function savedPulse(txt) { const m = $("memtag"); $("memtxt").textContent = txt || "saved to memory"; m.classList.add("show"); if (memTimer) clearTimeout(memTimer); memTimer = setTimeout(() => m.classList.remove("show"), 2400); }

  /* ---------------- tool-block shaping ---------------- */
  function addToolBlock(c, name, detail) {
    const leaf = (name || "").split("__").pop();
    if (name === "Bash") { // show the command as a code block
      addBlk(c, "code", `<span style="color:#8fa3c9">$</span> ${esc(detail || "")}`);
      return;
    }
    const label = detail ? `${esc(leaf)} · ${esc(detail)}` : esc(leaf);
    const b = addBlk(c, "tool", `<span class="sp"></span><span class="tx">${label}</span>`);
    // mark done shortly (we only get the call, not the result) so it doesn't spin forever
    setTimeout(() => { if (b.isConnected) { b.classList.add("ok"); b.innerHTML = `<span class="tx">${label}</span>`; } }, 1400);
  }

  /* ---------------- event router ---------------- */
  let replaying = false;
  function route(ev) {
    switch (ev.type) {
      case "history_start": replaying = true; return;
      case "history_end": replaying = false; return;
      case "status": setState(ev.state, ev.text);
        if (ev.state === "idle") markDone(MAIN);
        return;
      case "project": if (ev.name) $("projPill").textContent = ev.name; return;
      case "session": return;
      case "workspaces": if (ev.focused) focusedWs = ev.focused; return;
      case "transcript_partial":
        if (replaying) return;
        if (ev.role === "user") { const c = ensureCluster(MAIN, shortTitle(ev.text)); ensureYou(c, ev.text, true); }
        return;
      case "transcript_final":
        if (replaying) return;
        if (ev.role === "user") { const c = ensureCluster(MAIN, shortTitle(ev.text)); ensureYou(c, ev.text, false); c.reply = null; }
        else { const key = ev.ws || MAIN; const c = ensureCluster(key); finalizeReply(c, ev.text); }
        return;
      case "assistant_delta":
        if (replaying) return;
        { const key = ev.ws || MAIN; const c = ensureCluster(key); streamReply(c, ev.text); }
        return;
      case "tool_use":
        if (replaying) return;
        { const key = ev.ws || MAIN; const c = ensureCluster(key); addToolBlock(c, ev.name, ev.detail); }
        return;
      case "research":
        if (ev.active) researchOn(ev.name || "your projects");
        else { researchOff(ev.name); if (ev.name) savedPulse("saved · " + ev.name); }
        return;
      case "ask_user":
        { const c = ensureCluster(MAIN, "Orrin needs you"); const b = addBlk(c, "reply", esc(ev.question || "")); b.style.borderLeft = "3px solid var(--warn)"; $("cmdInput").focus(); }
        return;
      case "canvas_render": renderCanvas(ev); return;
      case "suggestion": if (!replaying) { placeSuggestion(ev.item, { flash: true }); savedPulse("new suggestion"); } return;
      case "alert": if (!replaying) { placeSuggestion(ev.item, { flash: true }); } return;
      case "suggestion_resolved": if (!replaying) { removeSuggestion(ev.id); savedPulse("resolved · suggestion"); } return;
      case "roadmap": if (!replaying) { roadmapText = ev.text || roadmapText; renderRoadmap(); savedPulse("roadmap updated"); } return;
      case "notify": toastNotify(ev.title, ev.body); return;
      case "preflight": renderPreflight(ev); return;
      case "mood": return; // reserved
      case "log": return;  // logs not surfaced in the ambient view
    }
  }
  function shortTitle(txt) { txt = (txt || "").trim(); return txt.length > 40 ? txt.slice(0, 38) + "…" : (txt || "Working"); }
  function ensureYou(c, text, partial) {
    if (!c._you) c._you = addBlk(c, "you", "");
    c._you.textContent = "“" + (text || "") + "”";
    if (!partial) c._you = null; // finalize
  }
  function streamReply(c, delta) {
    if (!c.reply) c.reply = addBlk(c, "reply", "");
    c.reply.textContent = (c.reply.textContent || "") + (delta || "");
    bump(c);
  }
  function finalizeReply(c, text) {
    if (text) { if (!c.reply) c.reply = addBlk(c, "reply", ""); c.reply.textContent = text; }
    c.reply = null; bump(c);
  }

  /* canvas: model HTML in a sandboxed CSP iframe (blocks exfiltration) */
  const CANVAS_CSP = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data: blob:; media-src data: blob:; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; font-src data:; connect-src \'none\'; form-action \'none\'; base-uri \'none\'">';
  function renderCanvas(ev) {
    const c = ensureCluster("__canvas__", "Canvas");
    if (c._frameHost) c._frameHost.remove();
    const host = addBlk(c, "canvas", `<iframe sandbox="allow-scripts" title="canvas"></iframe>`);
    c._frameHost = host;
    const html = ev.format === "markdown" ? mdToHtml(ev.content || "") : (ev.content || "");
    host.querySelector("iframe").srcdoc = CANVAS_CSP + html;
    if (c.timer) clearTimeout(c.timer); c.timer = setTimeout(() => closeCluster("__canvas__"), 30000);
  }
  function mdToHtml(md) {
    return "<div style='font:14px -apple-system,sans-serif;color:#1b2438;padding:14px;line-height:1.5'>" +
      esc(md).replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>")
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>").replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>") + "</div>";
  }

  /* notify → short-lived floating note that drifts away */
  function toastNotify(title, body) {
    const ai = takeAnchor(), an = ANCHORS[ai];
    const node = el(`<div class="cluster"><div class="ch"><span class="ic">🔔</span><span class="nm">${esc(title || "Notice")}</span></div><div class="deck"><div class="blk note in">${esc(body || "")}</div></div></div>`);
    Object.assign(node.style, an.css); node.style.setProperty("--fx", an.fx); node.style.setProperty("--fy", an.fy); node.style.setProperty("--fd", an.fd);
    layer.appendChild(node);
    setTimeout(() => { node.classList.add("out"); usedAnchors.delete(ai); setTimeout(() => node.remove(), 560); }, 6000);
  }

  /* ---------------- background work: SEPARATE panels by type ----------------
     Idle research is transient (a pass runs for seconds while you're away), so its output must
     stay visible on the Home. It's split into three faint floating panels — Risks (⚠), Ideas &
     next steps (💡), and the daily Roadmap (🗺) — each in its own corner, compact one-line rows,
     readable, no overlap. Reconciled from /api/suggestions + /api/roadmap and live WS events. */
  const SUGG_MAX = 5;
  const suggItems = new Map(); // id -> item
  let roadmapText = "", flashId = null;
  const suggRank = (it) => ({ high: 20, medium: 10, low: 0 }[it.severity] || 0);

  function bgPanel(cls, icon, title) {
    let p = layer.querySelector("." + cls);
    if (!p) {
      p = el(`<div class="bgpanel ${cls}"><div class="bph"><span class="bpi">${icon}</span>${title}</div><div class="bpbody"></div></div>`);
      layer.appendChild(p);
    }
    return p;
  }
  function dropPanel(cls) { const p = layer.querySelector("." + cls); if (p) p.remove(); }
  function suggRowsHtml(items) {
    const shown = items.slice(0, SUGG_MAX), extra = items.length - shown.length;
    const rows = shown.map((it) => {
      const raw = (it.text || "").replace(/^FEATURE:\s*/i, "");
      const t = raw.length > 96 ? raw.slice(0, 94) + "…" : raw;
      return `<div class="bprow${it.id === flashId ? " flash" : ""}"><b>${esc(it.name || "?")}</b> ${esc(t)}</div>`;
    }).join("");
    return rows + (extra > 0 ? `<div class="bpmore">+${extra} more</div>` : "");
  }
  function renderSuggPanel() {
    const all = [...suggItems.values()];
    const risks = all.filter((x) => x.kind === "risk").sort((a, b) => suggRank(b) - suggRank(a) || (b.ts || 0) - (a.ts || 0));
    const ideas = all.filter((x) => x.kind !== "risk").sort((a, b) => (b.ts || 0) - (a.ts || 0));
    if (risks.length) bgPanel("panel-risks", "⚠", "Risks").querySelector(".bpbody").innerHTML = suggRowsHtml(risks);
    else dropPanel("panel-risks");
    if (ideas.length) bgPanel("panel-ideas", "💡", "Ideas &amp; next steps").querySelector(".bpbody").innerHTML = suggRowsHtml(ideas);
    else dropPanel("panel-ideas");
  }
  function renderRoadmap() {
    const lines = (roadmapText || "").split("\n")
      .map((l) => l.trim()).filter((l) => l && !l.startsWith("#")).slice(0, 7);
    if (!lines.length) { dropPanel("panel-roadmap"); return; }
    const body = lines.map((l) => {
      const t = l.length > 104 ? l.slice(0, 102) + "…" : l;
      return `<div class="bprow">${esc(t)}</div>`;
    }).join("");
    bgPanel("panel-roadmap", "🗺", "Roadmap").querySelector(".bpbody").innerHTML = body;
  }
  function placeSuggestion(item, opts) {
    if (!item || !item.id) return;
    suggItems.set(item.id, item);
    if (opts && opts.flash) { flashId = item.id; setTimeout(() => { if (flashId === item.id) { flashId = null; renderSuggPanel(); } }, 1400); }
    renderSuggPanel();
  }
  function removeSuggestion(id) { if (suggItems.delete(id)) renderSuggPanel(); }
  function loadSuggestions() {
    fetch("/api/suggestions").then((r) => r.json()).then((d) => {
      suggItems.clear();
      (d.items || []).forEach((it) => { if (it && it.id) suggItems.set(it.id, it); });
      renderSuggPanel();
    }).catch(() => {});
  }
  function loadRoadmap() {
    fetch("/api/roadmap").then((r) => r.json()).then((d) => { roadmapText = d.roadmap || ""; renderRoadmap(); }).catch(() => {});
  }

  /* ---------------- alerts (preflight) as tiny top icons ---------------- */
  const PANE = { microphone: "microphone", accessibility: "input_monitoring", screen_recording: "screen_recording", claude_cli: "", claude_test: "", voice_output: "" };
  function renderPreflight(ev) {
    const checks = ev.checks || [];
    const failing = checks.filter((c) => c.status === "fail" || (c.required && c.status !== "ok" && c.status !== "warn"));
    const mic = checks.find((c) => c.key === "microphone");
    const siMic = $("siMic"); if (mic) { siMic.querySelector(".v").textContent = mic.status === "ok" ? "on" : (mic.status || "?"); siMic.classList.toggle("warn", mic.status !== "ok"); }
    const siReady = $("siReady");
    if (failing.length) {
      siReady.classList.add("alert"); siReady.querySelector(".ico").textContent = "⚠";
      siReady.querySelector(".v").textContent = failing[0].title || failing[0].key;
      siReady.onclick = () => { const pane = PANE[failing[0].key]; if (pane) send({ type: "open_pane", pane }); else send({ type: "recheck" }); };
    } else {
      siReady.classList.remove("alert"); siReady.querySelector(".ico").textContent = "✓"; siReady.querySelector(".v").textContent = "ready"; siReady.onclick = null;
    }
  }

  /* ---------------- controls ---------------- */
  let ws = null, wsReady = false;
  function send(obj) { try { if (ws && wsReady) ws.send(JSON.stringify(obj)); } catch (e) {} }
  $("dock").addEventListener("submit", (e) => { e.preventDefault(); const inp = $("cmdInput"); const v = inp.value.trim(); if (!v) return; send({ type: "user_text", text: v }); inp.value = ""; });
  $("stopBtn").onclick = () => send({ type: "stop" });
  let micMuted = false, voiceMuted = false;
  $("muteMic").onclick = function () { micMuted = !micMuted; this.classList.toggle("on", micMuted); this.textContent = micMuted ? "🔇" : "🎙"; send({ type: "mute_mic", value: micMuted }); };
  $("muteVoice").onclick = function () { voiceMuted = !voiceMuted; this.classList.toggle("on", voiceMuted); this.textContent = voiceMuted ? "🔈" : "🔊"; send({ type: "mute_voice", value: voiceMuted }); };
  $("settingsBtn").onclick = () => { location.href = "/settings"; };
  $("seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; root.setAttribute("data-v", b.dataset.v); [...e.currentTarget.children].forEach((x) => x.classList.toggle("on", x === b)); try { localStorage.setItem("orrin-variant", b.dataset.v); } catch (e2) {} });
  (function initVariant() { let v = "aurora"; try { v = localStorage.getItem("orrin-variant") || "aurora"; } catch (e) {} [...$("seg").children].forEach((x) => x.classList.toggle("on", x.dataset.v === v)); })();

  /* ---------------- websocket ---------------- */
  const conn = $("conn");
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsReady = true; conn.classList.remove("show"); setState("idle"); loadSuggestions(); loadRoadmap(); };
    ws.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch (e) { return; } try { route(ev); } catch (e) {} };
    ws.onclose = () => { wsReady = false; conn.classList.add("show"); setTimeout(connect, 1500); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  connect();
})();
