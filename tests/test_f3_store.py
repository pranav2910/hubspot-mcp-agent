import multiprocessing
import sqlite3
from zoneinfo import ZoneInfo
import pytest
from hubspot_mcp.common import StartupError
from hubspot_mcp.store import TRANSITIONS, Conflict, LimitReached, OpenProposal, Store, StoreError

STATES = ["pending", "approved", "executing", "ambiguous", "done", "failed", "rejected", "expired", "stale"]


def add(s, id="p1", kind="score", cid="1", ttl=3600):
    s.add_proposal(id=id, kind=kind, contact_id=cid, payload="{}", payload_sha="x", snapshot={}, display={},
                   expires_at=s.clock.epoch() + ttl)
    return s.proposal(id)


def force(s, id, state):
    c = sqlite3.connect(s.path)
    c.execute("UPDATE proposals SET state=? WHERE id=?", (state, id))
    c.commit()
    c.close()
    return s.proposal(id)


def test_caps_restart_and_clock(w):
    for _ in range(3):
        w.store.reserve("k", 3)
    with pytest.raises(LimitReached):
        w.store.reserve("k", 3)
    w.clock.advance(days=-2)
    with pytest.raises(LimitReached):
        w.store.reserve("k", 3)
    w.clock.advance(days=3)
    day = w.store.reserve("k", 3)
    w.store.release(day, "k")
    assert w.store.count("k") == 0


def _open_now(path, gate, q):
    gate.wait()
    try:
        Store(path, ZoneInfo("UTC"))
        q.put("ok")
    except Exception as e:
        q.put(repr(e))


def test_many_processes_open_a_fresh_store_together(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    for round in range(3):
        gate, q = ctx.Barrier(8), ctx.Queue()   # everyone imports first, then all open at once
        ps = [ctx.Process(target=_open_now, args=(tmp_path / str(round), gate, q)) for _ in range(8)]
        [p.start() for p in ps]
        assert [q.get(timeout=30) for _ in ps] == ["ok"] * 8
        [p.join() for p in ps]


def _take(path, q):
    s, n = Store(path, ZoneInfo("UTC")), 0
    for _ in range(40):
        try:
            s.reserve("k", 50)
            n += 1
        except LimitReached:
            pass
    q.put(n)


def test_two_process_cap(w):
    w.store
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    ps = [ctx.Process(target=_take, args=(w.root / "state", q)) for _ in range(2)]
    [p.start() for p in ps]
    total = q.get() + q.get()
    [p.join() for p in ps]
    assert total == 50


@pytest.mark.parametrize("a", STATES)
@pytest.mark.parametrize("b", STATES)
def test_every_transition(w, a, b):
    s = w.store
    add(s)
    p = force(s, "p1", a)
    if b in TRANSITIONS.get(a, set()):
        assert s.move(p, b).state == b
    else:
        with pytest.raises(Conflict):
            s.move(p, b)


def test_cas_and_one_retry(w):
    s = w.store
    p = add(s)
    s.move(p, "approved")
    with pytest.raises(Conflict):
        s.move(p, "rejected")
    p = force(s, "p1", "ambiguous")
    p = s.start_write(p, 20)
    assert p.retries == 1 and p.state == "executing"
    p = s.move(p, "ambiguous")
    with pytest.raises(Conflict):
        s.start_write(p, 20)
    assert s.count("writes") == 1  # the refused retry rolled its count back


def test_tasks_never_retry(w):
    s = w.store
    add(s, kind="task")
    with pytest.raises(Conflict):
        s.start_write(force(s, "p1", "ambiguous"), 20)


def test_done_at(w):
    s = w.store
    add(s)
    assert s.move(force(s, "p1", "executing"), "done").done_at == w.clock.epoch()
    day = s.today()
    assert [p.id for p in s.done_on(day)] == ["p1"]
    w.clock.advance(days=1)
    assert s.done_on(s.today()) == [] and len(s.done_on(day)) == 1


@pytest.mark.parametrize("state", ["pending", "approved", "executing", "ambiguous"])
def test_one_open(w, state):
    s = w.store
    add(s)
    force(s, "p1", state)
    with pytest.raises(OpenProposal):
        add(s, id="p2")
    add(s, id="p3", kind="task")
    with pytest.raises(OpenProposal):
        s.add_review("1", "x")


def test_expiry_frees_the_slot(w):
    s = w.store
    add(s, ttl=10)
    w.clock.advance(seconds=10)
    add(s, id="p2")
    assert s.proposal("p1").state == "expired"


def test_review_cleared_only_by_score(w):
    s = w.store
    s.add_review("1", "low")
    s.add_review("1", "low again")
    assert len(s.reviews()) == 1
    add(s, kind="task", id="t")
    assert len(s.reviews()) == 1
    add(s)
    assert s.reviews() == []


def test_busy_is_denied(w):
    s = Store(w.root / "state", ZoneInfo("UTC"), w.clock, busy_ms=50)
    c = sqlite3.connect(s.path, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(StoreError):
        s.reserve("k", 5)
    c.close()


def test_newer_and_corrupt(w):
    s = w.store
    c = sqlite3.connect(s.path)
    c.execute("PRAGMA user_version=99")
    c.close()
    with pytest.raises(StartupError):
        w.store
    for f in (w.root / "state").glob("store.sqlite3*"):
        f.unlink()
    s.path.write_bytes(b"not a database" * 100)
    with pytest.raises(StartupError):
        w.store


def test_newer_schema_while_running_is_denied_and_audited(w):
    w.boot()
    c = sqlite3.connect(w.root / "state" / "store.sqlite3")
    c.execute("PRAGMA user_version=99")
    c.close()
    assert w.call("search_new_leads", {"days": 1}) == ({"error": "unavailable"}, True)
    assert w.log()[-1]["decision"] == "deny" and w.hs.requests == []


def test_seen_ttl(w):
    s = w.store
    s.mark_seen(["5"])
    assert s.is_seen("5") and not s.is_seen("6")
    w.clock.advance(hours=24, seconds=1)
    assert not s.is_seen("5")


def test_no_personal_columns(w):
    w.store
    c = sqlite3.connect(w.root / "state" / "store.sqlite3")
    sql = " ".join(r[0] or "" for r in c.execute("SELECT sql FROM sqlite_master"))
    assert not any(x in sql.lower() for x in ("email", "phone", "message"))
