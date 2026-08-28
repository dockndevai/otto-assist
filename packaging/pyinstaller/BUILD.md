# Building the standalone Orrin.app

Turns Orrin into a self-contained macOS app (no "build a venv on first run"), signed and
notarized so it opens cleanly on any Mac. This is the consumer-grade distribution path;
the Homebrew tap stays the developer path.

## Quick build (unsigned — local testing only)
```bash
bash packaging/pyinstaller/build.sh
open dist/Orrin.app
```
Unsigned apps trigger a Gatekeeper warning ("unidentified developer") — fine for you to test,
not for distribution.

## Signed + notarized DMG (for release) ⚖️ needs an Apple Developer account
One-time setup:
1. Enroll in the **Apple Developer Program** ($99/yr).
2. Create a **Developer ID Application** certificate (Xcode → Settings → Accounts, or the
   developer portal) and install it in your login keychain.
3. Store a notarization credential:
   ```bash
   xcrun notarytool store-credentials orrin-notary \
     --apple-id you@apple.id --team-id TEAMID --password <app-specific-password>
   ```
Then:
```bash
SIGN_ID="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE=orrin-notary \
  bash packaging/pyinstaller/build.sh
```
Outputs `dist/Orrin.app` and a signed, notarized, stapled `dist/Orrin.dmg`.

## What's in the bundle
- Entry point: `orrin_main.py` → `otto.menubar:main` (the menu-bar app).
- `orrin.spec` collects the awkward deps (faster-whisper/ctranslate2, onnxruntime, silero,
  openwakeword, rumps, fastmcp, claude-agent-sdk) and our web/asset data files.
- Info.plist sets `LSUIElement` (menu-bar agent, no Dock icon) and the mic-usage string.
- `entitlements.plist` grants the mic + the JIT/library-validation exceptions Python needs
  under the hardened runtime.

## Runtime dependencies NOT bundled
- **The Claude CLI.** Orrin drives the user's own `claude`. The app adds common locations
  (`/opt/homebrew/bin`, `/usr/local/bin`) to PATH, but document that users need Claude Code
  installed and signed in. (This is the "bring your own Claude" model.)
- **ffmpeg**, if any path needs it — verify at runtime or bundle the binary.
- **Whisper model weights** — download to the user cache on first listen.

## Verified (2026-08-28)
- `bash build.sh` produces `dist/Orrin.app` + `dist/Orrin.dmg`, and the frozen app **runs** —
  the dashboard serves and, with voice enabled, the recorder initialises with **no torch**
  and no import errors.
- **Torch removed:** the voice stack is torch-free — Whisper via ctranslate2, and Silero VAD
  via onnxruntime (`silero_use_onnx=True` in `otto/voice/stt.py`). `orrin.spec` excludes
  torch/torchaudio/torchvision/tensorboard. **Size dropped 1.2 GB → ~815 MB app / ~335 MB DMG.**
- `build.sh` aliases the `webrtcvad-wheels` metadata so PyInstaller's `webrtcvad` hook passes.
- `orrin_main.py` prepends Homebrew/user bins to PATH so the frozen app finds the user's
  `claude` (LaunchServices otherwise gives a bare PATH).

### Further slimming (not yet done — each needs validation)
- **`claude_agent_sdk/_bundled` ≈ 310 MB** — the SDK vendors a full Claude CLI. Under
  BYO-Claude we drive the *user's* `claude`, so this may be droppable — but confirm the SDK
  still spawns the system CLI (a real query) before excluding those data files.
- **llvmlite/numba ≈ 123 MB** — pulled in by openwakeword, which the default `phrase` wake
  backend doesn't use. Exclude only after confirming RealtimeSTT imports without openwakeword.
- Together these could roughly halve the bundle again (~350 MB app).

## Known gotchas (expect iteration on the first build)
- `ctranslate2` / `onnxruntime` dylibs: if the app fails to import them, add them explicitly to
  `binaries` in `orrin.spec`.
- `faster-whisper` avoids torch (uses ctranslate2) — keep it that way to stay small. If a dep
  drags torch in, exclude it or the bundle balloons to multiple GB.
- `rumps`/AppKit: must run from the `.app` (not a plain binary) for the menu bar to appear.
- First launch: macOS will prompt for **Microphone** permission (the Info.plist string).

## Icon
Drop `packaging/pyinstaller/orrin.icns` to brand the app (generate from a 1024×1024 PNG with
`iconutil`). Without it, PyInstaller uses a default icon.
