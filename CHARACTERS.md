# Orrin characters

Orrin can show a small **floating on-screen character** that reacts to what it's doing —
idle, listening, thinking, speaking, or signed-out. The default is **Orrin the red panda**, but the
character is a swappable *pack*, so you can keep the default, pick another, or make your own.

## Turning it on

- **Menu bar → "Show Orrin (floating)"** toggles the character panel.
- To show it automatically at launch, set in your config (`~/Library/Application Support/Otto/config.yaml`):

  ```yaml
  voice:
    floating_character: true
    character: orrin        # which pack to use
  ```

The panel is borderless, transparent, always-on-top, and floats over full-screen apps without
stealing focus. Drag Orrin to reposition. It reacts live to Orrin's voice state.

## Using a different character

Set `voice.character` to any pack name. Orrin looks for the pack in **two places** (yours wins):

1. `~/Library/Application Support/Otto/characters/<name>/`  ← your own packs
2. the bundled packs shipped with Orrin (e.g. `orrin`)

So to override the default without touching the app, create
`~/Library/Application Support/Otto/characters/orrin/` with your own files, or use a new name and
point `voice.character` at it.

## Making a 2D character pack

A pack is a folder with a `character.json` manifest and PNG frames (transparent background works
best). Minimum:

```
mycharacter/
  character.json
  idle.png
  talking.png
  blink.png
  listening.png
  thinking.png
```

`character.json`:

```json
{
  "name": "My Character",
  "type": "sprite2d",
  "states": {
    "idle": "idle.png",
    "listening": "listening.png",
    "thinking": "thinking.png",
    "working": "thinking.png",
    "speaking": "idle.png",
    "signed_out": "thinking.png"
  },
  "blink": "blink.png",
  "mouth": { "closed": "idle.png", "open": "talking.png" }
}
```

- **`states`** — which frame to show for each voice state. `idle` is required; the rest fall back to it.
- **`blink`** — shown briefly on a timer while idle/listening (optional).
- **`mouth`** — while *speaking*, Orrin alternates `closed`/`open` for a simple lip-flap (optional).

Tips: frames should share the same size and framing so they swap cleanly. Transparent PNGs (matte
the background out) make the character float; large flat-background PNGs also work but show a box.

## Coming later

The same pack format is designed to grow a `type` field beyond `sprite2d`:

- **`vrm`** — a rigged 3D avatar (`model.vrm`), rendered with three-vrm, with real lip-sync and
  look-at. Bring your own from VRoid Studio or ReadyPlayerMe.
- **`live2d`** — a rigged Live2D model (you supply the model, due to Live2D's license).

Until those land, `sprite2d` (2D frames) is the supported pack type.
