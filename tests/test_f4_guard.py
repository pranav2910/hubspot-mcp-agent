import json
import os
import subprocess
import sys
import threading
import time
import anyio
import pytest
from mcp import Client
from conftest import PROJECT, READ_KEY
from hubspot_mcp import server
from hubspot_mcp.audit import Audit, AuditError
from hubspot_mcp.common import StartupError

pytestmark = pytest.mark.anyio


async def test_list_and_call_through_the_sdk(w):
    w.hs.add("101")
    async with Client(server.make_server(w.boot())) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert len(tools) == 6
        assert all(t.input_schema["additionalProperties"] is False for t in tools.values())
        r = await c.call_tool("search_new_leads", {"days": 3})
        assert not r.is_error and json.loads(r.content[0].text)["leads"][0]["contact_id"] == "101"
        for name, args in [("nope", {}), ("search_new_leads", {"days": "3"}), ("search_new_leads", {"days": 3, "x": 1}),
                           ("search_new_leads", {"days": True}), ("search_new_leads", {"days": 3.0}),
                           ("search_new_leads", None), ("post_slack_summary", {"text": "hi"})]:
            assert (await c.call_tool(name, args)).is_error, (name, args)
    decisions = [r for r in w.log() if r["phase"] == "decision"]
    assert len(decisions) == 8 and sum(d["decision"] == "deny" for d in decisions) == 7


@pytest.mark.parametrize("args", [
    {"contact_id": "1", "score": float("nan"), "reasons": ["a"], "confidence": 0.9, "source_ids": []},
    {"contact_id": "1", "score": 5, "reasons": ["a"], "confidence": float("inf"), "source_ids": []},
    {"contact_id": "1", "score": 5, "reasons": ["a"], "confidence": True, "source_ids": []},
    {"contact_id": "1\n", "score": 5, "reasons": ["a"], "confidence": 0.9, "source_ids": []},
    {"contact_id": 1, "score": 5, "reasons": ["a"], "confidence": 0.9, "source_ids": []},
    {"contact_id": "١", "score": 5, "reasons": ["a"], "confidence": 0.9, "source_ids": []},
    {"contact_id": "1", "score": 101, "reasons": ["a"], "confidence": 0.9, "source_ids": []},
    "text", [1], 5,
])
def test_strict_arguments(w, args):
    assert w.call("update_lead_score", args) == ({"error": "invalid"}, True)
    assert w.log()[-1]["decision"] == "deny"


def test_huge_and_deep(w):
    assert w.call("search_new_leads", {"days": "x" * 20000})[0] == {"error": "invalid"}
    assert w.log()[-1]["rule"] == "too_large"
    deep = []
    for _ in range(100000):
        deep = [deep]
    assert w.call("search_new_leads", {"days": deep})[0] == {"error": "invalid"}
    assert w.call("search_new_leads", {"days": 10 ** 5000})[0] == {"error": "invalid"}
    assert w.call("search_new_leads", {"days": "\ud800"})[0] == {"error": "invalid"}
    assert w.audit.verify().ok and len(w.log()) == 5


def test_policy_can_tighten_days(w):
    w.edit_policy("max_days: 30", "max_days: 2")
    assert w.call("search_new_leads", {"days": 3})[0] == {"error": "invalid"}


def test_tool_exception_is_masked(w, capsys):
    g = w.boot()
    def boom(args):
        raise RuntimeError(f"leak {READ_KEY}")
    g.tools["get_company"] = type(g.tools["get_company"])("get_company", "", g.tools["get_company"].model, boom, True)
    assert g.call("get_company", {"contact_id": "1"}) == ({"error": "unavailable"}, True)
    out = capsys.readouterr()
    assert out.out == "" and "RuntimeError" in out.err and READ_KEY not in out.err
    assert w.log()[-1]["phase"] == "result"


def test_per_day_limit_survives_restart(w):
    w.edit_policy("per_day: 200, max_days", "per_day: 2, max_days")
    for _ in range(2):
        assert not w.call("search_new_leads", {"days": 1})[1]
    w.boot()
    assert w.call("search_new_leads", {"days": 1})[0] == {"error": "limit"}


def test_decision_audit_failure_denies_and_releases(w, monkeypatch):
    w.boot()
    n = len(w.hs.requests)
    monkeypatch.setattr(Audit, "append", lambda *a, **k: (_ for _ in ()).throw(AuditError("x")))
    assert w.call("search_new_leads", {"days": 1}) == ({"error": "unavailable"}, True)
    assert len(w.hs.requests) == n and w.store.count("tool:search_new_leads") == 0


