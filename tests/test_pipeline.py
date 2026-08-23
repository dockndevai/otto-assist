"""Tests for the parts of Jarvis that don't need a live model:
config precedence, the event bus, tool/subagent construction, and the orchestrator's
turn-taking logic (ask_user routing, confirm parsing, canvas rendering).

The live brain path (Claude auth -> streamed reply) is exercised separately by running
the app; it needs a valid `claude` login and is not part of this offline suite.
"""

from __future__ import annotations

import asyncio

from jarvis.brain.subagents import default_agents
from jarvis.brain.tools import build_server, qualified_names
from jarvis.config import load_settings
from jarvis.orchestrator import Orchestrator
from jarvis.server.app import create_app
from jarvis.server.events import EventBus


class FakeCtx:
    async def say(self, t): ...
    async def ask_user(self, q): return "x"
    def render_canvas(self, c, f="html"): ...
    def set_status(self, t): ...
    def notify(self, t, b=""): ...


def test_config_precedence(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN__MODEL", "opus")
    s = load_settings()
    assert s.brain.model == "opus"          # env overrides yaml
    assert s.voice.stt.wake_words == "hey jarvis"  # from yaml
    assert s.server.port == 8000            # yaml/default


def test_event_bus_fanout():
    bus = EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.status("idle", "ok")
    assert q1.get_nowait()["type"] == "status"
    assert q2.get_nowait()["state"] == "idle"
    bus.unsubscribe(q1)
    bus.canvas("<b>x</b>")
    assert q2.get_nowait()["type"] == "canvas_render"
    assert q2.empty()  # q1 no longer receives


def test_tools_and_subagents_build():
    server = build_server(FakeCtx())
    assert server is not None
    names = qualified_names()
    assert "mcp__jarvis__render_canvas" in names
    assert "mcp__jarvis__ask_user" in names
    agents = default_agents()
    assert set(agents) == {"researcher", "coder"}
    assert "WebSearch" in agents["researcher"].tools


def test_app_routes():
    s = load_settings()
    bus = EventBus()
    app = create_app(Orchestrator(s, bus), bus)
    paths = {r.path for r in app.routes}
    assert "/" in paths and "/ws" in paths


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
        orch.submit_user_input("b")            # routed as the answer, not a new turn
        assert await asyncio.wait_for(task, 2) == "b"
    asyncio.run(run())


def test_confirm_parsing():
    async def run():
        orch = Orchestrator(load_settings(), EventBus())
        orch._loop = asyncio.get_running_loop()

        async def answer(text, question="do a thing"):
            t = asyncio.create_task(orch.confirm(question))
            await asyncio.sleep(0.05)
            orch.submit_user_input(text)
            return await asyncio.wait_for(t, 2)

        assert await answer("yes") is True
        assert await answer("go ahead") is True
        assert await answer("no thanks") is False
        assert await answer("stop") is False
    asyncio.run(run())
