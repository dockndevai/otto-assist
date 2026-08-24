"""Offline tests for the wiring that doesn't need a live model: config/storage, the event
bus, voice/screen tool construction, permission routing, project discovery, the MCP server,
and the orchestrator's turn-taking logic.

The live path (Otto running the user's Claude, resuming a project session) is exercised by
running the app; it needs a valid `claude` login and is not part of this offline suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from otto.brain.agent import VOICE_WRAPPER, ClaudeBrain
from otto.brain.tools import build_server, qualified_names
from otto.config import PROJECT_ROOT, load_settings
from otto.mcpserver import build_mcp
from otto.orchestrator import Orchestrator
from otto.projects import _decode, list_projects
from otto.server.app import create_app
from otto.server.events import EventBus


class FakeCtx:
    def __init__(self):
        self._answer = True
    async def say(self, t): ...
    async def ask_user(self, q): return "x"
    async def confirm(self, action): return self._answer
    def render_canvas(self, c, f="html"): ...
    def set_status(self, t): ...
    def notify(self, t, b=""): ...


def test_config_precedence(monkeypatch):
    monkeypatch.setenv("OTTO_BRAIN__MODEL", "opus")
    s = load_settings()
    assert s.brain.model == "opus"                 # env overrides yaml
    assert s.brain.type == "claude"
    assert s.voice.stt.wake_words == "hey otto"    # from yaml
    assert s.server.port == 8000


def test_event_bus_fanout():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.status("idle", "ok")
    assert q1.get_nowait()["type"] == "status"
    assert q2.get_nowait()["state"] == "idle"
    bus.unsubscribe(q1)
    bus.canvas("<b>x</b>")
    assert q2.get_nowait()["type"] == "canvas_render"


def test_voice_tools_build():
    server = build_server(FakeCtx())
    assert server is not None
    names = qualified_names()
    assert "mcp__voice__render_canvas" in names
    assert "mcp__voice__ask_user" in names


def test_defers_to_claude_config():
    # ClaudeBrain injects only voice manners; no custom persona/subagents/connectors.
    assert "Otto" in VOICE_WRAPPER and "text-to-speech" in VOICE_WRAPPER
    import otto.brain as pkg
    assert not (Path(pkg.__file__).parent / "subagents.py").exists()
    assert not (Path(pkg.__file__).parent / "connectors.py").exists()


def test_read_only_classifier():
    from otto.brain.agent import _is_read_only
    # reads flow freely
    assert _is_read_only("mcp__claude_ai_Gmail__search_threads")
    assert _is_read_only("mcp__gdrive__list_files")
    assert _is_read_only("Read") and _is_read_only("WebSearch")
    # writes require confirmation
    assert not _is_read_only("mcp__claude_ai_Gmail__send_message")
    assert not _is_read_only("mcp__cal__create_event")
    assert not _is_read_only("Bash")          # unknown/dangerous -> not auto-read


def test_bash_read_only_classifier():
    from otto.brain.agent import _bash_is_read_only
    assert _bash_is_read_only("date")
    assert _bash_is_read_only("gh repo list --limit 5")
    assert _bash_is_read_only("git status")
    assert not _bash_is_read_only("rm -rf /tmp/x")
    assert not _bash_is_read_only("echo hi > file.txt")     # redirection = write
    assert not _bash_is_read_only("gh repo delete foo")
    assert not _bash_is_read_only("git push")


def test_read_only_permission_allows_without_confirm():
    async def run():
        s = load_settings()
        s.brain.permission = "voice"           # would normally prompt
        b = ClaudeBrain(FakeCtx(), s)
        # a Gmail *read* is allowed with no confirm even in voice mode
        r = await b._permission("mcp__claude_ai_Gmail__search_threads", {}, None)
        assert isinstance(r, PermissionResultAllow)
    asyncio.run(run())


def test_permission_routing():
    async def run():
        s = load_settings()
        ctx = FakeCtx()
        b = ClaudeBrain(ctx, s)
        s.brain.permission = "auto"
        assert isinstance(await b._permission("Bash", {"command": "ls"}, None), PermissionResultAllow)
        s.brain.permission = "deny"
        assert isinstance(await b._permission("Bash", {}, None), PermissionResultDeny)
        # Otto's own voice/screen tools are always allowed, even under deny.
        assert isinstance(await b._permission("mcp__voice__say", {}, None), PermissionResultAllow)
        # voice mode defers to ctx.confirm
        s.brain.permission = "voice"
        ctx._answer = True
        assert isinstance(await b._permission("Bash", {"command": "x"}, None), PermissionResultAllow)
        ctx._answer = False
        assert isinstance(await b._permission("Bash", {"command": "x"}, None), PermissionResultDeny)
    asyncio.run(run())


def test_projects_discovery():
    # _decode round-trips a real directory (encode PROJECT_ROOT the way Claude does).
    encoded = str(PROJECT_ROOT).replace("/", "-")   # leading "/" -> leading "-"
    assert _decode(encoded) == str(PROJECT_ROOT)
    # list_projects always includes the current project, marked.
    projs = list_projects(current=str(PROJECT_ROOT), pinned=[])
    cur = [p for p in projs if p.current]
    assert cur and cur[0].path == str(PROJECT_ROOT)


def test_app_routes_and_mcp():
    s = load_settings()
    app = create_app(Orchestrator(s, EventBus()), EventBus())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/" in paths and "/ws" in paths and "/settings" in paths
    assert any(p.startswith("/api/config") for p in paths)
    # MCP sub-app mounted when register_mcp is on.
    assert any("/mcp" in getattr(r, "path", "") for r in app.routes)


def test_mcp_server_builds():
    mcp = build_mcp(Orchestrator(load_settings(), EventBus()))
    assert mcp is not None
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"speak", "show", "ask", "notify", "status"} <= names


def test_listening_state():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        assert orch.state == "starting"
        orch._on_speech_start()                       # wake word / speech begins
        assert orch.state == "listening"
        orch._on_recording_stop()                     # speech ends, no command
        await asyncio.sleep(2.7)                       # watchdog resets after ~2.5s
        assert orch.state == "idle"
    asyncio.run(run())


def test_ptt_bypasses_wake_word():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        orch.settings.voice.stt.wakeword_backend = "phrase"
        seen = {}
        orch.submit_user_input = lambda t: seen.setdefault("cmd", t)  # capture
        # Without arming, a non-wake utterance is ignored.
        orch._on_voice_final("what is the time")
        assert "cmd" not in seen
        # Armed (push-to-talk): the utterance goes straight through, no wake word.
        orch.arm_ptt()
        assert orch._ptt_armed is True
        orch._on_voice_final("what is the time")
        assert seen.get("cmd") == "what is the time"
        assert orch._ptt_armed is False
    asyncio.run(run())


def test_phrase_wake_matcher():
    orch = Orchestrator(load_settings(), EventBus())
    orch.settings.voice.stt.wake_words = "hey otto"
    assert orch._match_wake("Hey Otto, what's the time?") == (True, "whats the time")
    assert orch._match_wake("otto open my email") == (True, "open my email")   # "hey" optional
    assert orch._match_wake("Hey Otto") == (True, "")                          # bare wake
    assert orch._match_wake("Auto, what is the time?") == (True, "what is the time")  # misheard
    assert orch._match_wake("what's the weather") == (False, "")              # no wake word
    assert orch._match_wake("someone said hey otto later") == (False, "")     # must be a prefix


def test_porcupine_custom_keyword_shim(monkeypatch):
    monkeypatch.setenv("PICOVOICE_ACCESS_KEY", "KEY123")
    import pvporcupine
    from otto.voice.stt import _install_porcupine_custom_keyword_shim
    captured = {}
    monkeypatch.setattr(pvporcupine, "create", lambda **kw: captured.update(kw) or "PPN")
    _install_porcupine_custom_keyword_shim()
    pvporcupine.create(keywords=["/tmp/hey_otto.ppn"], sensitivities=[0.6])
    assert captured["keyword_paths"] == ["/tmp/hey_otto.ppn"]
    assert "keywords" not in captured
    assert captured["access_key"] == "KEY123"


def test_history_persist_and_replay(tmp_path):
    from otto.history import History
    h = History(tmp_path / "history.jsonl")
    bus = EventBus()
    bus.add_sink(h.sink)
    bus.transcript("user", "hello")
    bus.transcript("assistant", "hi there")
    bus.canvas("<b>old</b>")
    bus.canvas("<b>new</b>")
    bus.status("idle", "ignored")          # not persisted
    loaded = h.load()
    kinds = [e["type"] for e in loaded]
    assert kinds.count("transcript_final") == 2
    # only the latest canvas is replayed, and it comes last
    canvases = [e for e in loaded if e["type"] == "canvas_render"]
    assert len(canvases) == 1 and canvases[0]["content"] == "<b>new</b>"
    assert "status" not in kinds


def test_render_canvas_publishes():
    bus = EventBus()
    orch = Orchestrator(load_settings(), bus)
    q = bus.subscribe()
    orch.render_canvas("<h1>Chart</h1>", "html")
    ev = q.get_nowait()
    assert ev["type"] == "canvas_render" and "Chart" in ev["content"]


def test_ask_user_answer_routing():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        task = asyncio.create_task(orch.ask_user("pick a or b?"))
        await asyncio.sleep(0.05)
        orch.submit_user_input("b")
        assert await asyncio.wait_for(task, 2) == "b"
    asyncio.run(run())


def test_confirm_parsing():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()

        async def answer(text):
            t = asyncio.create_task(orch.confirm("do a thing"))
            await asyncio.sleep(0.05)
            orch.submit_user_input(text)
            return await asyncio.wait_for(t, 2)

        assert await answer("yes") is True
        assert await answer("go ahead") is True
        assert await answer("no thanks") is False
    asyncio.run(run())
