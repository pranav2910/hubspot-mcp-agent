import errno
import json
import multiprocessing
import os
import pytest
from conftest import CLAUDE_KEY, READ_KEY, WEBHOOK, WRITE_KEY
from hubspot_mcp.audit import MAX_RECORD, Audit, AuditError, TornLog
from hubspot_mcp.common import StartupError, canonical


@pytest.fixture
def log(w):
    a = w.audit
    a.append("start")
    for i in range(4):
        a.append("decision", call_id=str(i), tool="t", decision="deny", rule=str(i))
    return a


def lines(a):
    return a.path.read_bytes().splitlines(keepends=True)


def test_clean(log):
    r = log.verify()
    assert r.ok and r.records == 5 and log.verify(anchor=r.head).ok


@pytest.mark.parametrize("how", ["edit", "delete", "reorder"])
def test_tampering(log, how):
    L = lines(log)
    if how == "edit":
        L[2] = L[2].replace(b'"rule":"1"', b'"rule":"9"')
    elif how == "delete":
        del L[2]
    else:
        L[2], L[3] = L[3], L[2]
    log.path.write_bytes(b"".join(L))
    assert not log.verify().ok


def test_line_replaced_with_a_self_consistent_one(log):
    from hubspot_mcp.audit import _digest
    L = lines(log)
    rec = json.loads(L[2])
    rec.pop("hash")
    rec["rule"] = "forged"
    L[2] = (canonical({**rec, "hash": _digest(rec["prev_hash"], rec)}) + "\n").encode()
    log.path.write_bytes(b"".join(L))
    assert any("chain broken" in e for e in log.verify().errors)


@pytest.mark.parametrize("edit", [
    lambda b: b.replace(b'{"actor"', b'{"decision":"allow","rule":"approved by person","actor"', 1),
    lambda b: b.replace(b'":', b'": ', 1),
    lambda b: b.replace(b'"deny"', b'"\\u0064eny"', 1),
    lambda b: b[:-1] + b" \n",
])
@pytest.mark.parametrize("at", [2, -1])
def test_bytes_must_be_exact(log, edit, at):
    head = log.head()
    L = lines(log)
    assert edit(L[at]) != L[at]
    L[at] = edit(L[at])
    log.path.write_bytes(b"".join(L))
    assert not log.verify(anchor=head).ok
    if at == -1:
        with pytest.raises(AuditError):
            log.append("decision")


@pytest.mark.parametrize("change", [lambda r: r.pop("call_id"), lambda r: r.update(call_id=["x"]), lambda r: r.update(seq="4"),
                                    lambda r: r.update(seq=True), lambda r: r.update(extra=1), lambda r: r.update(rule=None)])
def test_self_consistent_but_wrong_shape(log, change):
    from hubspot_mcp.audit import _digest
    L = lines(log)
    rec = json.loads(L[-1])
    rec.pop("hash")
    change(rec)
    L[-1] = (canonical({**rec, "hash": _digest(rec["prev_hash"], rec)}) + "\n").encode()
    log.path.write_bytes(b"".join(L))
    assert not log.verify().ok
    with pytest.raises(TornLog):
        log.head()


@pytest.mark.parametrize("raw", [b"[" * 100000, b"5", b"[]", b"null", b"\xff\xfe", b'{"hash":1}'])
def test_verify_never_crashes(log, raw):
    with open(log.path, "ab") as f:
        f.write(raw + b"\n")
    assert not log.verify().ok


def test_torn_first_line_can_be_repaired(w):
    a = w.audit
    a.path.write_bytes(b'{"torn')
    assert a.repair() == 6
    assert a.verify().ok and [r["phase"] for r in w.log()] == ["start", "repair"]


def test_repair_touches_nothing_when_the_line_before_is_bad(log):
    L = lines(log)
    L[-1] = L[-1].replace(b'"rule":"3"', b'"rule":"8"')
    log.path.write_bytes(b"".join(L) + b"torn")
    before = log.path.read_bytes()
    with pytest.raises(TornLog):
        log.repair()
    assert log.path.read_bytes() == before


