from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from hubspot_mcp import approve, server
from hubspot_mcp.audit import Audit
from hubspot_mcp.common import Clock
from hubspot_mcp.policy import load_policy
from hubspot_mcp.store import Store

PROJECT = Path(__file__).resolve().parent.parent
WEBHOOK = "https://hooks.slack.com/services/T0/B0/secretsecret"
READ_KEY, WRITE_KEY, CLAUDE_KEY = "read-key-SECRET", "write-key-SECRET", "claude-key-SECRET"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClock(Clock):
    def __init__(self):
        self.t = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
        self.mono, self.slept = 0.0, []

    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        self.slept.append(s)
        self.mono += s
        self.t += dt.timedelta(seconds=s)

    def advance(self, **kw):
        d = dt.timedelta(**kw)
        self.t += d
        self.mono += d.total_seconds()


class FakeHubSpot:
    """Just enough of the API. `faults` is a list of (predicate, behavior) applied once each."""

    def __init__(self):
        self.contacts, self.companies, self.links, self.tasks = {}, {}, {}, {}
        self.requests, self.writes, self.faults, self.props = [], [], [], {}
        self.lag_tasks = False
        self.transport = httpx2.MockTransport(self.handle)

    def add(self, cid, company=True, **props):
        self.contacts[cid] = {"jobtitle": "VP Operations", "message": "hello", "lead_score": None,
                              "lead_score_reason": None, "hubspot_owner_id": "77", "email": f"l{cid}@example.com", **props}
        if company:
            co = "9" + cid
            self.companies[co] = {"name": f"Acme {cid}", "domain": f"acme{cid}.com", "industry": "RETAIL",
                                  "numberofemployees": "4000", "city": "Austin", "country": "US"}
            self.links[cid] = [co]
        return cid

    def fail(self, behavior, method=None, part="", times=1):
        for _ in range(times):
            self.faults.append((method, part, behavior))

    def handle(self, req):
        path, method = req.url.path, req.method
        self.requests.append((method, path))
        for f in list(self.faults):
            if (f[0] in (None, method)) and f[1] in path:
                self.faults.remove(f)
                b = f[2]
                if b == "timeout":
                    raise httpx2.ReadTimeout("t", request=req)
                if b == "accept_then_timeout":
                    self.route(req, method, path)
                    raise httpx2.ReadTimeout("t", request=req)
                if callable(b):
                    b()
                    continue
                return b
        return self.route(req, method, path)

    def route(self, req, method, path):
        j = lambda body, code=200: httpx2.Response(code, json=body)
        parts = path.strip("/").split("/")
        body = json.loads(req.content) if req.content else {}
        if path.endswith("/contacts/search"):
            rows = [{"id": i, "properties": {k: p.get(k) for k in ("jobtitle", "message", "email", "hubspot_owner_id")}}
                    for i, p in self.contacts.items()]
            return j({"total": len(rows), "results": rows[:body["limit"]]})
        if path.endswith("/tasks/batch/read"):
            return j({"status": "COMPLETE", "results": [
                {"id": x["id"], "properties": {"hs_task_body": self.tasks[x["id"]]["properties"]["hs_task_body"]}}
                for x in body["inputs"]]})
        if "/associations/" in path:
            cid, kind = parts[4], parts[6]
            if kind == "companies":
                return j({"results": [{"toObjectId": int(c)} for c in self.links.get(cid, [])]})
            ids = [] if self.lag_tasks else [t for t, v in self.tasks.items()
                                             if v["associations"][0]["to"]["id"] == cid]
            return j({"results": [{"toObjectId": int(t)} for t in ids]})
        if parts[1] == "properties":
            if len(parts) == 4:
                return j({"results": list(self.props.values())})
            return j(self.props[parts[4]]) if parts[4] in self.props else j({}, 404)
        if method == "PATCH":
            if req.headers["authorization"] != f"Bearer {WRITE_KEY}":
                return j({"category": "MISSING_SCOPES"}, 403)
            cid = parts[4]
            if cid not in self.contacts:
                return j({}, 404)
            self.writes.append((method, path, body))
            self.contacts[cid].update(body["properties"])
            return j({"id": cid, "properties": {}})
        if method == "POST" and path.endswith("/tasks"):
            if req.headers["authorization"] != f"Bearer {WRITE_KEY}":
                return j({"category": "MISSING_SCOPES"}, 403)
            self.writes.append((method, path, body))
            tid = str(5000 + len(self.tasks))
            self.tasks[tid] = body
            return j({"id": tid, "properties": {}}, 201)
        table = self.contacts if parts[3] == "contacts" else self.companies
        return j({"id": parts[4], "properties": table[parts[4]]}) if parts[4] in table else j({}, 404)


