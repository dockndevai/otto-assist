#!/usr/bin/env bash
# Create a STABLE self-signed code-signing identity for local Orrin builds — once.
#
#   bash scripts/setup_signing.sh
#
# Why: an ad-hoc signature (PyInstaller's default) gets a NEW code hash on every rebuild, so macOS
# treats each reinstall as a brand-new app and forgets every privacy grant (Microphone,
# Accessibility, Screen Recording). You then have to re-grant on every reinstall.
#
# A self-signed certificate that stays the same across rebuilds keeps the app's "designated
# requirement" constant, so TCC remembers the grants — you grant once, then reinstalls just work.
# build.sh auto-detects this identity (by name) and signs with it. Idempotent: safe to re-run.
#
# Remove later with:  security delete-identity -c "Orrin Local Signing"
set -euo pipefail

NAME="${LOCAL_SIGN_ID:-Orrin Local Signing}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }

if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  bold "✓ Signing identity \"$NAME\" already exists — nothing to do."
  echo "  Rebuild (packaging/pyinstaller/build.sh) and it will be used automatically."
  exit 0
fi

bold "▶ Creating self-signed code-signing identity: \"$NAME\""
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS="orrin-local"

# A code-signing leaf cert: digitalSignature + codeSigning EKU, not a CA.
openssl req -x509 -newkey rsa:2048 -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -days 3650 -nodes \
  -subj "/CN=$NAME" \
  -addext "keyUsage=critical,digitalSignature" \
  -addext "extendedKeyUsage=critical,codeSigning" \
  -addext "basicConstraints=critical,CA:false" >/dev/null 2>&1

# -legacy: macOS's Security framework rejects OpenSSL 3's default PKCS#12 MAC/PBE.
openssl pkcs12 -export -legacy -out "$TMP/id.p12" -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -passout "pass:$PASS" >/dev/null 2>&1

# Import key+cert into the login keychain and pre-authorize codesign to use it (no prompt at build).
security import "$TMP/id.p12" -k "$KEYCHAIN" -T /usr/bin/codesign -P "$PASS" >/dev/null

if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  bold "✓ Created \"$NAME\" in your login keychain."
  echo "  It is self-signed (untrusted) — that's fine for signing and for TCC persistence;"
  echo "  Gatekeeper will still warn on first open, same as before."
  echo "  Next: rebuild + reinstall (scripts/reinstall_app.sh) and grant permissions ONCE."
else
  echo "✗ Import did not stick. Try creating it via Keychain Access ▸ Certificate Assistant" >&2
  echo "  ▸ Create a Certificate… (name \"$NAME\", type: Code Signing)." >&2
  exit 1
fi
