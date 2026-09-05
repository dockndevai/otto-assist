# Changelog

Feature-based notes per released tag. Orrin is the packaged product name; the code package
is `otto-assist`.

## Unreleased

**Persistent memory**
- **Orrin remembers across sessions** — a shared knowledge store under `OTTO_HOME/memory` that
  every session reads and writes, organized per project with a global tier for facts about you.
  Before each turn a bounded preamble (durable facts + a rolling summary) is injected so Claude
  has cross-session context; after each turn a fast background pass automatically decides what's
  worth keeping and folds it into `global.md` / `project.md` / `summary.md` (plus a durable
  `turns.jsonl` log). Fully automatic — no "remember this" required. Tunable/off under
  `memory:` in `config.yaml` (`enabled`, `inject`, `auto_capture`, `max_preamble_chars`,
  `max_summary_lines`, `capture_model`).

**Live feedback**
- **See what you're saying** — the dashboard now streams partial words as you speak (a small realtime model) and shows them as a live caption on the main stage, plus a clear “Listening…” state — so talking visibly updates the screen instead of only appearing after you finish. Toggle with `voice.stt.realtime_partials`.

**All-sessions access**
- **Open any chat from any project** — the Chats drawer has a new **All projects** scope with a
  search box that spans *every* Claude session on the machine (normal chats and cowork alike —
  cowork sessions are just projects with temp cwds). Picking one focuses that project and resumes
  that chat in a single step. By voice, "switch to `<name>`" now searches beyond the focused
  project and opens the match anywhere. (`GET /api/all-sessions`, `POST /api/session/open-anywhere`.)
  Sessions whose working directory has since been deleted aren't listed (they can't be resumed).

**Voice**
- **Pick Otto's voice** — Settings now lists every installed macOS voice (183 on a typical Mac,
  English first) in a dropdown with a **▶ Preview** button. Choosing one applies it live and
  saves it; no restart. (`GET /api/voices`, `POST /api/voice/{select,preview}`.)
- **Hands-free sleep/wake** — in always-listen, say **"stop listening"** (or "go to sleep",
  "be quiet") to pause; the mic stays on but only the **wake word** ("Otto") resumes it, back to
  fully hands-free.
- **No more self-echo loop** — strict half-duplex: Otto ignores the mic while it's speaking (and
  a short cooldown after), so on a laptop speaker it no longer hears its own voice and answer
  itself. The dashboard Talk button still bypasses this for push-to-talk.

## v0.2.6 — 2026-09-05

- **Seamless-install signing wired** — `build.sh` now signs the app inside-out (hardened runtime,
  secure timestamp) and notarizes + staples the DMG when `SIGN_ID` + `NOTARY_PROFILE` are set;
  `entitlements.plist` + `SIGNING.md` document the one-time Apple Developer setup for a warning-free,
  permission-persistent install.
- **Removed the Calendar/Upcoming card** — it was permission-gated (EventKit) and unreliable; Home
  is now Weather + GitHub Activity + Quick Actions.

## v0.2.5 — 2026-09-04

**Rename**
- The product is now **Orrin** across the UI (sidebar, menu bar, setup, status bar). **Otto** is
  the wake word.

**Redesigned dashboard**
- New product-grade layout: left **sidebar** (brand, Listening pill, nav — Home / Conversations /
  Visuals / Skills / MCP Connectors / Settings, push-to-talk), a **hero** card, a widget row, a
  right **Chat** drawer ("Ask Orrin anything"), a floating **listening bar**, and a bottom
  **status bar** (Claude Code · MCP connectors · Mode · TTS · version). Hamburger + responsive for
  phones; full **light/dark** theme with a toggle; a **mood color** (green/blue/red) that tints the
  orb by severity — automatic, or set by Orrin via a `set_mood` voice tool.

**Live data (new APIs, no keys)**
- **Weather** hero — IP geolocation + Open-Meteo current/daily + air quality (temp, condition,
  hi/lo, precip, wind, AQI).
- **GitHub Activity** — `gh` for the focused repo (open PRs, issues assigned, commits in 7 days).
- **Upcoming** — calendar events via EventKit (needs Calendar access; requested up front).
- Status bar shows the real MCP connector count.
- Cards render only when they have data — no empty placeholder boxes.

**Voice reliability (from 0.2.4.x fixes)**
- Hardware half-duplex mic mute while Orrin speaks (cures the self-echo loop); hands-free
  sleep/wake ("stop listening" → wake word); lower latency (int8/CPU, greedy beam, ~1.3s silence);
  pick any macOS voice with live preview; open any chat from any project (cowork included).

