# Orrin — 60-second voice demo script

A tight recording flow that shows Orrin is *your* Claude Code, by voice + a screen. Record with
**⌘⇧5** (screen record, with the mic). Product name is **Orrin**; the wake word is **"Otto"**.

## Before you hit record (30s of prep)
- **Headphones/earphones on** — this is what lets you talk over Orrin without it cutting out
  (route-aware barge-in), and it kills speaker→mic echo for a clean capture.
- Make sure **your voice is enrolled** (menu bar → *Enroll a voice*, or the `/enroll` page) so the
  demo shows Orrin responding to *you* and ignoring background noise.
- Launch Orrin; open the dashboard (menu bar → *Open Dashboard*). Wait for **✓ Ready**.
- Frame the shot so the **notch dock** (top-center), the dashboard (sidebar + weather hero + Chat
  drawer), and the menu-bar icon are all visible.
- Speak at a normal pace; ~1s pause after each request so Orrin picks up the turn.

## The flow (~60s)

| # | You say (out loud) | What the viewer sees |
|---|--------------------|----------------------|
| 1 | *"Otto — what can you do?"* | The **notch dock** drops down and expands (listening → thinking → speaking) with a live transcript of your words; the reply streams in the Chat drawer. It tucks back into a sliver when idle. |
| 2 | *"Otto, what's the weather and my GitHub activity?"* | Point to the **weather hero** + **GitHub Activity** cards — real, live data on the dashboard. |
| 3 | *"Otto, check the ClickHouse project's git status, and meanwhile open the mcp-server folder."* | Shows **parallel "meanwhile" dispatch** — the background task runs in its own lane while the foreground one proceeds. |
| 4 | *"Otto, create a file called demo.txt that says hello, and chart my last five commits by day."* | Real tool calls stream in the chat; a **chart renders in Visuals** — your actual Claude doing the work. |
| 5 | *(with a TV or someone talking nearby)* stay silent, then *"Otto, what's next?"* | Shows **speaker verification** — the room/TV is ignored, only your voice gets through. |
| 6 | *"Otto, stop listening."* then *"Otto"* to wake it | Hands-free **sleep/wake**; the dock calms, then re-arms on the wake word. |
| 7 | Toggle **🌙/☀️** and click through **Home / Conversations / Visuals / Settings** | Light/dark theme + the whole UI in ~5s. |

## One-liner to open the video with
> "This is Orrin — it wraps the Claude Code I already run and gives it a voice and a screen.
> Everything you'll see is my real Claude doing the work; I'm just talking to it."

## Optional closer (the background teammate)
> "And when I'm away, I can turn on its autonomous mode — it researches my projects and opens
> ready-for-review pull requests on its own. I just merge."
(Only show this if `voice.autonomous_work` is on and you want to feature it.)

## Tips
- On-device speech is fast (MLX Whisper on Apple silicon) and it starts replying at the first
  sentence — keep the pace brisk; the detail lands on screen.
- With earphones you can interrupt mid-reply; on the built-in speakers it stays half-duplex (mutes
  the mic while speaking), so wait for it to finish before the next line.
- Mute the mic between takes (menu bar or the dock) so ambient noise doesn't trigger a turn.
- Good closer: "…and it's all my own Claude — my MCP servers, subagents, skills, and permissions,
  just spoken."

## Saving the recording
Export the clip to `assets/orrin-demo.mp4` (replace the old one). For the README's inline preview,
also make a GIF — e.g. `ffmpeg -i assets/orrin-demo.mp4 -vf "fps=12,scale=900:-1:flags=lanczos" assets/orrin-demo.gif`
(ask and I'll run the conversion + wire it into the README).
