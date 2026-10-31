"""C1a — product-name consistency ("Orrin") + a configurable wake word (default "Otto").

A regression gate: the product is always surfaced as "Orrin"; "Otto" appears only as the (default,
configurable) wake word; and changing the wake word actually changes what wakes Orrin.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_product_name_is_orrin_in_user_facing_copy():
    mb = _read("otto/menubar.py")
    assert 'super().__init__("Orrin", title="Orrin"' in mb      # menu-bar app title
    assert '"🎤 Talk to Orrin"' in mb and '"Quit Orrin"' in mb   # key menu items
    assert "<title>Orrin" in _read("otto/server/web/index.html")  # dashboard title
    assert "# Orrin" in _read("README.md")                        # docs
    # No stray "Jarvis" as the PRODUCT name in app copy (the oww backend's "hey jarvis" phrase,
    # which only appears in README/config, is allowed and not checked here).
    for rel in ("otto/menubar.py", "otto/server/web/index.html", "otto/server/web/settings.html"):
        assert not re.search(r"jarvis", _read(rel), re.I), f"stray 'Jarvis' in {rel}"


def test_wake_word_default_and_documented_as_configurable():
    from otto.config import load_settings
    assert load_settings().voice.stt.wake_words == "hey otto"     # default
    s = _read("otto/server/web/settings.html")
    assert 'id="stt_wake"' in s and "Default" in s                # UI field + "configurable" helper
    assert "configurable" in _read("README.md").lower()


def test_sleeping_hint_uses_configured_wake_word():
    # The menu-bar sleeping label no longer hardcodes "Otto" — it's appended from the config.
    mb = _read("otto/menubar.py")
    assert '"☾ Sleeping",' in mb and "_wake_word" in mb
    assert "say “Otto”" not in mb                                  # the old hardcode is gone


def test_wake_word_is_honored_by_match(monkeypatch):
    """Changing wake_words changes what wakes Orrin — proving it's configurable, not hardcoded."""
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    from otto.config import load_settings
    from otto.orchestrator import Orchestrator
    from otto.server.events import EventBus
    orch = Orchestrator(load_settings(), EventBus())
    orch.settings.voice.stt.wake_words = "computer"
    matched, remainder = orch._match_wake("computer what's the time")
    assert matched and "time" in remainder
    assert orch._match_wake("otto what's the time")[0] is False    # old word no longer wakes it
