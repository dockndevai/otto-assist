# Installing Otto

Otto is a voice + screen for the Claude you already run on your Mac. This walks you through
a full install from scratch. Takes ~10 minutes.

> **Platform:** macOS (Apple Silicon or Intel). Otto uses macOS `say`, the menu bar, and
> launchd. It won't run as-is on Windows/Linux.

---

## 1. Prerequisites

- **macOS** with **[Homebrew](https://brew.sh)** — install it if you don't have it:
  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- **Python 3.11+** (`python3 --version`).
- **Claude** — either:
  - the **Claude Code CLI** logged in (`claude` — run it once and sign in), **or**
  - an **`ANTHROPIC_API_KEY`** (you'll add it in step 4).

## 2. Get the code

```bash
git clone https://github.com/dockndevai/otto-assist.git
cd otto-assist
```

## 3. Run setup (installs everything)

```bash
bash scripts/setup.sh
```

This installs `portaudio` + `ffmpeg` (Homebrew), creates a `.venv`, installs Otto with the
voice + menu-bar dependencies, pre-downloads the Whisper model, and creates a `.env`.

## 4. Connect Claude

Otto uses **your** Claude. Either sign into the CLI:

```bash
claude    # then complete the sign-in, and quit with Ctrl+C
```

…**or** add a key to `.env`:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
```

Whatever MCP servers / subagents / skills / `CLAUDE.md` you have in Claude, Otto inherits.

## 5. Grant macOS permissions

System Settings → **Privacy & Security**:
- **Microphone** → enable for your terminal (and for Python/Otto when prompted). *Required
  for voice.*
- **Accessibility** and **Input Monitoring** → enable your terminal. *Only needed for the
  ⌘⇧J push-to-talk hotkey; skip if you'll use the on-screen Talk button.*

## 6. Run it

**Option A — try it in a terminal:**
```bash
source .venv/bin/activate
python run.py
```
Opens http://127.0.0.1:8000.

**Option B — install as an always-on menu-bar app (recommended):**
```bash
bash scripts/install_service.sh
```
Puts a **🧠** in your menu bar, starts at login, and registers Otto's voice/screen MCP into
Claude Code. Remove any time with `bash scripts/uninstall_service.sh`.

> First launch loads the Whisper model — the dashboard may take ~30–60s the first time.

## 7. Use it

Open the dashboard (menu bar 🧠 → **Open Dashboard**, or the URL above):

- **Type** a message, or
- Click **🎤 Talk**, then speak your request (no wake word needed — the reliable path), or
- Set up a **custom wake word** (below) to go hands-free.

Try: *"check my recent emails"*, *"what's in my Documents projects?"*, *"show me a chart of…"*.
Reads happen freely; consequential actions (sending, deleting) ask first.

## 8. (Optional) Your own wake word — "hey otto"

Make a custom wake word by recording it a few times — no account, no training:

```bash
source .venv/bin/activate
python -m otto.enroll_wake_word "hey otto"
```

Say your phrase 4 times when prompted, then **Restart Otto** (menu bar → Restart). Now say
*"hey otto"* — it greets you and waits for your request. Tune sensitivity in Settings
(`voice.stt.eff_threshold`) if needed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Replies say *"OAuth session expired"* | Re-run `claude` and sign in, or set `ANTHROPIC_API_KEY` in `.env`. |
| No sound | Check Mac volume/output. Otto speaks via macOS `say`; test with `say hello`. |
| Voice input off / mic not working | Grant **Microphone** to your terminal (step 5), then menu bar → **Restart**. |
| Push-to-talk (⌘⇧J) does nothing | Grant **Accessibility + Input Monitoring** (step 5). Or just use the Talk button. |
| Wake word misses | Software `phrase` mode is unreliable; enroll a custom wake word (step 8). |
| Menu bar 🧠 missing | Run `bash scripts/install_service.sh`; check `logs/` for errors. |

Config lives at `~/Library/Application Support/Otto/config.yaml` and is editable from the
menu bar → **Settings**.