## v0.2.4 — 2026-09-03

**Setup & reliability**
- **First-run permissions + "Otto ready" health check** — on launch Otto now *asks* macOS for
  microphone access up front (the system prompt) instead of letting the recorder subprocess trip
  a silent denial, and only starts the ears once access is actually granted. A setup panel in the
  dashboard shows a live checklist — Microphone, Claude CLI, a real **Claude test turn**, Voice
  output, Accessibility — with "Open Settings" and "Recheck" actions, and a ✓ Ready / ⚠ Setup pill
  in the HUD. The checks re-run on every start/restart, so a missing permission is visible instead
  of a silent "Otto isn't listening". Granting the mic then clicking Recheck brings the ears up with
  no restart.

## v0.2.3 — 2026-09-03

**Dashboard**
- **Logs drawer** — warnings/errors and Otto's own diagnostics stream to the dashboard (with a
  replayable buffer), so failures are visible without opening a log file. Level-colored, the
  toggle flags unread on a problem, and known RealtimeSTT poll-noise is filtered out.

## v0.2.2 — 2026-09-03

**Distribution & CI**
- **GitHub Actions macOS build** — builds `Orrin.dmg` on an Apple-Silicon runner (manual, or
  automatically on a `v*` tag), uploads it as an artifact and attaches it to the release.
- **Real installer background baked in** — the DMG ships a pre-made background image (no
  build-time generation), so the drag-to-Applications window looks finished.
- **Headless-safe build** — the Finder window-arrangement step is bounded by a timeout and a
  detach retry, so CI/headless builds never hang; they still produce a working DMG with the
  `/Applications` symlink.
- **Non-clobbering release upload** — a locally built DMG (with the arranged installer window)
  stays authoritative; CI won't overwrite it.

## v0.2.1 — 2026-09-02

**Chats**
- **Resume in place** — when Otto works in a project it now continues that project's existing
  Claude session instead of forking a new one, so the conversation shows up in the same chat
  you reopen in Claude Code.

**Installer**
- **Real drag-to-Applications window** — the DMG opens as a proper installer: the app beside an
  `/Applications` symlink on a styled background.

**Fixes**
- **Packaged-app mic crash fixed** — the global push-to-talk hotkey (pynput) was aborting the
  bundled app (SIGTRAP from off-main-thread Carbon input-source calls); it's now opt-in
  (`voice.hotkey.enabled`, default off). Voice input still works via the wake word and the
  dashboard mic button.
- Build script robustness: defined `PY` for the background step; bounded the Finder step so it
  can't hang.

## v0.2.0 — 2026-09-02

**Parallel workspaces**
- Run **several projects' Claudes at once**, each in its **own chat window** and its own session
  (its own CLI subprocess → true concurrency).
- **Targeting** — a focused workspace takes commands by default; **"in `<name>`, …"** sends work
  to another workspace in the background.
- **Focused speaks, background notifies** — only the focused workspace streams to screen + voice;
  background ones work silently and post a completion notification. Every workspace streams into
  its own window, so you can switch to any of them.
- **Clone-to-workspace** — **"clone `<url>`"** clones a repo into a fresh workspace and focuses it.

**Chats within a project**
- Switch between a project's Claude sessions, start a new chat, or rename one — by voice or from
  the dashboard/menu bar.

**Dashboard & voice**
- **In-app dashboard** — opens in a native macOS window (WKWebView), not the browser.
- **Voice-driven Claude Code** — your real Claude Code work (tool calls, output, code) shows in
  the chat window; the spoken reply is a short, natural summary; a visual canvas is used only when
  it genuinely helps.

**Packaging & fixes**
- Standalone app's voice works: torch bundled + `multiprocessing.freeze_support()` +
  ONNX Silero VAD (no `torch.hub` download/trust prompt).
- Fixed a chat-swap race (a command right after "new chat" could hit the old session).

## 0.1.0 — baseline

The core assistant: a realtime, voice-driven macOS menu-bar app that wraps **your own Claude
Code** (via the Claude Agent SDK) — talk to it, watch a visual canvas, and it uses your existing
Claude setup (MCP servers, subagents, skills, `CLAUDE.md`, permissions). Local Whisper ears,
system/ElevenLabs voice, a web dashboard, and a per-action permission model.
