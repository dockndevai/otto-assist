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

echo "==> Ensuring build tooling (pyinstaller) in the venv"
[ -x .venv/bin/python ] || { echo "Run scripts/setup.sh first (.venv missing)"; exit 1; }
.venv/bin/pip install -q --upgrade pyinstaller

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

echo "==> Building Orrin.app"
rm -rf build dist
.venv/bin/pyinstaller packaging/pyinstaller/orrin.spec --noconfirm
[ -d "$APP" ] || { echo "Build failed: $APP not produced"; exit 1; }
echo "    Built $APP"

if [ -n "${SIGN_ID:-}" ]; then
  echo "==> Code-signing (hardened runtime)"
  # Sign nested code first, then the app, with an entitlements file allowing the mic + JIT.
  find "$APP" -name "*.dylib" -o -name "*.so" | while read -r f; do
    codesign --force --timestamp --options runtime --sign "$SIGN_ID" "$f" || true
  done
  codesign --force --deep --timestamp --options runtime \
    --entitlements packaging/pyinstaller/entitlements.plist \
    --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
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
hdiutil convert "$RW" -format UDZO -ov -o "$DMG"
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
