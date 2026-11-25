import json
import sqlite3
import threading
import httpx2
import pytest
from hubspot_mcp import approve as ap
from hubspot_mcp.audit import Audit, AuditError
from hubspot_mcp.common import file_lock


def sql(w, q, *a):
    c = sqlite3.connect(w.root / "state" / "store.sqlite3")
    c.execute(q, a)
    c.commit()
    c.close()


def intents(w):
    return [r for r in w.log() if r["phase"] == "intent"]


def check_invariants(w):
    """No write without a decision and an intent on record; at most one task per proposal."""
    log = w.log()
    sends = [x for x in w.hs.requests if x[0] == "PATCH" or (x[0] == "POST" and x[1].endswith("/tasks"))]
    assert len(w.hs.writes) <= len(sends)
    assert (not sends) or intents(w)
    decided = {r["call_id"] for r in log if r["phase"] == "decision" and r["decision"] == "approve"}
    assert all(i["call_id"] in decided for i in intents(w))
    bodies = [b["properties"]["hs_task_body"] for m, p, b in w.hs.writes if m == "POST"]
    assert len(bodies) == len(set(bodies))
    assert w.audit.verify().ok


def test_approve_score(scored):
    code, out = scored.approve("y")
    assert code == 0 and scored.state(scored.pid) == "done"
    assert scored.hs.writes == [("PATCH", "/crm/objects/2026-09/contacts/101",
                                 {"properties": {"lead_score": "82", "lead_score_reason": "[Tier A] 120 stores"}})]
    assert "Acme 101" in out and "en.wikipedia.org" in out and "0.9" in out
    assert [r["phase"] for r in scored.log()][-4:] == ["start", "decision", "intent", "result"]
    assert scored.log()[-1]["actor"] and scored.log()[-1]["actor"] != "model"
    check_invariants(scored)


def test_approve_task(tasked):
    tasked.approve("y")
    assert tasked.state(tasked.pid) == "done" and len(tasked.hs.tasks) == 1
    check_invariants(tasked)


def test_sends_exact_stored_bytes(scored):
    sent = []
    real = scored.hs.route
    scored.hs.route = lambda req, m, p: (sent.append(req.content) if m == "PATCH" else None, real(req, m, p))[1]
    scored.approve("y")
    assert sent == [scored.store.proposal(scored.pid).payload.encode()]


@pytest.mark.parametrize("answer,state", [("n", "rejected"), ("q", "pending"), ("", "pending"), ("yes", "pending"), ("yy", "pending")])
def test_no_is_no(scored, answer, state):
    scored.approve(answer)
    assert scored.state(scored.pid) == state and scored.hs.writes == []


def test_needs_a_terminal(scored):
    assert scored.approve("y", tty=False)[0] == 1 and scored.hs.writes == []


def test_kill_switch(scored):
    scored.edit_policy("writes_enabled: true", "writes_enabled: false")
    assert scored.approve("y")[0] == 1 and scored.hs.writes == []


@pytest.mark.parametrize("old,new,state", [
    ("writes_enabled: true", "writes_enabled: false", "pending"),
    ("update_lead_score:    {allowed: true", "update_lead_score:    {allowed: false", "pending"),
    ("contact: [lead_score, lead_score_reason]", "contact: [lead_score]", "stale"),
    ("writes_per_day: 20", "writes_per_day: 0", "pending"),
])
def test_policy_tightened_while_waiting(scored, old, new, state):
    real = ap.Approver.show
    def show(self, *a):
        scored.edit_policy(old, new)
        return real(self, *a)
    ap.Approver.show = show
    try:
        scored.approve("y")
    finally:
        ap.Approver.show = real
    assert scored.state(scored.pid) == state and scored.hs.writes == []
    assert scored.log()[-1]["phase"] == "result"


def test_policy_broken_while_waiting(scored):
    real = ap.Approver.show
    ap.Approver.show = lambda self, *a: (scored.root / "policy.yaml").write_text("x: [")
    good = (scored.root / "policy.yaml").read_text()
    try:
        assert scored.approve("y")[0] == 1
    finally:
        ap.Approver.show = real
    (scored.root / "policy.yaml").write_text(good)
    assert scored.hs.writes == [] and scored.state(scored.pid) == "approved"
    scored.approve("q")
    assert scored.state(scored.pid) == "pending"


