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
    assert "Claude Code" in VOICE_WRAPPER and "text-to-speech" in VOICE_WRAPPER
    # Coding work should stay in the chat, not be forced into a narrated canvas.
    assert "render_canvas is OPTIONAL" in VOICE_WRAPPER
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


def test_efficientword_backend_wiring(monkeypatch):
    # config carries the few-shot wake-word fields
    monkeypatch.setenv("OTTO_VOICE__STT__WAKEWORD_BACKEND", "efficientword")
    monkeypatch.setenv("OTTO_VOICE__STT__EFF_THRESHOLD", "0.7")
    s = load_settings()
    assert s.voice.stt.wakeword_backend == "efficientword"
    assert s.voice.stt.eff_threshold == 0.7
    # the listener + enrollment modules import
    import importlib
    assert importlib.util.find_spec("otto.voice.wakeword_eff")
    assert importlib.util.find_spec("otto.enroll_wake_word")


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
    # chat (session) switcher endpoints
    assert {"/api/sessions", "/api/session/switch", "/api/session/new",
            "/api/session/rename"} <= paths
    # per-project history replay endpoint
    assert "/api/history" in paths
    # parallel-workspace endpoints
    assert {"/api/workspaces", "/api/workspace/open", "/api/workspace/focus",
            "/api/workspace/close"} <= paths
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


def test_wake_greeting_and_echo_filter():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        orch.settings.voice.stt.wakeword_backend = "phrase"
        seen = {}
        orch.submit_user_input = lambda t: seen.__setitem__("cmd", t)
        # bare wake -> greet + await (a real greeting, not just "yes")
        orch._enter_awaiting_command()
        assert orch._awaiting_command and orch._last_ack in __import__("otto.orchestrator", fromlist=["_WAKE_ACKS"])._WAKE_ACKS
        # Otto hears its own greeting echo -> ignored, still awaiting, no command
        orch._on_voice_final(orch._last_ack)
        assert "cmd" not in seen and orch._awaiting_command
        # the real command after the wake -> submitted
        orch._on_voice_final("what is the weather")
        assert seen.get("cmd") == "what is the weather"
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


def test_pretrust_silero_vad_skips_prompt():
    """The ears must not hang on torch.hub's 'trust this repo? (y/N)' prompt in a
    non-interactive process (launchd/service) — that EOFErrors and leaves Otto deaf."""
    import torch.hub as hub
    from otto.voice.stt import _pretrust_silero_vad

    def _boom(owner, name, *a, **k):
        raise EOFError("EOF when reading a line")   # what the interactive prompt does headless

    hub._check_repo_is_trusted = _boom
    _pretrust_silero_vad()
    # Silero VAD is now trusted (no raise); unrelated repos still defer to the original check.
    assert hub._check_repo_is_trusted("snakers4", "silero-vad") is None
    import pytest
    with pytest.raises(EOFError):
        hub._check_repo_is_trusted("someone", "other-repo")
    _pretrust_silero_vad()          # idempotent — doesn't stack wrappers
    assert hub._check_repo_is_trusted("snakers4", "silero-vad") is None


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
    from otto.memory import Memory
    mem = Memory(tmp_path / "memory", load_settings())
    proj = "/tmp/histproj"
    h = History(mem, lambda: proj)
    bus = EventBus()
    bus.add_sink(h.sink)
    bus.transcript("user", "hello", ws=proj)
    bus.transcript("assistant", "hi there", ws=proj)
    bus.canvas("<b>old</b>", ws=proj)
    bus.canvas("<b>new</b>", ws=proj)
    bus.status("idle", "ignored")          # not persisted
    loaded = h.load(proj)
    kinds = [e["type"] for e in loaded]
    assert kinds.count("transcript_final") == 2
    # only the latest canvas is replayed, and it comes last
    canvases = [e for e in loaded if e["type"] == "canvas_render"]
    assert len(canvases) == 1 and canvases[0]["content"] == "<b>new</b>"
    assert "status" not in kinds
    # per-project isolation: a different project has its own (empty) history
    assert h.load("/tmp/other-proj") == []


def test_history_is_per_project_and_migrates(tmp_path):
    import json
    from otto.history import History
    from otto.memory import Memory
    mem = Memory(tmp_path / "memory", load_settings())
    h = History(mem, lambda: "/tmp/focused")
    bus = EventBus()
    bus.add_sink(h.sink)
    bus.transcript("user", "in project A", ws="/tmp/a")
    bus.transcript("user", "in project B", ws="/tmp/b")
    bus.transcript("user", "untagged goes to focused")     # ws=None -> resolve_project()
    assert [e["text"] for e in h.load("/tmp/a")] == ["in project A"]
    assert [e["text"] for e in h.load("/tmp/b")] == ["in project B"]
    assert [e["text"] for e in h.load("/tmp/focused")] == ["untagged goes to focused"]

    # legacy flat file is split per project, then set aside
    legacy = tmp_path / "history.jsonl"
    legacy.write_text("\n".join([
        json.dumps({"type": "transcript_final", "role": "user", "text": "old A", "ws": "/tmp/a"}),
        json.dumps({"type": "transcript_final", "role": "user", "text": "old default"}),
    ]))
    h.migrate_legacy(legacy, "/tmp/default")
    assert any(e["text"] == "old A" for e in h.load("/tmp/a"))
    assert [e["text"] for e in h.load("/tmp/default")] == ["old default"]
    assert not legacy.exists() and legacy.with_suffix(".jsonl.migrated").exists()


