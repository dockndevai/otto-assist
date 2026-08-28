# Orrin — Lemon Squeezy listing pack

Everything to create the Lemon Squeezy product. Copy the blocks verbatim; adjust price to taste.
Lemon Squeezy is the merchant of record — it hosts the checkout, collects tax/VAT, delivers the
file, and issues license keys. You don't need a website or a payment backend.

---

## Product identity
- **Name:** Orrin
- **Tagline (≤60 chars):** Give your Claude a voice.
- **Category:** Software / Developer tools (macOS)
- **What the buyer receives:** `Orrin.dmg` (a signed, notarized macOS app) + a license key.

## Short description (store card, ~160 chars)
> A voice and a screen for the Claude Code you already run. Talk to it, hear it answer, and
> watch replies render on a JARVIS-style canvas — hands-free, on your Mac.

## Long description (paste into the product body)
**Orrin gives the Claude you already run a voice and a face.**

It isn't another AI — it's the wiring. Orrin plugs into the Claude Code already installed on
your Mac and lets you *talk* to it. Ask out loud, hear the answer, and watch it render as a
live visual — a clock, a weather scene, a diagram, a chart — on a JARVIS-style canvas. The
full conversation waits quietly in a drawer until you want it.

Because Orrin loads *your* Claude, everything you've already set up just works, spoken:
your MCP connectors (Gmail, Drive, GitHub…), your subagents, your skills, your `CLAUDE.md`.

**Why people use it**
- Hands-free Claude while you code, read, or step away from the keyboard.
- Answers you can *glance* at, not scroll through.
- Your existing setup, no reconfiguration.

**How it works**
1. Say your wake word (“Otto”) or click the mic.
2. Orrin runs the Claude on your machine — your connectors, your project context.
3. You hear the reply and see it rendered on the canvas.

**Private by design.** Your microphone is transcribed on-device with local Whisper. Only your
prompt reaches your Claude — there is no Orrin cloud in the middle.

**Bring your own Claude.** Orrin is the shell, not the brain. Point it at the Claude Code you
already pay for — your account, your usage, your control.

> Orrin is an independent product and is not affiliated with, or endorsed by, Anthropic.
> “Claude” is a trademark of Anthropic.

## Feature bullets (for the highlights block)
- 🎙️ Talk to your own Claude — voice in, voice out
- 🖥️ Canvas-first answers (clock, weather, diagrams, charts)
- 🧩 Inherits your MCP connectors, subagents, skills & `CLAUDE.md`
- 🍎 Lives in your menu bar; starts at login
- 🔒 On-device transcription — no Orrin cloud
- 🔌 Also registers as an MCP so any Claude session gains voice + screen

## Requirements / "before you buy" (put this above the fold)
- macOS 12 or later, Apple silicon
- **Claude Code installed and signed in** (Claude Pro/Team or an Anthropic API key) — Orrin
  uses your own Claude and does not include model access
- Microphone permission (asked on first launch)

---

## Pricing (recommendation)
Dev-audience utility → **one-time is simplest and converts best**. License keys via Lemon Squeezy.

- **Launch price: $29 one-time** (early adopter), regular **$39** — includes 1 year of updates.
- Alternative if you want recurring revenue: **$4/mo or $39/yr** subscription.
- Consider a **free 7-day trial** (Lemon Squeezy supports trials) to offset the BYO-Claude ask.

Start with one-time $29 launch; you can add a "Pro" tier later without re-listing.

## License keys
- Enable **License keys** on the product; Lemon Squeezy generates one per purchase and emails it.
- v1 can sell *without* hard in-app enforcement (honor-system key). Add validation later via
  Lemon Squeezy's license API (`/v1/licenses/validate`) — a small check at first launch.

## File to upload
- `dist/Orrin.dmg` — **must be signed + notarized first** (see `packaging/pyinstaller/BUILD.md`),
  or buyers hit Gatekeeper warnings. This is the one blocker that needs your Apple Developer ID.

## Visual assets checklist
Lemon Squeezy shows a cover image + a gallery. Use:
- **Cover / hero:** the landing-page hero (Orrin wordmark + HUD). (Artifact already built.)
- **Gallery:** the demo GIF + 2–3 dashboard screenshots showing canvas cards.
- ⚠️ Re-capture these with the new **Orrin** branding (the app header now says "Orrin"); the
  current `assets/otto-demo.gif` shows the old "Otto" header. Ask Claude to regenerate.

## FAQ (add as product FAQ)
- **Do I need a separate subscription?** Yes — Orrin uses your own Claude Code (Claude Pro/Team
  or an API key). Orrin doesn't include model access.
- **Does my audio leave my Mac?** Transcription runs on-device. Only the resulting text prompt
  goes to your Claude, exactly as if you'd typed it.
- **Which Macs?** Apple silicon, macOS 12+.
- **Is it affiliated with Anthropic?** No. Independent product; "Claude" is Anthropic's trademark.
- **Refunds?** Handled by Lemon Squeezy per their policy.

## Store setup checklist (in the Lemon Squeezy dashboard)
1. Create Store → set name, logo (the 🤖 mark / Orrin wordmark).
2. New Product → paste name, short + long description, price ($29 launch).
3. Upload `Orrin.dmg` (signed) as the deliverable file.
4. Enable **License keys**.
5. Add cover + gallery images (see checklist above).
6. Add the FAQ entries.
7. (Optional) Turn on a 7-day free trial.
8. Publish → share the hosted checkout/product URL (this *is* your storefront — no site needed).

## Still needed from you (outside Lemon Squeezy)
- ⚖️ Apple Developer ID → sign + notarize the DMG (`SIGN_ID`/`NOTARY_PROFILE` in build.sh).
- ⚖️ Confirm Anthropic's commercial terms for a paid product built on the Agent SDK.
- ⚖️ Trademark-clear "Orrin" before scaling marketing.