@pytest.mark.parametrize("payload", [
    {"properties": {"lead_score": "99", "lead_score_reason": "x"}},
    {"properties": {"lead_score": "82", "lead_score_reason": "x", "email": "e"}},
])
def test_changed_payload(scored, payload):
    sql(scored, "UPDATE proposals SET payload=?", json.dumps(payload))
    scored.approve("y")
    assert scored.state(scored.pid) == "stale" and scored.hs.writes == []


@pytest.mark.parametrize("props", [{"email": "e"}, {"lead_score": "1", "lead_score_reason": "x", "phone": "5"}, {"lead_score": "1"}])
def test_forbidden_field_even_with_matching_hash(scored, props):
    from hubspot_mcp.proposals import payload_sha
    body = json.dumps({"properties": props})
    sql(scored, "UPDATE proposals SET payload=?, payload_sha=?", body, payload_sha("score", "101", body))
    scored.approve("y")
    assert scored.state(scored.pid) == "stale" and scored.hs.writes == []


def test_task_body_rules(tasked):
    from hubspot_mcp.proposals import payload_sha
    p = tasked.store.proposal(tasked.pid)
    for change in ("owner", "link", "marker", "field"):
        b = json.loads(p.payload)
        if change == "owner":
            b["properties"]["hubspot_owner_id"] = "1"
        elif change == "link":
            b["associations"][0]["to"]["id"] = "999"
        elif change == "marker":
            b["properties"]["hs_task_body"] = "x"
        else:
            b["properties"]["hs_task_priority"] = "HIGH"
        body = json.dumps(b)
        sql(tasked, "UPDATE proposals SET state='pending', payload=?, payload_sha=?", body, payload_sha("task", "101", body))
        tasked.approve("y")
        assert tasked.state(tasked.pid) == "stale" and tasked.hs.writes == [], change


def test_expired(scored):
    scored.clock.advance(hours=24)
    scored.approve("y")
    assert scored.state(scored.pid) == "expired" and scored.hs.writes == []


def test_expires_on_screen(scored):
    real = ap.Approver.show
    ap.Approver.show = lambda self, *a: scored.clock.advance(hours=24)
    try:
        scored.approve("y")
    finally:
        ap.Approver.show = real
    assert scored.state(scored.pid) == "expired" and scored.hs.writes == []


@pytest.mark.parametrize("field,value", [("lead_score", "10"), ("lead_score_reason", "someone else")])
def test_stale_score(scored, field, value):
    scored.hs.contacts["101"][field] = value
    scored.approve("y")
    assert scored.state(scored.pid) == "stale" and scored.hs.writes == []


def test_owner_changed(tasked):
    tasked.hs.contacts["101"]["hubspot_owner_id"] = "88"
    tasked.approve("y")
    assert tasked.state(tasked.pid) == "stale" and tasked.hs.writes == []


def test_contact_gone(scored):
    del scored.hs.contacts["101"]
    scored.approve("y")
    assert scored.state(scored.pid) == "stale"


def test_unreadable_contact_goes_back(scored):
    scored.hs.fail(httpx2.Response(500), "GET", "/contacts/101", times=6)
    scored.approve("y")
    assert scored.state(scored.pid) == "pending" and scored.hs.writes == []


def test_cap_at_execution(w):
    w.edit_policy("writes_per_day: 20", "writes_per_day: 1")
    w.hs.add("101"), w.hs.add("102")
    w.call("search_new_leads", {"days": 7})
    a, b = w.task("101")[0]["proposal_id"], w.task("102")[0]["proposal_id"]
    w.approve("y", "y")
    assert sorted([w.state(a), w.state(b)]) == ["done", "pending"] and len(w.hs.tasks) == 1


def test_second_approver_refused(scored):
    with file_lock(scored.root / "state" / "approve.lock", 0):
        code, out = scored.approve("y")
    assert code == 1 and "already running" in out and scored.hs.writes == []
    assert scored.approve("y")[0] == 0 and scored.state(scored.pid) == "done"


