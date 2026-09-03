# Orrin — features

Orrin (code package `otto-assist`) is a JARVIS-style **voice + screen layer over your own
Claude Code**. You talk to it instead of typing; it does the real work in your repos using your
existing Claude setup, shows that work on a dashboard, and speaks a short reply. It can drive
several projects at once, each as its own chat.

---

## Bring-your-own-Claude
- Wraps **your own Claude** via the Claude Agent SDK — no separate model or API key; it uses your
  Claude Code login.
- Loads your existing configuration: **MCP servers, subagents, skills, `CLAUDE.md`, permissions**
  (`setting_sources`). Otto behaves exactly like your Claude, just spoken — it only adds voice
  manners + its own voice/screen tools.

## Talk to it (voice) — or type
- **Wake word** — say **"Otto, …"** then your request.
- **Dashboard mic button** — click 🎤 and speak.
- **Type** — the composer box does the same thing; no mic needed.
- **Push-to-talk hotkey** (⌘⇧J) — opt-in (`voice.hotkey.enabled`), needs macOS Accessibility.
- Local **Whisper** speech-to-text; **system voice or ElevenLabs** text-to-speech.
- **Smart, brief spoken replies** — one or two natural sentences (the takeaway), not a play-by-play;
  the detail stays on screen.

## The dashboard (its screen)
- **In-app native window** (WKWebView) — opens from the menu bar, not the browser.
- **Chat window per workspace** — your turns, Claude's tool calls, output and code stream here,
  like a normal Claude Code session.
- **Canvas** — an optional big visual (chart, table, diagram, clock/weather, a highlighted answer)
  shown only when a picture beats text; routine coding stays in the chat.
- **HUD** — current project + chat, live state (listening / thinking / speaking / working).

## Parallel workspaces (several projects at once)
- A **bounded pool (up to 3)** of workspaces — each a project running **its own Claude** in its
  own subprocess → genuine concurrency.
- **Focused workspace** takes commands by default and is the one spoken aloud + shown live.
- **Target another** with **"in `<name>`, …"** — it runs in the background and **pings you when
  done**; switch to its window anytime to see the work.
- **Clone-to-workspace** — **"clone `<url>` [branch `<b>`]"** clones a repo into a fresh workspace
  (`~/otto-workspaces/<repo>`) and drops you into it.
- Open/focus/close from the dashboard (Settings → Projects, ⇄ Parallel) or the menu bar.

## Chats within a project
- Each project keeps multiple Claude **sessions**; **switch**, start a **new chat**, or **rename**
  one — by voice or from the dashboard 🗂 drawer / menu bar.
- **Resume in place** — working in a project continues its *existing* session, so the conversation
  is right there when you reopen it in Claude Code.

## Permissions (per action)
- **`voice`** (default) — Otto asks before consequential actions (writes, `git push`, installs).
  A background workspace can't take a spoken yes, so it defers and pings you.
- **`auto`** — approves consequential actions unattended (good for parallel coding in trusted repos).
- **`deny`** — read-only.
- Read-only lookups (reading files, `git status`, `find`, searches) never prompt.

## Two-way conversation
- Otto can **ask you a question** mid-task (`ask_user`) and wait for your answer, then act on it —
  a real back-and-forth, by voice or text.

## Setup & health check
- **First-run permissions** — on launch Otto asks macOS for **microphone** access up front (the
  system prompt), and only starts listening once it's granted — no more silent "can't hear you".
- **"Otto ready" panel** — a dashboard checklist that validates everything on every start/restart:
  Microphone, the **Claude CLI**, a real **Claude test turn** (proves it's installed, logged in and
  can complete a turn), Voice output, and Accessibility. Each failing item offers **Open Settings**;
  a **Recheck** button re-runs the checks (and brings the ears up the moment the mic is granted, no
  restart). A ✓ Ready / ⚠ Setup pill in the HUD shows status at a glance.

## Menu-bar app
- Lives in the macOS menu bar (no Dock icon): status, **Talk**, **Open Dashboard**, **Project /
  Workspaces / Chat** switchers, mute mic/voice, Restart, Quit. Auto-starts at login (launchd).

## Install & distribution
- **Single-file installer** — `Orrin.dmg`: open it, drag **Orrin** into **Applications**.
- Self-contained `.app` (bundles Python, Whisper via ctranslate2, ONNX VAD, and the Claude CLI).
  First run downloads a small voice model.
- **Built in CI** (GitHub Actions, macOS/arm64) or locally via `packaging/pyinstaller/build.sh`.
- Unsigned today (right-click → Open once); signing/notarization is wired for when an Apple
  Developer account is added.

See [CHANGELOG.md](../CHANGELOG.md) for what landed in each version, and
[docs/TEAM_SETUP.md](TEAM_SETUP.md) for install/run instructions.
