# Taking Orrin commercial

Working brand: **Orrin** (the app). Assistant persona / wake word: **Otto** (configurable).
Repo, CLI (`otto`), Python package (`otto/`), and config keys stay as-is until the name is
legally cleared — this doc tracks the path from personal tool → product.

> Status: groundwork. Nothing here is legal advice — items marked ⚖️ need a professional.

## 0. The gating question — business model ⚖️
Orrin is a **wiring layer over the customer's own Claude**. Pick the model before building GTM:

- **BYO-Claude (recommended to start).** The customer supplies their own Claude Code / API
  key; Orrin never brokers Anthropic access. Cleanest legally; market = people already paying
  Anthropic.
- **Hosted (you resell Claude).** You provide the model access. **Verify Anthropic's
  commercial/redistribution terms first** — this may not be permitted, and changes pricing and
  liability entirely.

**Action:** confirm Anthropic's terms for building a commercial product on the Agent SDK and
for any resale before investing in launch.

## 1. Name & brand
- Candidate **Orrin** passed a preliminary web scan (no AI/software product or obvious mark).
- ⚖️ Before filing/announcing: USPTO clearance search + a trademark attorney.
- Check domains: `orrin.ai`, `getorrin.com`, `orrinapp.com`, etc.
- When cleared: rename repo, Homebrew tap/formula, CLI command, bundle id, config keys, docs.
  Until then, keep internal names as `otto` to avoid churn and broken installs.

## 2. Legal foundation
- **License:** `LICENSE` is currently **proprietary / all rights reserved** (safe default).
  Decide: proprietary vs **open-core** (open shell, paid features) vs source-available (BUSL).
- ⚖️ **Privacy policy + ToS** — required before shipping: Orrin records microphone audio and
  transcribes it (on-device), and forwards prompts to the user's Claude. State exactly what is
  captured, where it goes, and what is/ isn't retained.
- **THIRD_PARTY_NOTICES.md** — generate from installed deps (`pip-licenses`) for redistribution.

## 3. Consumer-grade distribution
Today's Homebrew tap + first-run venv is developer-grade. For a product:
- **Standalone `.app`** via PyInstaller — see `packaging/pyinstaller/` (scaffolded).
- ⚖️ **Apple Developer Program** ($99/yr) → **code-sign (Developer ID)** + **notarize** the app
  and DMG, so it opens without Gatekeeper warnings.
- Ship a signed **DMG**; auto-update later (Sparkle).
- Mac App Store is likely **out** — its sandbox blocks the menu-bar + launchd + `claude`-spawn
  design.

## 4. Go-to-market
- Landing page: built (Artifact) — wire the waitlist to a real backend (e.g. a form service)
  before launch; today it stores intent locally only.
- Pricing/packaging, payments (Stripe/Paddle), support channel, docs site.

## Immediate next steps
1. ⚖️ Confirm Anthropic commercial terms + pick BYO vs hosted.
2. ⚖️ Trademark clearance for "Orrin" + grab domains.
3. Decide license model (proprietary vs open-core).
4. Build + sign the standalone `.app` (needs your Apple Developer ID).
5. Stand up a real waitlist backend and publish the landing page.
