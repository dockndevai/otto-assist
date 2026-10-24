"""Point Otto's config at the repo (not the real ~/Library dir) during tests, so the suite
reads the packaged config.yaml and never pollutes the user's data dir."""

import os
from pathlib import Path

import pytest

# Must be set before `otto.config` is imported.
os.environ.setdefault("OTTO_HOME", str(Path(__file__).resolve().parent.parent))
# Keep the offline suite offline: the orchestrator's post-turn memory capture would otherwise
# spawn a live model call. Tests that exercise capture enable it explicitly on their own
# Settings instance.
os.environ.setdefault("OTTO_MEMORY__AUTO_CAPTURE", "false")
# Don't create the SQLite system-of-record under the repo (OTTO_HOME) during the suite; store
# tests construct their own Store(tmp_path) and set orch.store explicitly.
os.environ.setdefault("OTTO_MEMORY__STORE", "false")
# Likewise, the hands-free command-verify gate makes a live model call — off for the offline
# suite; tests that exercise it enable it explicitly and monkeypatch the model.
os.environ.setdefault("OTTO_VOICE__STT__HANDS_FREE_VERIFY", "false")


@pytest.fixture(autouse=True)
def _no_config_writes(monkeypatch):
    """Because OTTO_HOME points at the repo, any code path that calls save_config would rewrite
    the packaged config.yaml (stripping its comments). Neuter it everywhere by default; tests
    that assert on persistence monkeypatch their own capture over this."""
    noop = lambda data: None
    for mod in ("otto.config", "otto.orchestrator", "otto.server.app"):
        try:
            monkeypatch.setattr(f"{mod}.save_config", noop, raising=False)
        except Exception:
            pass
