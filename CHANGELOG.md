# Changelog

Feature-based notes per released tag. Orrin is the packaged product name; the code package
is `otto-assist`.

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