def test_second_approver_refused_mid_request(scored):
    started, finish, res = threading.Event(), threading.Event(), {}
    scored.hs.fail(lambda: (started.set(), finish.wait(5)), "PATCH")
    t = threading.Thread(target=lambda: res.update(a=scored.approve("y")))
    t.start()
    assert started.wait(5)
    assert scored.approve("y")[0] == 1
    finish.set()
    t.join()
    assert res["a"][0] == 0 and len(scored.hs.writes) == 1 and scored.state(scored.pid) == "done"


@pytest.mark.parametrize("code,state", [(400, "failed"), (401, "failed"), (403, "failed"), (404, "failed"),
                                        (409, "failed"), (422, "failed")])
def test_4xx(scored, code, state):
    scored.hs.fail(httpx2.Response(code), "PATCH")
    scored.approve("y")
    assert scored.state(scored.pid) == state and scored.hs.writes == []
    check_invariants(scored)


def test_429_then_ok(scored):
    scored.hs.fail(httpx2.Response(429, headers={"retry-after": "99"}), "PATCH")
    scored.approve("y")
    assert scored.state(scored.pid) == "done" and max(scored.clock.slept) == 10


@pytest.mark.parametrize("kind", ["scored", "tasked"])
def test_429_exhausted(kind, request):
    w = request.getfixturevalue(kind)
    m = "PATCH" if kind == "scored" else "POST"
    w.hs.fail(httpx2.Response(429), m, "contacts/101" if kind == "scored" else "/tasks", times=4)
    w.approve("y")
    assert w.state(w.pid) == "failed" and w.hs.writes == []


def test_429_then_person_edits(scored):
    def edit():
        scored.hs.contacts["101"]["lead_score"] = "5"
    scored.hs.faults = [("PATCH", "", httpx2.Response(429))]
    real = scored.clock.sleep
    scored.clock.sleep = lambda s: (edit(), real(s))
    scored.approve("y")
    assert scored.state(scored.pid) == "failed" and scored.hs.contacts["101"]["lead_score"] == "5"


@pytest.mark.parametrize("fault", [httpx2.Response(500), httpx2.Response(302), "timeout"])
def test_score_not_applied_is_retried_once(scored, fault):
    scored.hs.fail(fault, "PATCH")
    scored.approve("y")
    assert scored.state(scored.pid) == "done" and len(scored.hs.writes) == 1
    assert scored.store.proposal(scored.pid).retries == 1 and scored.store.count("writes") == 2
    check_invariants(scored)


@pytest.mark.parametrize("code", [400, 404, 429])
def test_refused_recovery_retry_is_held(scored, code):
    scored.hs.fail("timeout", "PATCH")
    scored.hs.fail(httpx2.Response(code), "PATCH", times=4)
    scored.approve("y")
    assert scored.state(scored.pid) == "ambiguous" and scored.hs.writes == []


def test_score_retry_happens_only_once_ever(scored):
    scored.hs.fail("timeout", "PATCH", times=9)
    for _ in range(3):
        scored.approve("y")
    sends = [r for r in scored.hs.requests if r[0] == "PATCH"]
    assert len(sends) == 2 and scored.state(scored.pid) == "ambiguous"
    check_invariants(scored)


def test_score_applied_then_timeout(scored):
    scored.hs.fail("accept_then_timeout", "PATCH")
    scored.approve("y")
    assert scored.state(scored.pid) == "done" and len(scored.hs.writes) == 1
    check_invariants(scored)


def test_score_changed_by_person_after_timeout_is_held(scored):
    scored.hs.fail("timeout", "PATCH")
    real = scored.hs.route
    def route(req, m, p):
        if any(r[0] == "PATCH" for r in scored.hs.requests):
            scored.hs.contacts["101"]["lead_score"] = "7"
        return real(req, m, p)
    scored.hs.route = route
    scored.approve("y")
    assert scored.state(scored.pid) == "ambiguous" and scored.hs.writes == []


def test_task_applied_then_timeout(tasked):
    tasked.hs.fail("accept_then_timeout", "POST", "/tasks")
    tasked.approve("y")
    assert tasked.state(tasked.pid) == "done" and len(tasked.hs.tasks) == 1
    check_invariants(tasked)