class FakeSlack:
    def __init__(self):
        self.posts, self.script = [], []
        self.transport = httpx2.MockTransport(self.handle)

    def handle(self, req):
        b = self.script.pop(0) if self.script else 200
        if b == "timeout":
            raise httpx2.ReadTimeout("t", request=req)
        if b == "accept_then_timeout":
            self.posts.append(json.loads(req.content))
            raise httpx2.ReadTimeout("t", request=req)
        if isinstance(b, httpx2.Response):
            return b
        if b == 200:
            self.posts.append(json.loads(req.content))
        return httpx2.Response(b, text="ok")


def cite(text, url="https://en.wikipedia.org/wiki/Acme", title="Acme"):
    return {"type": "text", "text": text, "citations": [
        {"type": "web_search_result_location", "url": url, "title": title, "cited_text": "x", "encrypted_index": "e"}]}


def message(content=None, stop="end_turn", model="claude-opus-5-5", searches=1, iterations="auto", **usage):
    it = [{"type": "message", "model": model, "input_tokens": 1000, "output_tokens": 200,
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}] if iterations == "auto" else iterations
    return BetaMessage.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": model, "stop_reason": stop, "stop_sequence": None,
        "content": content if content is not None else [cite("Acme runs 120 stores."), {"type": "text", "text": "Uncited."}],
        "usage": {"input_tokens": 1000, "output_tokens": 200, "iterations": it,
                  "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": 0}, **usage}})


class FakeClaude:
    def __init__(self):
        self.calls, self.script = [], []

    def __call__(self, **kw):
        self.calls.append(kw)
        r = self.script.pop(0) if self.script else message()
        if isinstance(r, Exception):
            raise r
        return r() if callable(r) else r


class World:
    """One project directory with fakes around it."""

    def __init__(self, root: Path):
        self.root = root
        shutil.copy(PROJECT / "policy.yaml", root / "policy.yaml")
        self.env(".env.server", f"HUBSPOT_READ_KEY={READ_KEY}\nANTHROPIC_API_KEY={CLAUDE_KEY}\n"
                                f"SLACK_WEBHOOK_URL={WEBHOOK}\nHUBSPOT_PORTAL_ID=123\n")
        self.env(".env.approver", f"HUBSPOT_WRITE_KEY={WRITE_KEY}\n")
        self.clock, self.hs, self.slack, self.claude = FakeClock(), FakeHubSpot(), FakeSlack(), FakeClaude()
        self.guard = None

    def env(self, name, text, mode=0o600):
        p = self.root / name
        p.write_text(text)
        p.chmod(mode)

    def edit_policy(self, old, new):
        p = self.root / "policy.yaml"
        assert old in p.read_text()
        p.write_text(p.read_text().replace(old, new))

    def boot(self):
        self.guard = server.build(self.root, hs_transport=self.hs.transport, slack_transport=self.slack.transport,
                                  create=self.claude, clock=self.clock)
        return self.guard

    def call(self, name, args=None):
        return (self.guard or self.boot()).call(name, {} if args is None else args)

    @property
    def store(self):
        return Store(self.root / "state", load_policy(self.root / "policy.yaml")[0].tz, self.clock)

    @property
    def audit(self):
        return Audit(self.root / "state", "test", clock=self.clock)

    def log(self):
        p = self.root / "state" / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def seen(self, cid="101", **props):
        self.hs.add(cid, **props)
        assert self.call("search_new_leads", {"days": 7})[1] is False
        return cid

    def sources(self, cid="101"):
        payload, err = self.call("research_company", {"contact_id": cid})
        assert not err, payload
        return [f["source_id"] for f in payload["facts"]]

    def score(self, cid="101", **kw):
        args = {"contact_id": cid, "score": 82, "reasons": ["120 stores"], "confidence": 0.9, **kw}
        if "source_ids" not in args:
            args["source_ids"] = self.sources(cid)
        return self.call("update_lead_score", args)

    def task(self, cid="101", **kw):
        return self.call("create_followup_task", {"contact_id": cid, "subject": "Call them", "body": "Ask about sites",
                                                  "due_date": "2026-12-01", **kw})

    def approve(self, *answers, tty=True, default=""):
        out, left = [], list(answers)

        def ask(prompt):
            out.append(prompt)
            return left.pop(0) if left else default

        code = approve.start(self.root, hs_transport=self.hs.transport, clock=self.clock, ask=ask,
                             say=out.append, is_tty=lambda: tty)
        return code, "\n".join(out)

    def state(self, pid):
        return self.store.proposal(pid).state


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


@pytest.fixture
def scored(w):
    """A queued score proposal."""
    w.seen()
    payload, err = w.score()
    assert not err, payload
    w.pid = payload["proposal_id"]
    return w


@pytest.fixture
def tasked(w):
    w.seen()
    payload, err = w.task()
    assert not err, payload
    w.pid = payload["proposal_id"]
    return w
