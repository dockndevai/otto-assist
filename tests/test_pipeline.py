"""Tests for the parts of Otto that don't need a live model:
config precedence, the event bus, tool/subagent construction, and the orchestrator's
turn-taking logic (ask_user routing, confirm parsing, canvas rendering).

The live brain path (Claude auth -> streamed reply) is exercised separately by running
the app; it needs a valid `claude` login and is not part of this offline suite.
"""

from __future__ import annotations

import asyncio

from otto.brain.connectors import (
    build_delegate_server,
    cli_agents_description,
    external_mcp_servers,
    run_cli,
)
from otto.brain.subagents import default_agents
from otto.brain.tools import build_server, qualified_names
from otto.config import (
    CliConnector,
    ConnectorsConfig,
    McpConnector,
    Settings,
    load_settings,
)
from otto.orchestrator import Orchestrator
from otto.server.app import create_app
from otto.server.events import EventBus


class FakeCtx:
    async def say(self, t): ...
    async def ask_user(self, q): return "x"
    def render_canvas(self, c, f="html"): ...
    def set_status(self, t): ...
    def notify(self, t, b=""): ...


def test_config_precedence(monkeypatch):
    monkeypatch.setenv("OTTO_BRAIN__MODEL", "opus")
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
    assert "mcp__otto__render_canvas" in names
    assert "mcp__otto__ask_user" in names
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


def test_external_mcp_servers_mapping():
    s = load_settings()
    s.connectors = ConnectorsConfig(
        mcp={
            "fs": McpConnector(transport="stdio", command="npx", args=["-y", "srv"]),
            "remote": McpConnector(transport="http", url="http://127.0.0.1:9000/mcp"),
            "broken": McpConnector(transport="stdio"),  # no command -> skipped
        }
    )
    out = external_mcp_servers(s)
    assert out["fs"] == {"type": "stdio", "command": "npx", "args": ["-y", "srv"]}
    assert out["remote"] == {"type": "http", "url": "http://127.0.0.1:9000/mcp"}
    assert "broken" not in out


def test_delegate_server_presence():
    s = load_settings()
    assert build_delegate_server(s) is None            # no CLI connectors -> no server
    s.connectors = ConnectorsConfig(
        cli={"echoer": CliConnector(command=["echo"], description="prints input")}
    )
    assert build_delegate_server(s) is not None
    assert '"echoer"' in cli_agents_description(s)


def test_run_cli_executes():
    async def run():
        conn = CliConnector(command=["echo"], timeout=10)
        out = await run_cli(conn, "hello otto", ".")
        assert "hello otto" in out and "[ok]" in out
        # {task} substitution
        conn2 = CliConnector(command=["echo", "pre", "{task}", "post"], timeout=10)
        out2 = await run_cli(conn2, "MID", ".")
        assert "pre MID post" in out2
        # missing binary is reported, not raised
        conn3 = CliConnector(command=["definitely_not_a_real_binary_xyz"], timeout=10)
        assert "command not found" in await run_cli(conn3, "x", ".")
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