@pytest.mark.parametrize("answer,state", [("done", "done"), ("failed", "failed"), ("skip", "ambiguous")])
def test_task_unknown_is_held_never_resent(tasked, answer, state):
    tasked.hs.lag_tasks = True
    tasked.hs.fail("accept_then_timeout", "POST", "/tasks")
    tasked.approve("y", answer)
    assert tasked.state(tasked.pid) == state and len(tasked.hs.tasks) == 1
    tasked.approve("y")
    assert len(tasked.hs.tasks) == 1
    check_invariants(tasked)


def test_lag_clears_later(tasked):
    tasked.hs.lag_tasks = True
    tasked.hs.fail("accept_then_timeout", "POST", "/tasks")
    tasked.approve("y")
    tasked.hs.lag_tasks = False
    tasked.approve()
    assert tasked.state(tasked.pid) == "done" and len(tasked.hs.tasks) == 1


class Crash(BaseException):
    pass


POINTS = ["after_decision", "after_approved", "after_executing", "after_intent", "after_send", "after_result_state"]


@pytest.mark.parametrize("kind", ["scored", "tasked"])
@pytest.mark.parametrize("point", POINTS)
def test_crash_everywhere(kind, point, request, monkeypatch):
    w = request.getfixturevalue(kind)
    rec, move, start, send = Audit.append, ap.Store.move, ap.Store.start_write, ap.HubSpot.send

    def append(self, phase, **k):
        r = rec(self, phase, **k)
        if (point, phase) in (("after_decision", "decision"), ("after_intent", "intent")):
            raise Crash
        return r

    def mv(self, p, to):
        r = move(self, p, to)
        if (point == "after_approved" and to == "approved") or (point == "after_result_state" and to == "done"):
            raise Crash
        return r

    def st(self, *a):
        r = start(self, *a)
        if point == "after_executing":
            raise Crash
        return r

    def sd(self, *a):
        r = send(self, *a)
        if point == "after_send":
            raise Crash
        return r

    monkeypatch.setattr(Audit, "append", append)
    monkeypatch.setattr(ap.Store, "move", mv)
    monkeypatch.setattr(ap.Store, "start_write", st)
    monkeypatch.setattr(ap.HubSpot, "send", sd)
    with pytest.raises(Crash):
        w.approve("y")
    monkeypatch.undo()
    applied = len(w.hs.writes)
    w.approve("q")
    state = w.state(w.pid)
    if point in ("after_decision", "after_approved"):
        assert state == "pending" and applied == 0
    elif point in ("after_send", "after_result_state"):
        assert state == "done" and len(w.hs.writes) == 1
    elif kind == "scored":
        assert state == "done" and len(w.hs.writes) == 1      # never sent before, so retried once
    else:
        assert state == "ambiguous" and w.hs.writes == []     # held for the person
    assert state != "stale"
    check_invariants(w)


@pytest.mark.parametrize("phase,state,sent", [("decision", "pending", 0), ("intent", "pending", 0), ("result", "done", 1)])
def test_audit_failure_stops(scored, monkeypatch, phase, state, sent):
    scored.hs.add("102")
    scored.call("search_new_leads", {"days": 7})
    second = scored.task("102")[0]["proposal_id"]
    real = Audit.append
    def append(self, ph, **k):
        if ph == phase:
            raise AuditError("x")
        return real(self, ph, **k)
    monkeypatch.setattr(Audit, "append", append)
    sql(scored, "UPDATE proposals SET created_at=created_at+9 WHERE id=?", second)
    code, out = scored.approve("y", "y")
    monkeypatch.undo()
    assert code == 1 and scored.pid in out and len(scored.hs.writes) == sent
    assert scored.state(scored.pid) == state and scored.state(second) == "pending"


def test_start_audit_failure(scored, monkeypatch):
    monkeypatch.setattr(Audit, "append", lambda *a, **k: (_ for _ in ()).throw(AuditError("x")))
    assert scored.approve("y")[0] == 1 and scored.hs.writes == []


def test_intent_failure_on_recovery_retry_is_held(scored, monkeypatch):
    scored.hs.fail("timeout", "PATCH")
    real, n = Audit.append, []
    def append(self, ph, **k):
        if ph == "intent":
            n.append(1)
            if len(n) == 2:
                raise AuditError("x")
        return real(self, ph, **k)
    monkeypatch.setattr(Audit, "append", append)
    assert scored.approve("y")[0] == 1
    monkeypatch.undo()
    assert scored.state(scored.pid) == "ambiguous" and scored.hs.writes == []
    scored.approve()
    assert scored.state(scored.pid) == "ambiguous" and scored.hs.writes == []


