#!/usr/bin/env python3
"""Headless dashboard smoke test (CDP/Playwright).

Boots the dashboard server on a throwaway port, loads it in headless Chromium over the DevTools
protocol, and FAILS on any JS console error, any failed/4xx-5xx same-origin request, or a missing
core panel — the class of regression (empty dashboard, broken settings page, dead /api/* call) that
screenshots miss. Cross-OS and CI-friendly; never touches a live instance (own port, orch unstarted).

    python scripts/dashboard_smoke.py            # exit 0 = healthy, 1 = problems, 2 = setup missing

Needs the `playwright` package + a chromium build (`playwright install chromium`).
"""

from __future__ import annotations

import socket
import sys
import threading
import time


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("playwright not installed — `pip install playwright && playwright install chromium`",
              file=sys.stderr)
        return 2

    import uvicorn

    from otto.config import load_settings
    from otto.orchestrator import Orchestrator
    from otto.server.app import create_app
    from otto.server.events import EventBus

    port = _free_port()
    bus = EventBus()
    settings = load_settings()
    # Test the DASHBOARD, not the remote-auth layer: force local, no-token (how a Mac user loads it).
    settings.server.allow_remote = False
    settings.server.auth_token = ""
    settings.server.port = port          # the CSRF/origin guard builds its allow-list from this
    app = create_app(Orchestrator(settings, bus), bus)   # orch NOT started — routes only, hermetic
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", loop="asyncio"))
    threading.Thread(target=server.run, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):                                  # wait up to ~10s for the server
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    console_errors: list[str] = []
    bad_responses: list[str] = []
    failed_requests: list[str] = []
    problems: list[str] = []

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"chromium not available — `playwright install chromium` ({e})",
                      file=sys.stderr)
                return 2
            page = browser.new_page()
            page.on("console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            page.on("requestfailed",
                    lambda r: failed_requests.append(r.url) if r.url.startswith(base) else None)

            def _on_response(resp):
                if resp.url.startswith(base) and resp.status >= 400:
                    bad_responses.append(f"{resp.status} {resp.url}")
            page.on("response", _on_response)

            # Home: the main app shell must render (the "empty dashboard" regression).
            page.goto(base + "/", wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(600)                    # let app.js wire up + fetch /api/*
            if page.query_selector(".app") is None:
                problems.append("main .app container missing on /")

            # Settings page must render its own body too.
            page.goto(base + "/settings", wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(400)
            if not (page.query_selector(".wrap") and page.query_selector(".card")):
                problems.append("settings page rendered no cards")
            browser.close()
    except Exception as e:
        problems.append(f"browser run failed: {e!r}")
    finally:
        server.should_exit = True

    if console_errors:
        problems.append(f"{len(console_errors)} console error(s): {console_errors[:5]}")
    if bad_responses:
        problems.append(f"{len(bad_responses)} bad response(s): {bad_responses[:5]}")
    if failed_requests:
        problems.append(f"{len(failed_requests)} failed request(s): {failed_requests[:5]}")

    if problems:
        print("DASHBOARD SMOKE FAILED:", file=sys.stderr)
        for pr in problems:
            print("  -", pr, file=sys.stderr)
        return 1
    print("dashboard smoke OK — panels rendered, no console errors, no failed requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
