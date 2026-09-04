# Seamless installs: Developer ID signing + notarization

This makes Orrin install and run with **no Gatekeeper warning** and permissions that **persist
across every rebuild**. macOS still asks the user to **Allow the microphone once** (Apple
requires this for any app, signed or not) — but after that one click it sticks forever.

Everything in the build is already wired for this. You only need to do the one-time Apple setup
below (it needs your Apple Developer account — I can't do this part for you), then run one build
command. After that, `build.sh` produces a signed + notarized + stapled `dist/Orrin.dmg`.

## One-time setup (≈15 min)

1. **Enroll in the Apple Developer Program** ($99/yr): https://developer.apple.com/programs/enroll/

2. **Get a "Developer ID Application" certificate** (installs into your login keychain):
   - Easiest via Xcode: **Xcode ▸ Settings ▸ Accounts**, add your Apple ID, **Manage
     Certificates… ▸ + ▸ Developer ID Application**.
   - Or on the web: **Certificates, IDs & Profiles ▸ + ▸ Developer ID Application**, upload a CSR
     (Keychain Access ▸ Certificate Assistant ▸ *Request a Certificate From a CA*), download the
     `.cer`, double-click to install.
   - Verify it's there:
     ```bash
     security find-identity -v -p codesigning
     ```
     You should see a line like: `Developer ID Application: Your Name (TEAMID)`.

3. **Create an app-specific password** for notarization: https://appleid.apple.com ▸ **Sign-In &
   Security ▸ App-Specific Passwords ▸ +** (name it e.g. "orrin-notary").

4. **Store the notary credential** in your keychain (one time):
   ```bash
   xcrun notarytool store-credentials orrin-notary \
     --apple-id "you@apple.id" --team-id TEAMID --password "the-app-specific-password"
   ```

## Build a signed + notarized DMG

```bash
SIGN_ID="Developer ID Application: Your Name (TEAMID)" \
NOTARY_PROFILE=orrin-notary \
  bash packaging/pyinstaller/build.sh
```

This signs the app inside-out with the hardened runtime + `entitlements.plist` (mic + calendar +
the Python exceptions), builds the installer DMG, uploads it to Apple to notarize, waits, and
staples the ticket. The result — `dist/Orrin.dmg` — opens on any Mac with no warning.

## What the user experiences after this

1. Open `Orrin.dmg`, drag **Orrin** to **Applications** — no "unidentified developer" warning.
2. Launch it; click **Allow** on the microphone prompt once (Apple requires this for any app).
3. Done — it listens, and the grant persists across future updates.

> Without this, the app is ad-hoc signed: Gatekeeper warns on first open (right-click ▸ Open),
> and the mic permission has to be re-granted whenever the app is rebuilt.