def test_result_audit_failure_halts(w, monkeypatch):
    w.boot()
    real = Audit.append
    def only_decisions(self, phase, **k):
        if phase == "result":
            raise AuditError("x")
        return real(self, phase, **k)
    monkeypatch.setattr(Audit, "append", only_decisions)
    assert w.call("search_new_leads", {"days": 1})[1]
    monkeypatch.undo()
    n = len(w.hs.requests)
    assert w.call("search_new_leads", {"days": 1}) == ({"error": "unavailable"}, True)
    assert len(w.hs.requests) == n and len(w.audit.verify().unfinished) == 1
    w.guard = None
    assert not w.call("search_new_leads", {"days": 1})[1]


async def test_cancelled_call_still_finishes_and_is_audited(w):
    started = threading.Event()
    w.hs.fail(lambda: (started.set(), time.sleep(0.6)), part="search")
    async with Client(server.make_server(w.boot())) as c:
        async with anyio.create_task_group() as tg:
            async def go():
                with anyio.CancelScope() as scope:
                    tg.start_soon(stop, scope)
                    await c.call_tool("search_new_leads", {"days": 1})
            async def stop(scope):
                await anyio.to_thread.run_sync(started.wait)
                scope.cancel()
            tg.start_soon(go)
    # leaving the client shuts the server down; it must have waited for the call
    assert [r["phase"] for r in w.log()] == ["start", "decision", "result"]


async def test_pool_full_is_busy_and_audited(w):
    gate, n = threading.Event(), server.POOL
    g = w.boot()
    tool = g.tools["get_company"]
    g.tools["get_company"] = type(tool)("get_company", "", tool.model, lambda a: (gate.wait(5), {"status": "ok"})[1], True)
    async with Client(server.make_server(g)) as c:
        async with anyio.create_task_group() as tg:
            for _ in range(n):
                tg.start_soon(c.call_tool, "get_company", {"contact_id": "1"})
            with anyio.fail_after(5):
                while sum(r["decision"] == "allow" for r in w.log()) < n:
                    await anyio.sleep(0.02)
            r = await c.call_tool("get_company", {"contact_id": "1"})
            assert r.is_error and json.loads(r.content[0].text) == {"error": "busy"}
            gate.set()
    assert any(r["rule"] == "pool_full" for r in w.log())


def test_unexpected_failure_before_the_decision_is_audited(w, monkeypatch):
    g = w.boot()
    monkeypatch.setattr(g.store, "reserve", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert g.call("search_new_leads", {"days": 1}) == ({"error": "unavailable"}, True)
    assert w.log()[-1]["rule"] == "internal"
    monkeypatch.setattr(g, "_raw", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    assert g.busy("x", {}) == ({"error": "busy"}, True)


def test_library_logs_stay_off(w, capsys):
    import logging
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("httpx2").setLevel(logging.INFO)
    logging.getLogger("httpx2").propagate = True
    w.boot()
    w.call("post_slack_summary", {})
    assert "hooks.slack.com" not in capsys.readouterr().err


def test_startup_checks(w):
    w.env(".env.server", "HUBSPOT_READ_KEY=x\n")
    with pytest.raises(StartupError):
        w.boot()
    w.env(".env.server", (w.root / ".env.server").read_text(), mode=0o644)
    with pytest.raises(StartupError):
        w.boot()


def test_server_refuses_the_write_key(w):
    p = w.root / ".env.server"
    w.env(".env.server", p.read_text() + "HUBSPOT_WRITE_KEY=x\n")
    with pytest.raises(StartupError, match="must not contain"):
        w.boot()
    w.env(".env.server", p.read_text().replace("HUBSPOT_WRITE_KEY=x\n", ""))
    w.boot()
    assert "HUBSPOT_READ_KEY" not in os.environ and "HUBSPOT_WRITE_KEY" not in os.environ


async def test_real_stdio_process(w):
    from mcp import StdioServerParameters
    env = {**os.environ, "HUBSPOT_MCP_HOME": str(w.root)}
    async with Client(StdioServerParameters(command=sys.executable, args=["-m", "hubspot_mcp.server"], env=env)) as c:
        assert len((await c.list_tools()).tools) == 6
        assert (await c.call_tool("nope", {"a": 1})).is_error
    assert w.log()[-1]["rule"] == "unknown_tool"


def test_startup_failure_keeps_stdout_clean(w):
    (w.root / "policy.yaml").write_text("bad: [")
    r = subprocess.run([sys.executable, "-m", "hubspot_mcp.server"], capture_output=True, text=True, cwd=PROJECT,
                       env={**os.environ, "HUBSPOT_MCP_HOME": str(w.root)})
    assert r.returncode == 1 and r.stdout == "" and "policy.yaml" in r.stderr