def test_recovery_respects_kill_of_tool(scored):
    sql(scored, "UPDATE proposals SET state='executing'")
    scored.edit_policy("update_lead_score:    {allowed: true", "update_lead_score:    {allowed: false")
    code, _ = scored.approve()
    assert code == 0 and scored.log()[-1]["rule"] == "tool_not_allowed" and scored.log()[-1]["result"] == "ambiguous"
    assert scored.state(scored.pid) == "ambiguous" and scored.hs.writes == []


def test_screen_escapes_everything(scored):
    sql(scored, "UPDATE proposals SET display=?", json.dumps({
        "company": "Ac\x1b[2Jme‮", "tier": "A", "score": 82, "reasons": ["r"], "confidence": 0.9,
        "sources": [{"url": "https://x.com", "fact": "a​b"}]}))
    scored.hs.contacts["101"]["lead_score_reason"] = "was\x1b]0;x\x07"
    _, out = scored.approve("n")
    assert not any(c in out for c in "\x1b\x07‮​") and "\\u001b" in out and "\\u202e" in out


def test_screen_shows_every_hidden_code_point():
    from hubspot_mcp.common import visible
    for n in (0xE0145, 0xFE0F, 0x34F, 0x3164, 0x115F, 0x17B4, 0x2800, 0xA0, 0x3000, 0x301, 0x20DD, 0x1D159, 0xAD, 0x85):
        assert visible("a" + chr(n) + "b").startswith("a\\"), hex(n)
    assert visible("Plain, 東京 é $5 + 1%") == "Plain, 東京 é $5 + 1%"


def test_typed_ahead_answers_are_thrown_away():
    import os, pty, select, sys, time
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c",
            "import time; from hubspot_mcp.approve import terminal_ask as a;"
            "print('GOT1', a('one? '), flush=True); time.sleep(1); print('GOT2', a('two? '), flush=True)"])
    out = b""
    def until(text):
        nonlocal out
        end = time.time() + 15
        while text not in out and time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    break
        assert text in out, out
    until(b"one? ")
    os.write(fd, b"y\ny\n")
    until(b"GOT1 y")
    until(b"two? ")
    os.write(fd, b"n\n")
    until(b"GOT2 n")
    os.waitpid(pid, 0)
    os.close(fd)


def test_default_prompt_is_the_flushing_one():
    import inspect
    assert inspect.signature(ap.start).parameters["ask"].default is ap.terminal_ask


@pytest.mark.parametrize("payload", ["garbage", "[]", "{}", '{"properties": 5}', "[" * 100000, '{"properties":{"lead_score":82}}'])
@pytest.mark.parametrize("state,after", [("pending", "stale"), ("executing", "ambiguous")])
def test_garbage_payload_never_blocks_the_queue(scored, payload, state, after):
    scored.hs.add("102")
    scored.call("search_new_leads", {"days": 7})
    other = scored.task("102")[0]["proposal_id"]
    sql(scored, "UPDATE proposals SET payload=?, state=? WHERE id=?", payload, state, scored.pid)
    answers = ["y", "y"] if state == "pending" else ["y"]
    assert scored.approve(*answers)[0] == 0
    assert scored.state(scored.pid) == after and scored.state(other) == "done" and len(scored.hs.writes) == 1


def test_changed_payload_is_never_reconciled_to_done(scored):
    body = json.dumps({"properties": {"lead_score": "99", "lead_score_reason": "x"}})
    scored.hs.contacts["101"].update(lead_score="99", lead_score_reason="x")
    sql(scored, "UPDATE proposals SET payload=?, state='executing'", body)
    scored.approve()
    assert scored.state(scored.pid) == "ambiguous"