def test_log_bus_streams_to_dashboard():
    """The Logs drawer feed: Otto's INFO + anyone's WARNING/ERROR reach the bus; INFO from
    third-party loggers and known RealtimeSTT poll noise are filtered out."""
    import logging
    from otto.logbus import install_log_bus
    bus = EventBus()
    buffer = install_log_bus(bus)
    q = bus.subscribe()
    logging.getLogger("otto").info("otto diagnostic here")     # kept (otto INFO)
    logging.getLogger("thirdparty").info("chatty info")        # dropped (non-otto INFO)
    logging.getLogger("thirdparty").error("a real failure")    # kept (WARNING+)
    logging.getLogger("root").error("Error receiving data from connection: ")  # dropped (noise)
    events = []
    try:
        while True:
            events.append(q.get_nowait())
    except Exception:
        pass
    msgs = [e["msg"] for e in events if e["type"] == "log"]
    assert any("otto diagnostic here" in m for m in msgs)
    assert any("a real failure" in m for m in msgs)
    assert not any("chatty info" in m for m in msgs)
    assert not any("Error receiving data" in m for m in msgs)
    assert len(buffer) >= 2                                     # ring buffer for replay
    logging.getLogger().removeHandler(logging.getLogger().handlers[-1])  # keep the suite clean


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


# ---- Chat (Claude session) switching --------------------------------------

class _FakeInfo:
    def __init__(self, session_id, custom_title=None, summary="", first_prompt=None,
                 last_modified=0, git_branch=None):
        self.session_id = session_id; self.custom_title = custom_title
        self.summary = summary; self.first_prompt = first_prompt
        self.last_modified = last_modified; self.git_branch = git_branch


class _FakeSDKClient:
    """Stands in for ClaudeSDKClient so connect() spawns no `claude` subprocess."""
    def __init__(self, options=None):
        self.options = options
    async def connect(self): ...
    async def disconnect(self): ...


def test_session_title_fallback_and_sort(monkeypatch):
    from otto.sessions import list_sessions
    infos = [
        _FakeInfo("aaaa1111", custom_title="Auth refactor", last_modified=300),
        _FakeInfo("bbbb2222", summary="Billing summary", last_modified=200),
        _FakeInfo("cccc3333", first_prompt="how do I center a div", last_modified=100),
        _FakeInfo("dddd4444eeee", last_modified=50),          # no labels -> id[:8]
    ]
    monkeypatch.setattr("claude_agent_sdk.list_sessions", lambda **kw: infos, raising=False)
    out = list_sessions("/tmp/proj", current_id="bbbb2222")
    assert [s.title for s in out] == ["Auth refactor", "Billing summary",
                                      "how do I center a div", "dddd4444"]
    assert out[0].session_id == "aaaa1111"                    # sorted most-recent first
    assert next(s for s in out if s.session_id == "bbbb2222").current is True


def test_brain_resume_excludes_continue(monkeypatch):
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    async def run():
        s = load_settings()
        s.brain.continue_conversation = True
        # resume wins: resume set, the global continue flag is NOT carried through
        b = ClaudeBrain(FakeCtx(), s, resume="abc123")
        await b.connect()
        assert getattr(b.client.options, "resume", None) == "abc123"
        assert not getattr(b.client.options, "continue_conversation", False)
        assert b.session_id == "abc123"                       # pre-seeded for a resume
        # new chat: neither resume nor continue -> a fresh session
        b2 = ClaudeBrain(FakeCtx(), s, new_chat=True)
        await b2.connect()
        assert not getattr(b2.client.options, "resume", None)
        assert not getattr(b2.client.options, "continue_conversation", False)
        # default binding: the continue flag is honored
        b3 = ClaudeBrain(FakeCtx(), s)
        await b3.connect()
        assert getattr(b3.client.options, "continue_conversation", False) is True
    asyncio.run(run())


def test_brain_captures_session_id():
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    class _RecvClient:
        async def query(self, text, session_id="default"): ...
        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text="hi")], model="m",
                                   session_id="sid-xyz")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="sid-xyz",
                                total_cost_usd=0.0)

    async def run():
        b = ClaudeBrain(FakeCtx(), load_settings())
        b.client = _RecvClient()
        evs = [ev async for ev in b.send("hi")]
        assert b.session_id == "sid-xyz"
        result = [e for e in evs if e["type"] == "result"][0]
        assert result["session_id"] == "sid-xyz"
    asyncio.run(run())


