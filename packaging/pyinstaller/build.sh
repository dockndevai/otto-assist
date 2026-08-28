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
rm -f "$DMG"
hdiutil create -volname "Orrin" -srcfolder "$APP" -ov -format UDZO "$DMG"

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
