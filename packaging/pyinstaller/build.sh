#!/usr/bin/env bash
# Build (and optionally sign + notarize) a standalone Orrin.app, then a DMG.
#
#   bash packaging/pyinstaller/build.sh              # unsigned build (for local testing)
#   SIGN_ID="Developer ID Application: NAME (TEAMID)" \
#   NOTARY_PROFILE=orrin-notary \
#     bash packaging/pyinstaller/build.sh            # signed + notarized DMG (needs Apple acct)
#
# Signing/notarization prerequisites (one-time, needs your Apple Developer account):
#   - Enroll in the Apple Developer Program, create a "Developer ID Application" certificate.
#   - Store a notary credential:  xcrun notarytool store-credentials orrin-notary \
#       --apple-id you@apple.id --team-id TEAMID --password <app-specific-password>
set -euo pipefail
cd "$(cd "$(dirname "$0")/../.." && pwd)"

APP="dist/Orrin.app"
DMG="dist/Orrin.dmg"

echo "==> Ensuring build tooling (pyinstaller, ruff, pytest) in the venv"
[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first (.venv missing)"; exit 1; }
# pytest is needed by the test gate below; CI's venv only has the runtime extras, so install it
# here rather than assume a dev environment (a missing pytest silently aborted the CI build).
.venv/bin/pip install -q --upgrade pyinstaller ruff pytest

# Static gate: catch the class of bug that syntax checks and imports miss — an undefined name
# used on a rarely-run path (e.g. `time.sleep` with no `import time`, which crashed the recorder
# watchdog silently at runtime). F821=undefined name, F822=undefined __all__ export,
# F811=redefinition, F401=unused import. A hit here FAILS the build before we ship it.
echo "==> Lint gate (undefined names / bad imports)"
if ! .venv/bin/ruff check otto/ run.py --select F821,F822,F811,F401,F406 --output-format concise; then
    echo "Build aborted: fix the lint errors above (they would crash at runtime)."
    exit 1
fi

# Fast test suite — a second safety net before packaging (~7s).
echo "==> Running tests"
if ! .venv/bin/python -m pytest tests/test_pipeline.py -q; then
    echo "Build aborted: tests failing."
    exit 1
fi

# Neural voice (Kokoro) must be importable in the build venv or the packaged app ships with only
# the robotic system voice (this silently regressed the v0.2.14 CI build). Warn loudly — don't
# fail, since a deliberate voice-less build is allowed.
if ! .venv/bin/python -c "import kokoro_onnx" 2>/dev/null; then
    echo "⚠️  WARNING: kokoro-onnx not importable — the app will fall back to the robotic system"
    echo "    voice. Install it:  .venv/bin/pip install -e \".[neural-voice]\""
fi

echo "==> Metadata fixups (so PyInstaller's stdhooks don't crash)"
# RealtimeSTT ships 'webrtcvad-wheels' (module: webrtcvad), but PyInstaller's webrtcvad hook
# looks for dist metadata named 'webrtcvad'. Alias it so copy_metadata('webrtcvad') succeeds.
.venv/bin/python - <<'PY'
import glob, shutil, pathlib, importlib.metadata as md
try:
    md.version("webrtcvad"); print("   webrtcvad metadata already present")
except md.PackageNotFoundError:
    import webrtcvad
    sp = pathlib.Path(webrtcvad.__file__).parent
    srcs = glob.glob(str(sp / "webrtcvad_wheels-*.dist-info"))
    if not srcs:
        print("   WARN: webrtcvad_wheels dist-info not found; build may fail"); raise SystemExit(0)
    src = pathlib.Path(srcs[0]); ver = src.name.split("-", 1)[1].rsplit(".dist-info", 1)[0]
    dst = src.parent / f"webrtcvad-{ver}.dist-info"
    if not dst.exists():
        shutil.copytree(src, dst)
        meta = dst / "METADATA"
        lines = ["Name: webrtcvad" if l.startswith("Name:") else l
                 for l in meta.read_text().splitlines()]
        meta.write_text("\n".join(lines))
        print("   aliased ->", dst.name)
PY

echo "==> Building native audio helper (orrin-audio, Bundle B)"
if command -v swift >/dev/null 2>&1; then
  ( cd native/orrin-audio && swift build -c release ) \
    && echo "    Built orrin-audio" \
    || echo "    WARNING: orrin-audio build failed — packaged app falls back to RealtimeSTT"
else
  echo "    swift not found — skipping native audio helper (RealtimeSTT fallback)"
fi

echo "==> Building Orrin.app"
rm -rf build dist
.venv/bin/pyinstaller packaging/pyinstaller/orrin.spec --noconfirm
[ -d "$APP" ] || { echo "Build failed: $APP not produced"; exit 1; }
echo "    Built $APP"

# MLX smoke check (#73): if mlx is installed in the build venv, its native libs MUST be in the
# bundle and COLOCATED (libmlx.dylib + mlx.metallib in the same dir), or the packaged app silently
# falls back to the slower faster-whisper STT. Warn loudly (don't fail — an mlx-less build is allowed).
if .venv/bin/python -c "import mlx.core" 2>/dev/null; then
    # PyInstaller makes several copies of these files, so check for colocation DIRECTLY: is there
    # any directory in the bundle holding BOTH libmlx.dylib and mlx.metallib? That's what MLX's
    # dladdr-based metallib lookup needs at runtime.
    COLO=""
    while IFS= read -r d; do
        [ -f "$d/mlx.metallib" ] && { COLO="$d"; break; }
    done < <(find "$APP" -name libmlx.dylib -exec dirname {} \; | sort -u)
    if [ -n "$COLO" ]; then
        echo "    MLX native libs present and colocated ($COLO)"
    else
        echo "⚠️  WARNING: no bundle dir holds BOTH libmlx.dylib and mlx.metallib —"
        echo "    MLX will fail to initialize its Metal backend and fall back to faster-whisper (#73)."
    fi
fi

# Signing strategy:
#   SIGN_ID set            -> real Developer ID (hardened runtime + timestamp, for notarization).
#   LOCAL_SIGN_ID present   -> stable SELF-SIGNED identity. The whole point: an ad-hoc signature's
#                             code hash changes every rebuild, so macOS treats each build as a new
#                             app and DROPS every TCC grant (mic / Accessibility / Screen Recording)
#                             — the "re-grant permissions on every reinstall" pain. A stable local
#                             cert keeps the app's designated requirement constant, so grants PERSIST
#                             across rebuilds. Create it once with scripts/setup_signing.sh.
#   neither                -> ad-hoc (PyInstaller default); grants drop each rebuild.
LOCAL_SIGN_ID="${LOCAL_SIGN_ID:-Orrin Local Signing}"
if [ -n "${SIGN_ID:-}" ]; then
  echo "==> Code-signing (hardened runtime, inside-out)"
  # Notarization requires EVERY Mach-O in the bundle to be signed with a secure timestamp and
  # the hardened runtime. Sign the nested binaries deepest-first (reverse path sort), detecting
  # Mach-O by content so unsuffixed binaries are covered too, then sign the app bundle itself
  # with the entitlements (mic + calendar + Python/hardened-runtime exceptions).
  while IFS= read -r -d '' f; do
    if file "$f" | grep -q "Mach-O"; then
      codesign --force --timestamp --options runtime --sign "$SIGN_ID" "$f" 2>/dev/null || true
    fi
  done < <(find "$APP" -type f -print0 | sort -rz)
  codesign --force --timestamp --options runtime \
    --entitlements packaging/pyinstaller/entitlements.plist \
    --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo "    Signed with: $SIGN_ID"
elif security find-certificate -c "$LOCAL_SIGN_ID" >/dev/null 2>&1; then
  echo "==> Code-signing with STABLE LOCAL identity ($LOCAL_SIGN_ID) — TCC grants persist across rebuilds"
  # No hardened runtime / timestamp: those are for notarization. A plain self-signed signature is
  # enough to give the bundle a constant designated requirement so TCC remembers its grants.
  while IFS= read -r -d '' f; do
    if file "$f" | grep -q "Mach-O"; then
      codesign --force --sign "$LOCAL_SIGN_ID" "$f" 2>/dev/null || true
    fi
  done < <(find "$APP" -type f -print0 | sort -rz)
  codesign --force --entitlements packaging/pyinstaller/entitlements.plist \
    --sign "$LOCAL_SIGN_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP" \
    || echo "    (self-signed verify warnings are expected — Gatekeeper still warns on first open)"
  echo "    Signed with local identity: $LOCAL_SIGN_ID"
else
  echo "==> No signing identity — using ad-hoc (PyInstaller default)."
  echo "    ⚠️  macOS will DROP mic/Accessibility/Screen-Recording grants on every rebuild."
  echo "    Fix once:  bash scripts/setup_signing.sh   (creates a stable local identity)"
fi

echo "==> Creating DMG"
# A proper installer window: the app beside an /Applications symlink, on a background
# image, with the window size and icon positions baked into the volume's .DS_Store.
# That layout can only be written by Finder while the image is mounted read-write, so
# build UDRW first, arrange it, then compress to UDZO.
VOL="Orrin"
RW="dist/.Orrin-rw.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
mkdir "$STAGE/.background"
# Pre-baked background (checked in). Regenerate with make_dmg_background.py if you change it.
cp packaging/pyinstaller/dmg-background.png "$STAGE/.background/bg.png"

rm -f "$RW" "$DMG"
# HFS+, not APFS: Finder's icon layout does not persist reliably on an APFS image.
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDRW -fs HFS+ "$RW"
rm -rf "$STAGE"
hdiutil detach "/Volumes/$VOL" 2>/dev/null || true
hdiutil attach -readwrite -noverify -noautoopen "$RW" >/dev/null

echo "==> Arranging the installer window (Finder)"
# Needs Automation permission for the terminal to control Finder. If it is DENIED the script
# gets a non-zero exit and skips; but if the TCC prompt is never answered (headless / CI /
# background build) the Apple event BLOCKS forever, hanging the build. Bound it with a hard
# 25s timeout (perl alarm) so it degrades to "skipped" — the DMG still ships with the
# /Applications symlink, just without the arranged background.
perl -e 'alarm 25; exec @ARGV' osascript <<'AS' || echo "    Finder layout skipped (Automation->Finder not granted; DMG still has the /Applications symlink)"
tell application "Finder"
  tell disk "Orrin"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {300, 140, 940, 580}
    set opts to the icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 112
    set text size of opts to 12
    set background picture of opts to file ".background:bg.png"
    set position of item "Orrin.app" of container window to {160, 205}
    set position of item "Applications" of container window to {480, 205}
    update without registering applications
    delay 1
    close
  end tell
end tell
AS
sync
# Detach may need a retry if a (possibly timed-out) Finder still holds the volume.
hdiutil detach "/Volumes/$VOL" >/dev/null 2>&1 || { sleep 2; hdiutil detach "/Volumes/$VOL" -force >/dev/null 2>&1 || true; }
# `hdiutil convert` can transiently fail with "Resource temporarily unavailable" on busy CI
# runners when the RW image isn't fully released yet. Retry with a force-detach + growing backoff
# instead of failing the whole release build on a flake.
for _attempt in 1 2 3 4 5; do
  if hdiutil convert "$RW" -format UDZO -ov -o "$DMG"; then break; fi
  echo "    hdiutil convert failed (attempt $_attempt) — force-detaching + retrying…"
  hdiutil detach "/Volumes/$VOL" -force >/dev/null 2>&1 || true
  sync; sleep $((_attempt * 3))
  if [ "$_attempt" = 5 ]; then echo "hdiutil convert failed after 5 attempts"; exit 1; fi
done
rm -f "$RW"

if [ -n "${SIGN_ID:-}" ] && [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "==> Notarizing DMG (this uploads to Apple and can take a few minutes)"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  echo "    Notarized + stapled."
else
  echo "==> Skipped signing/notarization (set SIGN_ID + NOTARY_PROFILE to enable)."
  echo "    Unsigned app: Gatekeeper will warn users. Fine for local testing only."
fi

echo "==> Done: $APP  and  $DMG"