def test_switch_session_rebuilds_and_persists(monkeypatch):
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    saved = {}
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: saved.update(d))
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        q = orch.bus.subscribe()
        await orch.switch_session("sid1")
        assert orch._active_session == "sid1"
        assert getattr(orch.brain, "session_id", None) == "sid1"
        kinds = []
        try:
            while True:
                kinds.append(q.get_nowait()["type"])
        except Exception:
            pass
        assert "session" in kinds
        assert saved["brain"]["sessions"][orch.current_project] == "sid1"
    asyncio.run(run())


def test_voice_chat_commands_parsed():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        calls = []
        async def _new(): calls.append(("new",))
        async def _rename(t): calls.append(("rename", t))
        async def _byname(n): calls.append(("switch", n))
        async def _turn(t): calls.append(("turn", t))
        orch.new_chat = _new
        orch.rename_chat = _rename
        orch._switch_chat_by_name = _byname
        orch._handle_turn = _turn
        orch.submit_user_input("start a new chat")
        orch.submit_user_input("rename this chat to Auth work")
        orch.submit_user_input("switch to the auth chat")
        await asyncio.sleep(0.05)
        assert [c[0] for c in calls] == ["new", "rename", "switch"]   # no "turn"
        assert ("rename", "Auth work") in calls
        assert ("switch", "auth") in calls
    asyncio.run(run())


# ---- Parallel workspaces --------------------------------------------------

class _FakeBrain:
    """A brain that streams a canned reply without a `claude` subprocess."""
    def __init__(self, reply="done", sid="s1"):
        self.session_id = None
        self._reply = reply
        self._sid = sid
    async def connect(self): ...
    async def disconnect(self): ...
    async def send(self, text):
        yield {"type": "delta", "text": self._reply}
        self.session_id = self._sid
        yield {"type": "result", "is_error": False, "cost": 0.0,
               "error_text": "", "session_id": self._sid}


def _drain(q):
    out = []
    try:
        while True:
            out.append(q.get_nowait())
    except Exception:
        pass
    return out


def test_open_focus_and_cap(monkeypatch):
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        orch.max_workspaces = 2
        start = orch.focused
        ws2 = await orch.open_workspace("/tmp/p2", focus=True)
        assert ws2 is not None and orch.focused == "/tmp/p2"
        assert set(orch.workspaces) == {start, "/tmp/p2"}
        # cap reached -> the third open is refused, pool unchanged
        ws3 = await orch.open_workspace("/tmp/p3", focus=True)
        assert ws3 is None and "/tmp/p3" not in orch.workspaces
        # focusing back keeps both alive and re-points brain/current_project
        orch.focus_workspace(start)
        assert orch.focused == start
        assert orch.brain is orch.workspaces[start].brain
        # closing the focused one falls back to the survivor
        await orch.close_workspace(start)
        assert orch.focused == "/tmp/p2" and start not in orch.workspaces
    asyncio.run(run())


def test_background_workspace_streams_tagged_and_notifies(monkeypatch):
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    async def run():
        from otto.workspace import Workspace
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        bg = Workspace(orch, orch.settings, "/tmp/billing-svc")
        bg.brain = _FakeBrain(reply="all tests pass")
        orch.workspaces["/tmp/billing-svc"] = bg
        q = orch.bus.subscribe()
        await orch._run_turn(bg, "run the tests")
        evs = _drain(q)
        # A background workspace streams into its OWN window — chat events tagged with its
        # path (so the dashboard can show it as a separate chat you switch to)…
        streamed = [e for e in evs if e["type"] in
                    ("assistant_delta", "transcript_final", "tool_use")]
        assert streamed and all(e.get("ws") == "/tmp/billing-svc" for e in streamed)
        assert any(e["type"] == "assistant_delta" for e in evs)
        # …and it still fires a completion notification (the background "done" signal).
        assert any(e["type"] == "notify" and "billing" in e["title"].lower() for e in evs)
        assert bg.last_reply == "all tests pass" and bg.state == "idle"
    asyncio.run(run())


def test_focused_workspace_streams(monkeypatch):
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        focused = orch.focused
        orch.focused_ws.brain = _FakeBrain(reply="hello world", sid="sX")
        q = orch.bus.subscribe()
        await orch._run_turn(orch.focused_ws, "hi")
        evs = _drain(q)
        types = [e["type"] for e in evs]
        assert "assistant_delta" in types
        assert all(e.get("ws") == focused for e in evs if e["type"] == "assistant_delta")
        assert any(e["type"] == "transcript_final" and e.get("role") == "assistant"
                   and e.get("ws") == focused for e in evs)
        assert "canvas_render" not in types        # coding/plain answers no longer forced onto a canvas
        assert not any(e["type"] == "notify" for e in evs)   # focused doesn't self-notify
    asyncio.run(run())