def test_any_append_failure_is_an_audit_error(log, monkeypatch):
    monkeypatch.setattr(Audit, "_append_locked", lambda *a, **k: (_ for _ in ()).throw(RecursionError()))
    with pytest.raises(AuditError):
        log.append("result")


def test_rewrite_and_truncation_need_the_anchor(log, w):
    head = log.head()
    log.path.write_bytes(b"".join(lines(log)[:3]))
    assert log.verify().ok and not log.verify(anchor=head).ok
    log.path.unlink()
    log.append("start")
    assert log.verify().ok and not log.verify(anchor=head).ok


def test_torn_line(log, w):
    with open(log.path, "ab") as f:
        f.write(b'{"half')
    with pytest.raises(TornLog):
        log.append("decision")
    with pytest.raises(StartupError):
        w.boot()
    assert log.verify().torn
    assert log.repair() == 6
    assert log.verify().ok and json.loads(lines(log)[-1])["phase"] == "repair"
    w.boot()


def test_repair_refuses_complete_but_edited_line(log):
    L = lines(log)
    L[-1] = L[-1].replace(b'"rule":"3"', b'"rule":"8"')
    log.path.write_bytes(b"".join(L))
    assert log.repair() == 0
    with pytest.raises(TornLog):
        log.append("decision")


def _spam(path, n):
    a = Audit(path, "p")
    for _ in range(n):
        a.append("decision", decision="deny")


def test_two_processes(w):
    w.audit.append("start")
    ctx = multiprocessing.get_context("spawn")
    ps = [ctx.Process(target=_spam, args=(w.root / "state", 25)) for _ in range(2)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    r = w.audit.verify()
    assert r.ok and r.records == 51


def test_disk_full(log, monkeypatch):
    before, real = log.path.read_bytes(), os.write

    def full(fd, data):
        real(fd, bytes(data)[:10])
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(os, "write", full)
    with pytest.raises(AuditError):
        log.append("decision")
    monkeypatch.undo()
    assert log.path.read_bytes() == before and log.verify().ok


def test_record_size_and_floats(log):
    with pytest.raises(AuditError):
        log.append("decision", rule="x" * MAX_RECORD)
    with pytest.raises(AuditError):
        log.append("decision", args={"a": 1.5})
    with pytest.raises(ValueError):
        canonical([{"a": 0.1}])
    assert log.verify().ok


def test_unfinished(log):
    log.append("decision", call_id="x", tool="t", decision="allow")
    assert len(log.verify().unfinished) == 1
    log.append("result", call_id="x", tool="t")
    assert log.verify().unfinished == []


def test_no_secrets_or_text_in_log(w):
    w.seen(message="reach me at joe@corp.com or 415-555-1234")
    w.score(reasons=["UNIQUEREASON text"])
    w.task(subject="UNIQUESUBJECT", body="UNIQUEBODY")
    w.call("search_new_leads", {"days": 14155551234})
    w.call("search_new_leads", {"days": 10 ** 5000})
    w.call("UNIQUETOOL 415-555-9999", {"UNIQUEKEY@x.com": 1})
    w.call("update_lead_score", {f"k{i}": i for i in range(900)})
    w.approve("y", "y")
    text = (w.root / "state" / "audit.jsonl").read_text()
    for s in ("joe@corp.com", "415-555", "UNIQUE", "example.com", READ_KEY, WRITE_KEY, CLAUDE_KEY, WEBHOOK, "14155551234"):
        assert s not in text, s
    assert all(len(x) <= MAX_RECORD for x in text.splitlines())
    assert w.audit.verify().ok


def test_key_file(w):
    a = w.audit
    assert (a.dir / "hmac.key").stat().st_mode & 0o777 == 0o600
    assert a.secret("x") == w.audit.secret("x") and a.secret("x") != a.secret("y")
    (a.dir / "hmac.key").write_bytes(b"short")
    with pytest.raises(StartupError):
        w.audit
