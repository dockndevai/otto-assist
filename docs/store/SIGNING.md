# Sign, notarize & ship Orrin.dmg — copy-paste sheet

Run these once your **Apple Developer Program** enrollment is active. This produces the
signed, notarized `dist/Orrin.dmg` you upload to Lemon Squeezy. (Claude can run step 3–4 for
you; you must do the Apple enrollment + generate the app-specific password yourself.)

## What you need first
- Apple Developer Program membership (active).
- A **Developer ID Application** certificate installed in your login keychain
  (Xcode → Settings → Accounts → Manage Certificates → +, or the developer portal).
- Your **Team ID** — developer.apple.com → Membership details (10 chars, e.g. `AB12CD34EF`).
- An **app-specific password** — appleid.apple.com → Sign-In and Security → App-Specific
  Passwords → generate (looks like `abcd-efgh-ijkl-mnop`).

## 1. Find your exact signing identity
```bash
security find-identity -v -p codesigning
```
Copy the full quoted name, e.g. `Developer ID Application: Ada Lovelace (AB12CD34EF)`.

## 2. Store a notarization credential (one-time, saved in your keychain)
```bash
xcrun notarytool store-credentials orrin-notary \
  --apple-id "you@appleid.com" \
  --team-id "AB12CD34EF" \
  --password "abcd-efgh-ijkl-mnop"
```
(The app-specific password stays on your machine; never share it.)

## 3. Build + sign + notarize + staple + DMG
```bash
cd ~/Documents/projects/personal-assist
SIGN_ID="Developer ID Application: Ada Lovelace (AB12CD34EF)" \
NOTARY_PROFILE=orrin-notary \
  bash packaging/pyinstaller/build.sh
```
Notarization uploads the app to Apple and waits (a few minutes). Output: **`dist/Orrin.dmg`**
— signed, notarized, stapled.

## 4. Verify before uploading
```bash
spctl -a -vvv -t install dist/Orrin.app     # expect: "accepted" + "Notarized Developer ID"
xcrun stapler validate dist/Orrin.dmg       # expect: "The validate action worked!"
```

## 5. Upload
Upload `dist/Orrin.dmg` as the deliverable file on your Lemon Squeezy product
(see `docs/store/lemonsqueezy.md`).

> Substitute your real name/Team ID/Apple ID above. Keep the app-specific password private —
> never paste it into chat.