def test_new_chat_then_immediate_turn_uses_new_brain(monkeypatch):
    """A command fired right after 'new chat' must land on the NEW chat, not race the swap
    and hit the old session (found via the JARVIS battery: fresh chat 'remembered' old work)."""
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        ws = orch.focused_ws
        ws.brain = _FakeBrain(reply="OLD-CHAT-MEMORY")
        new = _FakeBrain(reply="NEW-CHAT")

        async def fake_rebind(settings, *, resume=None, new_chat=False):
            await asyncio.sleep(0.1)     # rebind (disconnect+reconnect) takes real time
            ws.brain = new
        ws.rebind = fake_rebind

        q = orch.bus.subscribe()
        # exactly what the WS handler does: schedule new_chat, then the turn, back to back
        t1 = asyncio.create_task(orch.new_chat())
        t2 = asyncio.create_task(orch._run_turn(ws, "which chat is this?"))
        await asyncio.gather(t1, t2)
        replies = [e["text"] for e in _drain(q)
                   if e["type"] == "transcript_final" and e.get("role") == "assistant"]
        assert any("NEW-CHAT" in r for r in replies)          # ran on the new brain
        assert not any("OLD-CHAT-MEMORY" in r for r in replies)
    asyncio.run(run())


def test_clone_command_parsed():
    orch = Orchestrator(load_settings(), EventBus())
    m = orch._RE_CLONE.match("clone https://github.com/acme/widget.git")
    assert m and m.group("url").endswith("acme/widget")
    m2 = orch._RE_CLONE.match("clone https://github.com/acme/widget on branch dev")
    assert m2 and m2.group("branch") == "dev"
    m3 = orch._RE_CLONE.match("set up a new workspace from https://github.com/acme/widget")
    assert m3 and m3.group("url").endswith("acme/widget")
    assert orch._RE_CLONE.match("create a function that adds two numbers") is None


