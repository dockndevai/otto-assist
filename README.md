# Otto — realtime personal assistant

A Otto-style assistant that runs on your Mac: it listens by voice, talks back, spawns
subagents to get real work done, and shows things on a web dashboard. Claude is the
brain (via the Claude Agent SDK); a local realtime voice layer and a browser UI wrap it.

```
  voice ─▶ Whisper (RealtimeSTT) ─▶ Orchestrator ─▶ Claude (Agent SDK) ─▶ subagents
                                         │                  │
                                         ▼                  ▼
                                   RealtimeTTS         web dashboard
                                    (speaks)         (transcript + canvas)
```

## Quick start

```bash
bash scripts/setup.sh
source .venv/bin/activate
python run.py
```

Then open http://127.0.0.1:8000. Type in the box, or say **“hey jarvis …”**, or hold the
push-to-talk chord (default ⌘⇧J).

> **Dashboard self-test:** type `/demo` in the box — Otto renders a sample chart to the
> canvas without calling the model, confirming the UI ↔ server pipeline is wired up.

## Run as an always-on Mac app (menu bar + login item)

Instead of a terminal, run Otto as a background **menu bar app** (🧠) that starts
automatically at login:

```bash
bash scripts/install_service.sh
```

This installs a launchd LaunchAgent, starts Otto, and puts a 🧠 in your menu bar with:

- **Open Dashboard** — the web UI
- **Mute microphone** / **Mute voice** — toggle ears/mouth without quitting
- live status (Ready / Listening / Thinking / Speaking)
- **Restart**, **Quit**

It relaunches on crash and comes back every time you log in. To remove it:

```bash
bash scripts/uninstall_service.sh
```

To run the menu bar app once, without installing the login item:

```bash
source .venv/bin/activate && python -m otto.menubar
```

Logs live in `logs/otto.err.log`. On first launch, grant **Microphone** (and, for the
push-to-talk hotkey, **Input Monitoring**) in System Settings, then use **Restart**.

## Requirements

- macOS with [Homebrew](https://brew.sh); `setup.sh` installs `portaudio` + `ffmpeg`.
- Python 3.11+.
- **Brain auth:** either be logged into the `claude` CLI (already installed) — the SDK
  reuses it — or put `ANTHROPIC_API_KEY` in `.env`.
- **Microphone permission:** System Settings → Privacy & Security → Microphone → enable
  your terminal app. (Needed for voice input and the push-to-talk hotkey/Input Monitoring.)

## How it talks back to you

- **Streamed replies** — spoken as they're generated, and interruptible (start talking and
  it stops — barge-in).
- **Asks you questions** — the `ask_user` tool speaks a question and waits for your spoken
  or typed answer before continuing. Also used to confirm consequential actions.
- **Proactive** — `otto/scheduler.py` can speak unprompted (reminders, "build's done").

## Connecting other agents (orchestration)

Otto can route work to other agents/tools on your machine — the foundation of the
multi-agent orchestrator. Add them under `connectors` in `config.yaml`; no code changes.

- **MCP servers** (`connectors.mcp`) — an external MCP server (stdio/http/sse). Its tools
  become Otto's tools directly. Good for filesystem/github/custom MCP servers, or Claude
  Code exposed as MCP.
- **CLI agents** (`connectors.cli`) — any command-line agent (e.g. `claude -p`, `aider`,
  a script). Otto hands it a task via the `delegate` tool and reads back its output.

```yaml
connectors:
  mcp:
    filesystem:
      transport: stdio
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
  cli:
    claude:
      command: ["claude", "-p"]        # the task is appended as the final argument
      description: "A fresh headless Claude session for a self-contained job."
    aider:
      command: ["aider", "--message", "{task}"]   # {task} is substituted in place
      description: "Autonomous coding in a repo."
      cwd: "/Users/you/code/myproject"
```

Then just ask: *"delegate this research to claude"* or *"use the filesystem tools to …"*.

> Note on Cursor: Cursor has no official local API to drive its AI, so it isn't a clean
> connector. Anything with a CLI or MCP server works; Cursor only shallowly (open files).

## Configuration

Edit `config.yaml` (or override with `OTTO_*` env vars). Highlights:

| Setting | Default | Notes |
|---|---|---|
| `brain.model` | `sonnet` | `opus` for harder tasks |
| `brain.permission` | `ask` | `auto` to skip confirmations for file/shell actions |
| `voice.input_enabled` | `true` | set `false` for a keyboard-only assistant |
| `voice.output_enabled` | `true` | set `false` to mute speech |
| `voice.stt.model` | `small.en` | `base.en` if CPU-bound; `medium`/`large-v3` for accuracy |
| `voice.stt.wake_words` | `hey jarvis` | empty string disables the wake word (VAD-only) |
| `voice.tts.engine` | `system` | `elevenlabs` (needs `ELEVENLABS_API_KEY`) for a premium voice |
| `voice.hotkey.push_to_talk` | `<cmd>+<shift>+j` | pynput chord syntax |

## Layout

```
run.py                     entrypoint
config.yaml / .env         configuration
otto/
  orchestrator.py          realtime loop; implements the tools' callbacks
  scheduler.py             proactive/unprompted speech
  config.py                settings loader
  brain/  agent.py         ClaudeSDKClient wrapper + persona + permissions
          tools.py         say / ask_user / render_canvas / set_status / notify
          subagents.py     researcher + coder subagents
  voice/  stt.py           RealtimeSTT ears (wake word + VAD)
          tts.py           RealtimeTTS mouth (+ barge-in)
          hotkey.py        push-to-talk
  server/ app.py           FastAPI + WebSocket
          events.py        event bus
          web/             dashboard (no build step)
```

## Notes

- The text path works even without the voice extras installed — set both `voice.*_enabled`
  to `false`, or just ignore the mic and type.
- With a wake word configured, push-to-talk forces an immediate capture; if the two
  interact oddly on your machine, disable one in `config.yaml`.
