# Otto — a voice/UI wiring layer for your own Claude

Otto is **not** another agent. It's the *wiring* — ears, a mouth, a screen, and an
always-on loop — that plugs into the **Claude already installed on your Mac** and uses
*Claude's own* brain, MCP servers, subagents, skills, `CLAUDE.md`, and permissions. You
configure your agents the normal Claude way; Otto only wires how voice/UI flows.

It works two ways at once:

```
  voice / ⌘⇧J ─▶ Otto shell ─▶ ClaudeBrain (Agent SDK)
                    │             ├─ loads YOUR ~/.claude config (MCP, subagents,
                    │             │  skills, CLAUDE.md, permissions)
                    ▼             └─ continue_conversation ⇒ resumes the project's chat
             speak / show ◀ voice+screen tools

  The same shell serves /mcp →  claude mcp add otto  → ANY Claude session gains
  speak / ask / show / notify, routed to the running shell.
```

Whatever you set up in Claude (a Gmail MCP, a custom subagent, a project `CLAUDE.md`) is
immediately usable by voice — no Otto-specific config.

## Quick start

```bash
bash scripts/setup.sh
source .venv/bin/activate
python run.py
```

Open http://127.0.0.1:8000. Click **🎤 Talk** and speak, type a message, or use the wake
word (see below). Type `/demo` for a model-less dashboard self-test.

## Talking to Otto

Three ways in, most-to-least reliable:

1. **🎤 Talk button** *(recommended)* — on the dashboard, in the menu bar (**🎤 Talk to
   Otto**), or the **⌘⇧J** hotkey. Click, then say your command — **no wake word**. This
   uses the proven VAD→Whisper path and never depends on the model hearing a name, so it's
   dependable. The dashboard/menu buttons need no macOS permission; the hotkey needs
   Accessibility.
2. **Wake word "hey otto"** *(convenient, less reliable)* — hands-free, but it rides on
   open-vocabulary speech-to-text, which frequently mishears/drops the name "Otto" (→
   "auto", "your"). Fine when it lands; use Talk when it doesn't. See *Wake word* below.
3. **Type** in the dashboard — always works.

Otto shows when it's hearing you: the menu-bar glyph goes 🧠→**👂** and the dashboard orb
pulses.

### Wake word backends

`voice.stt.wakeword_backend`:
- **`phrase`** (default) — software wake word, any phrase, no account/models. Convenient
  but limited by STT dropping the trigger word (above).
