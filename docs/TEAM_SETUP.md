# Orrin — team setup (macOS, local testing)

Orrin (aka Otto) is a JARVIS-style voice + screen layer over **your own Claude Code**. You
talk (or type) to it, it does real work in your projects, shows the work in a dashboard, and
speaks a short reply. It can run **several projects in parallel**, each in its own chat window.

There are two ways to run it. Pick one:

- **A. Installed app (`Orrin.dmg`)** — easiest; for anyone who just wants to test.
- **B. From source** — for developers who want to change code.

---

## Prerequisites (both paths)

1. **macOS 12 or newer** (Apple Silicon or Intel).
2. **Claude Code installed and signed in** — Orrin drives *your* Claude ("bring your own
   Claude"). If you don't already use it, install it (see the official docs at
   https://docs.claude.com/claude-code for the current method), e.g.:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude            # run once, then /login (uses your Claude subscription or API key)
   ```
   Verify: `claude --version` prints a version, and `claude` opens without asking you to log in.
3. **Microphone** — you'll grant mic access on first run (for voice). Text-only works without it.

> Orrin does **not** need a separate `ANTHROPIC_API_KEY` — it uses your Claude Code login.
> (ElevenLabs is optional, only if you switch the TTS engine off the built-in system voice.)

---

## A. Installed app (Orrin.dmg) — recommended for testers

1. Get **`Orrin.dmg`** (ask the maintainer to share it — it's ~337 MB, not in git). Or build it
   yourself (see path B → "Build the DMG").
2. Double-click `Orrin.dmg`, then **drag `Orrin` into your `Applications` folder**.
3. First launch is unsigned, so Gatekeeper will warn: **right-click `Orrin` → Open → Open**
   (only needed once).
4. Allow **Microphone** access if prompted.
5. The **🤖 Orrin** icon appears in the menu bar → click it → **Open Dashboard** (opens a native
   in-app window — no browser).

That's it. See **"Using it"** below.

---

## B. From source (developers)

```bash
git clone git@github.com:dockndevai/otto-assist.git
cd otto-assist
git checkout feature/parallel-workspaces      # until it's merged to main
bash scripts/setup.sh                          # installs portaudio+ffmpeg, a .venv, deps, Whisper model
```

Run it one of two ways:

```bash
source .venv/bin/activate

# menu-bar app (the full experience: 🤖 icon → Open Dashboard):
python -m otto.menubar

# …or headless (dashboard only, open http://127.0.0.1:8000 yourself):
python run.py
```

**Grant Microphone access to your terminal** (the app inherits it): System Settings → Privacy &
Security → Microphone → enable **Terminal** (or iTerm). Restart the app after granting.

### Build the DMG (to share the installed app)
```bash
bash packaging/pyinstaller/build.sh            # -> dist/Orrin.app and dist/Orrin.dmg (unsigned)
```
Share `dist/Orrin.dmg` with testers (AirDrop / Drive / etc.). Unsigned, so testers use the
right-click → Open step above. (Signing/notarization for warning-free launch needs an Apple
Developer account; the script supports it via `SIGN_ID` + `NOTARY_PROFILE`.)

---

## Using it

- **Talk:** say the wake word **"Otto"**, then your request (e.g. *"Otto, what's the status of
  this project?"*). Or hold-to-talk with **⌘⇧J**, or click the 🎤 in the dashboard.
  - The global ⌘⇧J hotkey needs **System Settings → Privacy & Security → Accessibility** →
    enable Orrin/your terminal. (The menu "🎤 Talk to Otto" and the dashboard mic don't need it.)
- **Type:** use the composer box in the dashboard — same thing, no mic needed.
- **Pick the project (workspace):** menu bar → **Project**, or dashboard **⚙ Settings → Projects**.
- **Run projects in parallel:** in Settings → Projects, click **⇄ Parallel** to open another
  project alongside; or say **"in <project>, <command>"** to send work to another window while
  you keep going. The strip under the header shows every workspace; click one to switch to it.
- **Chats within a project:** the 🗂 drawer (or menu → Chat) — start a new chat, switch, rename.
- **Permissions** (Settings → *Permission prompts*):
  - `voice` (default) — Orrin asks before consequential actions (writes, `git push`, installs).
    A background workspace can't take a spoken yes, so it defers and pings you to focus it.
  - `auto` — approves consequential actions unattended (good for parallel coding; use in repos
    you trust).
  - `deny` — read-only.
  Read-only lookups (reading files, `git status`, `find`) never prompt.

---

## Troubleshooting

- **"Otto isn't hearing me."** Grant Microphone access (to Terminal for source runs, or to Orrin
  for the app) and restart. First run also downloads a small voice model — give it a minute.
- **App won't open ("unidentified developer").** Right-click → Open → Open (unsigned build).
- **"Address already in use" / dashboard blank.** Something else is on port 8000. Quit any other
  Orrin instance (menu → Quit), or change `server.port` in the config.
- **Config file:** `~/Library/Application Support/Otto/config.yaml` (voice, wake word, model,
  permission, project). Edit + restart from the menu.
- **It says it can't find `claude`.** Make sure Claude Code is installed and on PATH, and that
  `claude` runs without asking you to log in.