def test_clone_workspace_opens_new_space(tmp_path, monkeypatch):
    """Otto making its own code space: clone a URL -> a fresh focused workspace."""
    monkeypatch.setenv("OTTO_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    import os as _os

    class _FakeProc:
        returncode = 0
        async def communicate(self): return (b"", None)

    async def fake_exec(*args, **kw):          # stand in for `git clone …`
        target = args[-1]
        _os.makedirs(_os.path.join(target, ".git"), exist_ok=True)
        return _FakeProc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        ws = await orch.clone_workspace("https://example.com/acme/widget.git")
        target = str(tmp_path / "ws" / "widget")
        assert ws is not None
        assert target in orch.workspaces and orch.focused == target
        assert _os.path.isdir(_os.path.join(target, ".git"))
    asyncio.run(run())


def test_targeted_turn_routing(monkeypatch):
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    async def run():
        from otto.workspace import Workspace
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        bg = Workspace(orch, orch.settings, "/tmp/billing-svc")
        bg.brain = _FakeBrain()
        orch.workspaces["/tmp/billing-svc"] = bg
        assert orch._resolve_workspace_name("billing") == "/tmp/billing-svc"
        captured = []
        async def _tt(name, cmd, original): captured.append((name, cmd, original))
        orch._targeted_turn = _tt
        orch.submit_user_input("in billing, run the tests")
        await asyncio.sleep(0.05)
        assert captured == [("billing", "run the tests", "in billing, run the tests")]
    asyncio.run(run())


# ---- First-run permissions + preflight health check -----------------------
def test_preflight_check_microphone_statuses(monkeypatch):
    from otto import preflight
    s = load_settings()
    monkeypatch.setattr(preflight, "mic_authorization_status", lambda: "authorized")
    assert preflight.check_microphone(s).status == "ok"
    monkeypatch.setattr(preflight, "mic_authorization_status", lambda: "denied")
    c = preflight.check_microphone(s)
    assert c.status == "fail" and c.pane == "microphone" and c.fix
    monkeypatch.setattr(preflight, "mic_authorization_status", lambda: "notDetermined")
    # undecided is non-blocking: the ears start anyway and opening the mic shows the OS prompt
    assert preflight.check_microphone(s).status == "warn"
    s.voice.input_enabled = False              # voice off -> not required, just a warn
    c = preflight.check_microphone(s)
    assert c.status == "warn" and c.required is False


def test_preflight_summarize_readiness():
    from otto.preflight import Check, summarize
    # a required check that only "warn"s does not block readiness
    assert summarize([Check("a", "A", status="ok"), Check("b", "B", status="warn")]) is True
    # a required "fail" blocks
    assert summarize([Check("a", "A", status="ok"), Check("c", "C", status="fail")]) is False
    # an optional "fail" does not block
    assert summarize([Check("a", "A", status="ok"),
                      Check("d", "D", status="fail", required=False)]) is True


def test_run_preflight_publishes_and_starts_ears(monkeypatch):
    from otto import preflight
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    monkeypatch.setattr(preflight, "request_mic_access", lambda *a, **k: "authorized")
    monkeypatch.setattr(preflight, "mic_authorization_status", lambda: "authorized")
    monkeypatch.setattr(preflight, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(preflight, "mark_setup_complete", lambda: None)

    async def _ok_test(settings, timeout=60.0):
        return preflight.Check("claude_test", "Claude connection (test run)", status="ok")
    monkeypatch.setattr(preflight, "check_claude_test", _ok_test)

    async def run():
        bus = EventBus(); events = []
        bus.add_sink(events.append)
        orch = Orchestrator(load_settings(), bus)
        orch._loop = asyncio.get_running_loop()
        started = {"n": 0}
        monkeypatch.setattr(orch, "_start_ears",
                            lambda: started.__setitem__("n", started["n"] + 1))
        await orch.run_preflight(request=True)
        assert orch.ready is True and orch.preflight_done is True
        pf = [e for e in events if e["type"] == "preflight"]
        assert pf and pf[-1]["done"] is True and pf[-1]["ready"] is True
        keys = {c["key"] for c in pf[-1]["checks"]}
        assert {"microphone", "claude_cli", "claude_test",
                "voice_output", "accessibility"} <= keys
        # mic authorized + no ears running yet -> preflight brings them up
        assert started["n"] == 1
    asyncio.run(run())


def test_start_skips_ears_when_mic_denied(monkeypatch):
    from otto import preflight
    monkeypatch.setattr("otto.brain.agent.ClaudeSDKClient", _FakeSDKClient)
    monkeypatch.setattr(preflight, "request_mic_access", lambda *a, **k: "denied")
    monkeypatch.setattr(preflight, "mic_authorization_status", lambda: "denied")

    async def run():
        s = load_settings()
        s.voice.output_enabled = False          # don't spin up real TTS in the test
        bus = EventBus(); notes = []
        bus.add_sink(lambda e: notes.append(e) if e.get("type") == "notify" else None)
        orch = Orchestrator(s, bus)
        started = {"n": 0}
        monkeypatch.setattr(orch, "_start_ears",
                            lambda: started.__setitem__("n", 1))

        async def _noop(**k):                   # skip the background health task
            return None
        monkeypatch.setattr(orch, "run_preflight", _noop)
        await orch.start()
        assert orch.ears is None and started["n"] == 0
        assert any("Microphone" in (e.get("title") or "") for e in notes)
    asyncio.run(run())


def test_preflight_routes_registered():
    app = create_app(Orchestrator(load_settings(), EventBus()), EventBus())
    paths = {r.path for r in app.routes}
    assert {"/api/preflight", "/api/preflight/recheck",
            "/api/system/open-pane"} <= paths


def test_sleep_and_wake_hands_free(monkeypatch):
    """Always-listen + 'stop listening' sleeps; only the wake word resumes hands-free."""
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        orch.settings.voice.stt.always_listen = True
        orch.settings.voice.stt.wakeword_backend = "phrase"
        orch.settings.voice.stt.wake_words = "otto"
        submitted = []
        monkeypatch.setattr(orch, "submit_user_input", lambda t: submitted.append(t))
        # sleep-command recognition (with/without a leading wake word or greeting)
        assert orch._is_sleep_command("stop listening")
        assert orch._is_sleep_command("otto stop listening")
        assert orch._is_sleep_command("hey otto, go to sleep")
        assert not orch._is_sleep_command("what time is it")
        # a spoken "stop listening" puts Otto to sleep (not sent to the brain)
        orch._on_voice_final("stop listening")
        assert orch._sleeping is True and submitted == []
        # while asleep, ordinary speech is ignored
        orch._on_voice_final("what is the weather today")
        assert submitted == [] and orch._sleeping is True
        # the wake word resumes hands-free; a trailing command runs immediately
        orch._on_voice_final("otto what is the weather")
        assert orch._sleeping is False
        assert submitted and submitted[-1].strip().lower().startswith("what is the weather")
    asyncio.run(run())


def test_half_duplex_suppresses_self_echo(monkeypatch):
    """On a speaker, Otto must drop mic input while it's speaking (and a cooldown after) so it
    doesn't transcribe and answer its own voice in a loop."""
    import time as _t

    class _Mouth:
        def __init__(self): self.said = []
        def is_playing(self): return False
        def speak(self, t): self.said.append(t)

    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        orch.settings.voice.stt.always_listen = True
        orch.mouth = _Mouth()
        submitted = []
        monkeypatch.setattr(orch, "submit_user_input", lambda t: submitted.append(t))
        # speaking a line pushes the mute window out; input is dropped during it
        orch._voice_out("Here is a spoken reply that takes a moment to say out loud.")
        assert orch._output_suppressing() is True
        orch._on_voice_final("Here is a spoken reply that takes a moment to say out loud.")
        assert submitted == []                      # its own echo, dropped
        # once the cooldown passes, real input flows again
        orch._mute_input_until = _t.monotonic() - 1
        orch._on_voice_final("what time is it")
        assert submitted == ["what time is it"]
    asyncio.run(run())


def test_list_system_voices_parses(monkeypatch):
    import otto.voice.tts as tts
    sample = ("Alex                en_US    # Most people recognize me by my voice.\n"
              "Samantha            en_US    # Hi, I'm Samantha!\n"
              "Amelie              fr_CA    # Bonjour, je m'appelle Amelie.\n"
              "Zarvox              en_US    # Beep boop.\n")

    class _R:
        stdout = sample
    monkeypatch.setattr(tts.subprocess, "run", lambda *a, **k: _R())
    voices = tts.list_system_voices()
    names = [v["name"] for v in voices]
    assert {"Alex", "Samantha", "Amelie", "Zarvox"} <= set(names)
    assert voices[0]["locale"].startswith("en")     # English first
    assert voices[-1]["name"] == "Amelie"           # non-English sorted last
    assert voices[0]["sample"]                        # description captured


def test_voice_routes_registered():
    app = create_app(Orchestrator(load_settings(), EventBus()), EventBus())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/api/voices", "/api/voice/select", "/api/voice/preview"} <= paths


def test_set_voice_applies_and_persists(monkeypatch):
    saved = {}
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: saved.update(d))
    orch = Orchestrator(load_settings(), EventBus())
    orch.set_voice("Samantha")
    assert orch.settings.voice.tts.voice == "Samantha"
    assert saved.get("voice", {}).get("tts", {}).get("voice") == "Samantha"


def test_list_all_sessions_aggregates(monkeypatch):
    """Global session index spans every project (cowork included), sorted by recency."""
    import otto.sessions as S

    fake = {
        "/Users/me/projA": [S.Session("a1", "Auth work", "fix auth", 300.0),
                            S.Session("a2", "Old thing", "misc", 100.0)],
        "/private/tmp/claude-x/coworks/web": [S.Session("c1", "Landing page", "build lp", 250.0)],
    }
    dirs = list(fake.keys())

    class _D:
        def __init__(s, p): s.name = p
        def is_dir(s): return True
        def stat(s): return type("st", (), {"st_mtime": 1.0})()

    class _Root:
        def is_dir(s): return True
        def iterdir(s): return [_D(d) for d in dirs]

    monkeypatch.setattr(S, "list_sessions", lambda cwd, current_id=None, limit=25: fake.get(cwd, []))
    monkeypatch.setattr("otto.projects.CLAUDE_PROJECTS", _Root())
    monkeypatch.setattr("otto.projects._decode", lambda name: name)  # each dir decodes to itself

    rows = S.list_all_sessions()
    titles = [r["title"] for r in rows]
    assert titles == ["Auth work", "Landing page", "Old thing"]   # recency order across projects
    web = next(r for r in rows if r["title"] == "Landing page")
    assert web["cowork"] is True and web["project_name"] == "web"
    # query filters by title/project/first-prompt
    assert [r["title"] for r in S.list_all_sessions(query="landing")] == ["Landing page"]


def test_all_session_routes_registered():
    app = create_app(Orchestrator(load_settings(), EventBus()), EventBus())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/api/all-sessions", "/api/session/open-anywhere"} <= paths


def test_voice_partial_emits_and_suppresses():
    async def run():
        bus = EventBus(); events = []
        bus.add_sink(events.append)
        orch = Orchestrator(load_settings(), bus)
        orch._loop = asyncio.get_running_loop()
        # normal: streams a live partial caption
        orch._on_voice_partial("hello wor")
        assert any(e["type"] == "transcript_partial" and e["text"] == "hello wor"
                   and e["role"] == "user" for e in events)
        # asleep: suppressed
        events.clear(); orch._sleeping = True
        orch._on_voice_partial("ignore me")
        assert not events
        orch._sleeping = False
        # while Otto is speaking: suppressed (would be its own echo)
        events.clear()
        orch.mouth = type("M", (), {"is_playing": lambda self: True})()
        orch._on_voice_partial("otto's own words")
        assert not events
    asyncio.run(run())


def test_describe_is_human_and_impactful():
    from otto.brain.agent import _describe
    # destructive → clear impact, never reads the raw command
    d = _describe("Bash", {"command": "rm -rf build"})
    assert "undone" in d.lower() and "rm -rf" not in d
    assert "publishes" in _describe("Bash", {"command": "git push origin main"}).lower()
    assert "force" in _describe("Bash", {"command": "git push --force"}).lower()
    assert "install" in _describe("Bash", {"command": "npm install left-pad"}).lower()
    assert "commit" in _describe("Bash", {"command": "git commit -m x"}).lower()
    # plain read: names the command, doesn't recite it in full
    r = _describe("Bash", {"command": "git status -sb && cat log.txt"})
    assert "git status -sb" not in r
    # files
    assert "edit" in _describe("Edit", {"file_path": "/a/b/foo.py"}).lower()
    assert "foo.py" in _describe("Edit", {"file_path": "/a/b/foo.py"})
    assert "write" in _describe("Write", {"file_path": "x.txt"}).lower()
    # connected (MCP) tool → readable words, no mcp__ noise
    assert _describe("mcp__claude_ai_Gmail__send_message", {}) == "send message"


# ---- persistent cross-session memory ----------------------------------------

def test_memory_slug_and_empty_preamble(tmp_path):
    from otto.memory import Memory, project_slug
    assert project_slug("/Users/x/proj") == "Users-x-proj"
    m = Memory(tmp_path / "memory", load_settings())
    # nothing learned yet → no preamble (a fresh install adds no noise)
    assert m.preamble("/Users/x/proj") == ""


def test_memory_record_turn_writes_log_and_index(tmp_path):
    import json
    from otto.memory import Memory, project_slug
    m = Memory(tmp_path / "memory", load_settings())
    proj = "/tmp/demo-proj"
    m.record_turn(proj, "hello", "hi there", session_id="s1")
    log = (tmp_path / "memory" / "projects" / project_slug(proj) / "turns.jsonl")
    rows = [json.loads(l) for l in log.read_text().splitlines()]
    assert rows and rows[0]["user"] == "hello" and rows[0]["reply"] == "hi there"
    idx = json.loads((tmp_path / "memory" / "index.json").read_text())
    assert idx[project_slug(proj)]["turns"] == 1


def test_memory_capture_applies_facts_and_dedups(tmp_path, monkeypatch):
    from otto.memory import Memory, project_slug
    s = load_settings()
    s.memory.auto_capture = True          # self-contained: don't depend on the suite's env
    m = Memory(tmp_path / "memory", s)
    proj = "/tmp/demo-proj"

    async def fake_extract(self, project_path, user_text, reply):
        return {"user_facts": ["The user prefers concise answers"],
                "project_facts": ["The project deploys from the release branch"],
                "summary": "Set up deploy conventions."}
    monkeypatch.setattr(Memory, "_extract", fake_extract)

    r1 = asyncio.run(m.capture(proj, "how do we deploy?", "From the release branch."))
    assert r1["user_facts"] == 1 and r1["project_facts"] == 1 and r1["summarized"]

    g = (tmp_path / "memory" / "global.md").read_text()
    pj = (tmp_path / "memory" / "projects" / project_slug(proj) / "project.md").read_text()
    assert "concise answers" in g and "release branch" in pj

    # same facts again → de-duplicated (nothing new added)
    r2 = asyncio.run(m.capture(proj, "remind me", "release branch again."))
    assert r2["user_facts"] == 0 and r2["project_facts"] == 0
    assert g.count("concise answers") == 1

    # now the preamble carries the durable context, clearly delimited + bounded
    pre = m.preamble(proj)
    assert "Orrin memory" in pre and "release branch" in pre and "concise answers" in pre
    assert len(pre) <= load_settings().memory.max_preamble_chars + 20


def test_memory_preamble_is_bounded(tmp_path):
    from otto.memory import Memory
    s = load_settings()
    s.memory.max_preamble_chars = 300
    m = Memory(tmp_path / "memory", s)
    proj = "/tmp/big"
    facts = [f"durable fact number {i} about the project that is fairly wordy" for i in range(40)]
    m._append_facts(m.project_dir(proj) / "project.md", "# Project\n", facts)
    pre = m.preamble(proj)
    assert 0 < len(pre) <= 300 + 20 and pre.rstrip().endswith("[end memory]")


def test_memory_document_ingest_search_and_preamble(tmp_path):
    from otto.memory import Memory
    m = Memory(tmp_path / "memory", load_settings())
    proj = "/tmp/docproj"
    e = asyncio.run(m.ingest_doc(
        proj, "Deploy runbook",
        "Deploys run from the release branch. Roll back with the previous tag. "
        "The staging database is reset every night."))
    assert e["title"] == "Deploy runbook" and e["chars"] > 0
    assert [d["title"] for d in m.list_docs(proj)] == ["Deploy runbook"]

    # keyword search finds it and returns a relevant excerpt
    hits = m.search_docs(proj, "how do we roll back a deploy?")
    assert hits and hits[0]["title"] == "Deploy runbook"
    assert "roll back" in hits[0]["excerpt"].lower()
    # an unrelated query doesn't match
    assert m.search_docs(proj, "banana smoothie recipe") == []

    # the preamble surfaces the matching note for a relevant turn...
    pre = m.preamble(proj, query="remind me how to roll back")
    assert "Relevant notes" in pre and "Deploy runbook" in pre
    # ...and lists available notes (not full excerpts) when the turn doesn't match
    pre2 = m.preamble(proj, query="what's the weather")
    assert "Saved notes available" in pre2 and "Deploy runbook" in pre2

    # re-ingesting the same title updates in place (no duplicate)
    asyncio.run(m.ingest_doc(proj, "Deploy runbook", "Now deploys from main."))
    assert len(m.list_docs(proj)) == 1


def test_memory_add_fact_scopes_and_dedups(tmp_path):
    from otto.memory import Memory, project_slug
    m = Memory(tmp_path / "memory", load_settings())
    proj = "/tmp/factproj"
    assert asyncio.run(m.add_fact(proj, "Deploys from the release branch")) is True
    assert asyncio.run(m.add_fact(proj, "Deploys from the release branch")) is False  # dup
    assert asyncio.run(m.add_fact(proj, "The user prefers metric units", scope="user")) is True
    pj = (tmp_path / "memory" / "projects" / project_slug(proj) / "project.md").read_text()
    g = (tmp_path / "memory" / "global.md").read_text()
    assert "release branch" in pj and "metric units" in g


def test_memory_capture_skips_trivial_turns(tmp_path, monkeypatch):
    from otto.memory import Memory, project_slug
    s = load_settings(); s.memory.auto_capture = True
    m = Memory(tmp_path / "memory", s)
    called = {"n": 0}
    async def boom(self, *a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(Memory, "_extract", boom)
    # a greeting/ack exchange → no extraction model call…
    r = asyncio.run(m.capture("/tmp/triv", "hi", "Hello! How can I help?"))
    assert called["n"] == 0 and r["summarized"] is False
    # …but the turn is still logged durably
    assert (tmp_path / "memory" / "projects" / project_slug("/tmp/triv") / "turns.jsonl").exists()
    # a substantive exchange DOES trigger extraction
    asyncio.run(m.capture("/tmp/triv", "how should we structure the billing service?",
                          "Use a ledger table and post double-entry rows."))
    assert called["n"] == 1


def test_memory_preamble_injected_once_then_on_change(tmp_path, monkeypatch):
    """The bounded preamble is injected on a chat's first turn and again only when the
    remembered content changes — not redundantly every turn (a token optimization)."""
    monkeypatch.setattr("otto.orchestrator.save_config", lambda d: None)
    from otto.workspace import Workspace

    class RecBrain(_FakeBrain):
        def __init__(self, **k):
            super().__init__(**k)
            self.seen = []
        async def send(self, text):
            self.seen.append(text)
            async for ev in super().send(text):
                yield ev

    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()
        proj = str(tmp_path / "proj"); (tmp_path / "proj").mkdir()
        ws = Workspace(orch, orch.settings, proj)
        ws.brain = RecBrain()
        orch.workspaces[proj] = ws
        orch.focused = proj
        await orch.memory.add_fact(proj, "We deploy from the release branch")
        await orch._run_turn(ws, "first question")
        await orch._run_turn(ws, "second question")
        assert "Orrin memory" in ws.brain.seen[0]        # injected on turn 1
        assert "Orrin memory" not in ws.brain.seen[1]     # not repeated (unchanged)
        assert ws.brain.seen[1] == "second question"      # user text sent verbatim
        await orch.memory.add_fact(proj, "The staging DB resets nightly")
        await orch._run_turn(ws, "third question")
        assert "Orrin memory" in ws.brain.seen[2]         # re-injected after a change
    asyncio.run(run())


def test_tts_kokoro_engine_config_and_fallback():
    """Kokoro is a selectable engine; if the model files/lib aren't available, the Mouth
    falls back to the always-present system voice instead of going mute."""
    from otto.voice.tts import Mouth, list_kokoro_voices
    voices = list_kokoro_voices()
    assert voices and all("name" in v for v in voices)
    s = load_settings()
    assert s.voice.tts.kokoro_voice == "af_heart"      # default from config
    s.voice.tts.engine = "kokoro"
    s.voice.tts.kokoro_model_dir = "/tmp/orrin-no-such-kokoro-dir"
    m = Mouth(s)
    m.start()                                          # model files absent → graceful fallback
    assert m._engine == "system" and m._kokoro is None
    # a system-voice job renders a `say` argv (no crash, no kokoro)
    argv, tmp = m._render_job("hello there")
    assert argv[0] == "/usr/bin/say" and tmp is None


def test_tts_config_accepts_kokoro():
    s = load_settings()
    s.voice.tts.engine = "kokoro"        # Literal allows it
    assert s.voice.tts.engine == "kokoro"


def test_hotkey_chord_parse_and_pane():
    """The push-to-talk chord parses to a modifier mask + key, and the Input Monitoring
    privacy pane is wired (the fix that lets PTT work in the packaged app)."""
    from otto.voice.hotkey_mac import parse_chord, _CMD, _SHIFT, MacHotkey
    mask, key = parse_chord("<cmd>+<shift>+j")
    assert key == "j" and (mask & _CMD) and (mask & _SHIFT)
    # constructing the monitor must not require AppKit (only start() touches it)
    s = load_settings()
    s.voice.hotkey.push_to_talk = "<cmd>+<shift>+k"
    hk = MacHotkey(s, loop=None, on_press=lambda: None, on_release=lambda: None)
    assert hk._key == "k"
    from otto.preflight import _PANES
    assert _PANES.get("input_monitoring") == "Privacy_ListenEvent"


def test_orchestrator_external_ptt_default_false():
    orch = Orchestrator(load_settings(), EventBus())
    assert orch._external_ptt is False      # menu bar flips this to own PTT on the main thread