- **`oww`** — [openWakeWord](https://github.com/dscripka/openWakeWord): built-in phrases
  ("hey jarvis", …) work out of the box; a robust custom **"hey otto"** needs a free
  trained model (personal Google account, ~20 min). This is the right hands-free upgrade.
- **`pvporcupine`** — [Porcupine](https://picovoice.ai/platform/porcupine/): instant custom
  keywords, but signup needs a work email (paid for production). Set `wake_words` to a
  `.ppn` path + `PICOVOICE_ACCESS_KEY`.

## Run as an always-on Mac app (menu bar + login item)

```bash
bash scripts/install_service.sh
```

Installs a launchd LaunchAgent (starts at login, restarts on crash) and puts a 🧠 in your
menu bar with **Open Dashboard**, **Settings…**, **Project ▸** (switch project/context),
**Mute microphone/voice**, live status, **Restart**, **Quit**. It also runs
`claude mcp add --transport http otto …` so any Claude session gains Otto's voice/screen.

Remove it with `bash scripts/uninstall_service.sh`. Run the menu bar app once without
installing: `python -m otto.menubar`.

## Projects & context routing

A **project = a working directory**, and Claude stores conversations per directory. Pick a
project (menu bar **Project ▸** or the Settings page) and Otto **continues that project's
most recent Claude conversation** — same context, `CLAUDE.md`, MCP, and subagents. Recent
projects are auto-discovered from `~/.claude/projects`.

## Settings & storage

Config and data live in **`~/Library/Application Support/Otto/`** (override with
`OTTO_HOME`). Edit everything from the menu bar → **Settings…** (a page in the dashboard):
current project, brain (model, permission mode), voice (wake word, TTS, hotkey), and the
MCP-registration toggle. `config.yaml` in this repo is the packaged template.

## Requirements

- macOS with [Homebrew](https://brew.sh); `setup.sh` installs `portaudio` + `ffmpeg`.
- Python 3.11+.
- **Brain auth:** be logged into the `claude` CLI (the SDK reuses it) or set
  `ANTHROPIC_API_KEY` in `.env`.
- **Permissions:** grant **Microphone** (voice) and **Accessibility + Input Monitoring**
  (the ⌘⇧J hotkey) in System Settings, then use the menu's **Restart**.

## How it talks back

- **Streamed replies**, spoken and interruptible (barge-in).
- **Asks you questions** (`ask_user`) and waits for your spoken/typed answer; also used to
  confirm consequential actions when `brain.permission: voice`.
- **Proactive** via `otto/scheduler.py` (reminders, "build's done").

## Configuration (wiring only)

Edit in the Settings UI or `~/Library/Application Support/Otto/config.yaml` (or `OTTO_*`
env vars). Agent behavior comes from your Claude config, not here.

| Setting | Default | Notes |
|---|---|---|
| `brain.model` | `""` | blank = Claude's default; e.g. `opus` |
| `brain.permission` | `voice` | how Claude's tool prompts are handled: `voice`/`auto`/`deny` |
| `brain.project_dir` | `~` | current project (the brain's cwd) |
| `brain.use_claude_config` | `true` | load your `~/.claude` MCP/subagents/skills/CLAUDE.md |
| `brain.continue_conversation` | `true` | resume the project's latest session |
| `brain.register_mcp` | `true` | serve `/mcp` for `claude mcp add otto` |
| `voice.stt.wakeword_backend` | `phrase` | `phrase` / `oww` / `pvporcupine` (see *Talking to Otto*) |
| `voice.stt.wake_words` | `hey otto` | phrase to listen for (or a `.ppn` path for Porcupine) |
| `voice.tts.engine` | `system` | `elevenlabs` (needs `ELEVENLABS_API_KEY`) |

> The most reliable voice input is the **🎤 Talk** button / ⌘⇧J — no wake word needed.

## Layout

```
run.py                     terminal entrypoint
config.yaml                packaged config template
otto/
  orchestrator.py          realtime loop + project switching + tool callbacks
  projects.py              recent-project discovery (~/.claude/projects)
  scheduler.py             proactive/unprompted speech
  config.py                storage + wiring config
  mcpserver.py             Otto-as-MCP for Claude (speak/ask/show/notify/status)
  brain/  base.py          Brain protocol (pluggable)
          agent.py         ClaudeBrain — runs YOUR Claude (setting_sources + preset)
          tools.py         in-process voice/screen tools ("voice" MCP)
  voice/  stt.py tts.py hotkey.py     ears / mouth / push-to-talk
  server/ app.py           FastAPI: dashboard, WebSocket, /api, mounts /mcp
          web/             dashboard + settings UI (no build step)
```

> Not an agent framework: there is no Otto persona, subagents, or connector config —
> those come from your Claude. Otto is the wiring around it.

## How Otto compares (community landscape, 2026)

Two architectures dominate realtime voice AI:

- **Cascaded** (STT → LLM → TTS) — transparent, swappable, gives a text transcript (needed
  for tools/logging), and a well-built pipeline streams first audio in ~100–250 ms. This is
  what Otto is. Popular frameworks: **[Pipecat](https://github.com/pipecat-ai/pipecat)**
  (Python pipeline that nails barge-in, partial-transcript gating, buffering) and
  **[LiveKit Agents](https://github.com/livekit/agents)** (WebRTC-native).
- **Native speech-to-speech** — one end-to-end audio model (OpenAI Realtime, Gemini Live,
  Amazon Nova Sonic). Lower latency and richer prosody, but less control, no transcript, and
  it *is* the brain — you can't point it at your own Claude.

**Otto is deliberately cascaded**, because the whole idea is to wire voice to *your* Claude
(a text/tool brain via the Agent SDK) — a native S2S model would replace Claude, not use it.
We hand-rolled the loop (RealtimeSTT + RealtimeTTS + barge-in) rather than adopt Pipecat to
stay dependency-light and Claude-Code-native; Pipecat is a reasonable future refactor of the
`voice/` + `orchestrator.py` layer.

**On wake words**, the community splits the same way we landed:
[openWakeWord](https://github.com/dscripka/openWakeWord) (free, on-device; custom phrases
need a short training step) vs [Porcupine](https://picovoice.ai/platform/porcupine/)
(instant custom keywords, but work-email/paid). Because a *software* wake word rides on
open-vocab STT and drops uncommon names, most local assistants use a trained acoustic wake
model **or** push-to-talk — which is exactly why Otto's dependable path is the **🎤 Talk**
button, with a trained openWakeWord "hey otto" as the hands-free upgrade.