def _killed_child(root, kind, applied):
    import os, signal
    from pathlib import Path
    from conftest import World
    w = World(Path(root))
    w.hs.add("101")
    def die():
        os.kill(os.getpid(), signal.SIGKILL)
    real = w.hs.route
    def route(req, m, p):
        if m == "PATCH" or (m == "POST" and p.endswith("/tasks")):
            if applied:
                real(req, m, p)
                (Path(root) / "applied.json").write_text(json.dumps([w.hs.contacts, w.hs.tasks]))
            die()
        return real(req, m, p)
    w.hs.route = route
    w.approve("y")


@pytest.mark.parametrize("applied", [True, False])
@pytest.mark.parametrize("kind", ["scored", "tasked"])
def test_really_killed_mid_request(kind, applied, request):
    import multiprocessing
    w = request.getfixturevalue(kind)
    p = multiprocessing.get_context("spawn").Process(target=_killed_child, args=(str(w.root), kind, applied))
    p.start()
    p.join(30)
    assert p.exitcode == -9 and w.state(w.pid) == "executing"
    if applied:
        w.hs.contacts, w.hs.tasks = json.loads((w.root / "applied.json").read_text())
    assert w.approve()[0] == 0     # the lock was freed by the kill
    if applied:
        assert w.state(w.pid) == "done" and w.hs.writes == []
    elif kind == "scored":
        assert w.state(w.pid) == "done" and len(w.hs.writes) == 1
    else:
        assert w.state(w.pid) == "ambiguous" and w.hs.tasks == {}
    assert w.audit.verify().ok


@pytest.mark.parametrize("state", ["pending", "executing"])
def test_huge_crm_number_does_not_block_the_queue(scored, state):
    scored.hs.contacts["101"]["lead_score"] = "1e1000000"
    sql(scored, "UPDATE proposals SET state=?", state)
    scored.hs.add("102")
    scored.call("search_new_leads", {"days": 7})
    scored.clock.advance(seconds=1)  # strictly newer, so the order on screen is fixed
    other = scored.task("102")[0]["proposal_id"]
    code, out = scored.approve(*(["n", "y"] if state == "pending" else ["y"]))
    assert code == 0 and scored.state(other) == "done" and len(out) < 20000


def test_nested_json_from_hubspot_is_just_unavailable(scored):
    scored.hs.fail(httpx2.Response(200, text="[" * 200000), "GET", "/contacts/101", times=9)
    assert scored.approve("y")[0] == 0 and scored.state(scored.pid) == "pending" and scored.hs.writes == []


@pytest.mark.parametrize("raw", [b"HUBSPOT_WRITE_KEY=\xff\xfe\n", "HUBSPOT_WRITE_KEY=\u201cabc\u201d\n".encode(), b"HUBSPOT_WRITE_KEY='a\tb'\n"])
def test_damaged_env_file_is_a_clear_message(scored, raw):
    p = scored.root / ".env.approver"
    p.write_bytes(raw)
    code, out = scored.approve("y")
    assert code == 1 and ".env.approver" in out


def test_state_folder_error_is_a_message(scored, monkeypatch):
    def broken(*a):
        raise OSError(28, "full")
    monkeypatch.setattr(ap, "file_lock", broken)
    code, out = scored.approve("y")
    assert code == 1 and "STOPPED" in out


def test_right_to_left_is_escaped():
    from hubspot_mcp.common import visible
    assert visible("a\u05d0b\u0627") == "a\\u05d0b\\u0627"


def test_reviews_listed_and_dismissed(w):
    w.seen()
    w.score(confidence=0.1)
    _, out = w.approve("")
    assert "Needs review: contact 101, low_confidence" in out and len(w.store.reviews()) == 1
    w.approve("d")
    assert w.store.reviews() == [] and w.hs.writes == [] and w.log()[-1]["result"] == "dismissed"


def test_env_rules(scored):
    scored.env(".env.approver", "HUBSPOT_WRITE_KEY=x\n", mode=0o644)
    assert scored.approve("y")[0] == 1
    scored.env(".env.approver", "HUBSPOT_WRITE_KEY=x\nHUBSPOT_READ_KEY=y\n")
    assert scored.approve("y")[0] == 1 and scored.hs.writes == []


def test_lock_freed_after_crash(scored, monkeypatch):
    monkeypatch.setattr(ap.Approver, "recover", lambda self: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        scored.approve("y")
    monkeypatch.undo()
    assert scored.approve("y")[0] == 0
