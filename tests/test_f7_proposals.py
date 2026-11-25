import json
import pytest
from hubspot_mcp.proposals import number


def test_score_payload(scored):
    p = scored.store.proposal(scored.pid)
    assert json.loads(p.payload) == {"properties": {"lead_score": "82", "lead_score_reason": "[Tier A] 120 stores"}}
    assert p.display["tier"] == "A" and p.display["confidence"] == 0.9 and p.display["sources"][0]["url"].startswith("https")
    assert p.expires_at - p.created_at == 86400 and scored.hs.writes == []


def test_task_payload(tasked):
    p = tasked.store.proposal(tasked.pid)
    b = json.loads(p.payload)
    assert b["properties"]["hs_timestamp"] == "2026-12-02T01:00:00Z" and b["properties"]["hubspot_owner_id"] == "77"
    assert b["properties"]["hs_task_body"].endswith(f"[mcp:{p.id}]")
    assert b["associations"] == [{"to": {"id": "101"}, "types": [
        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}]


@pytest.mark.parametrize("kw,ok", [
    ({"score": 0}, True), ({"score": 100}, True), ({"score": -1}, False), ({"score": 101}, False), ({"score": 50.0}, False),
    ({"reasons": ["a" * 200]}, True), ({"reasons": ["a" * 201]}, False), ({"reasons": ["a"] * 3}, True),
    ({"reasons": ["a"] * 4}, False), ({"reasons": []}, False), ({"reasons": [" "]}, False),
    ({"confidence": 1}, True), ({"confidence": 0.6}, True), ({"confidence": 1.01}, False), ({"confidence": -0.1}, False),
])
def test_score_limits(w, kw, ok):
    w.seen()
    out, err = w.score(**kw)
    assert (not err and out["status"] == "queued") if ok else out == {"error": "invalid"}


BAD_TEXT = ["a\x00b", "a\x1b[2Jb", "a\x7fb", "a​b", "a‮b", "a⁦b", "a﻿b", "ab", "a b",
            "a b", "a͸b", "é", "<b>x</b>", "a > b", "x &amp; y", "x &#60; y", "see https://evil.test",
            "WWW.evil.test", "done [mcp:0011223344556677]", "done [MCP:00]", "\ud800",
            "a\U000e0145b", "a\ufe0fb", "a\u034fb", "a\u3164b", "a\u115fb", "a\u17b4b", "a\u2800b", "a\u00a0b", "a\u3000b",
            "a\u20ddb", "\u0301a", "z\u0334\u0335\u0336b", "wide   gap", "&lt", "&#60 x", "&ltscript", "a\U0001d159b"]


@pytest.mark.parametrize("bad", BAD_TEXT + ["two\nlines", "tab\there"])
def test_text_rule(w, bad):
    w.seen()
    assert w.score(reasons=[bad], source_ids=[])[0] == {"error": "invalid"}
    assert w.task(subject=bad)[0] == {"error": "invalid"}
    if bad != "two\nlines":
        assert w.task(body=bad)[0] == {"error": "invalid"}
    assert w.store.in_state("pending") == []


def test_body_newline_and_ordinary_text(w):
    w.seen()
    assert w.task(body="Line one\nCafé & Co, R&D, AT&T 50% हिन्दी 東京")[0]["status"] == "queued"


@pytest.mark.parametrize("kw,ok", [
    ({"subject": "a" * 100}, True), ({"subject": "a" * 101}, False), ({"body": "a" * 500}, True), ({"body": "a" * 501}, False),
    ({"due_date": "2026-11-24"}, False), ({"due_date": "2026-11-25"}, True), ({"due_date": "2027-01-23"}, True),
    ({"due_date": "2027-01-24"}, False), ({"due_date": "2026-02-30"}, False), ({"due_date": "2026-1-5"}, False),
    ({"owner": "9"}, False), ({"hubspot_owner_id": "9"}, False),
])
def test_task_limits(w, kw, ok):
    w.seen()
    out, err = w.task(**kw)
    assert (not err) is ok, out


def test_task_expires_when_due(w):
    w.seen()
    pid = w.task(due_date="2026-11-25")[0]["proposal_id"]
    p = w.store.proposal(pid)
    assert p.expires_at == p.created_at + 86400  # due 17:00 Pacific is later than 24 h from 10:00
    w.clock.advance(hours=9)                     # 19:00 Pacific
    w.hs.add("102")
    w.call("search_new_leads", {"days": 7})
    p = w.store.proposal(w.task("102", due_date="2026-11-25")[0]["proposal_id"])
    assert p.expires_at - p.created_at == 22 * 3600


def test_sources(w):
    w.seen("101")
    w.hs.add("102")
    w.call("search_new_leads", {"days": 7})
    mine, other = w.sources("101"), w.sources("102")
    for ids in (other, ["src_000000000000"], mine + other, mine + mine, [], ["x"], mine * 6):
        assert w.score(source_ids=ids)[0] == {"error": "invalid"}, ids
    assert w.score(source_ids=mine)[0]["status"] == "queued"


def test_expired_sources(w):
    w.seen()
    ids = w.sources()
    w.clock.advance(days=7, seconds=1)
    w.call("search_new_leads", {"days": 7})
    assert w.score(source_ids=ids)[0] == {"error": "invalid"}


def test_company_changed_after_research(w):
    w.seen()
    ids = w.sources()
    w.hs.companies["9101"]["domain"] = "other.com"
    assert w.score(source_ids=ids)[0] == {"error": "invalid"}
    w.hs.companies["9101"]["domain"] = "acme101.com"
    w.hs.links["101"] = ["9102"]
    w.hs.companies["9102"] = dict(w.hs.companies["9101"])
    assert w.score(source_ids=ids)[0] == {"error": "invalid"}


def test_needs_review(w):
    w.seen()
    assert w.score(confidence=0.59)[0] == {"status": "needs_review", "reason": "low_confidence"}
    w.hs.links["101"] = []
    assert w.score(source_ids=[])[0] == {"status": "needs_review", "reason": "company_data_missing"}
    assert w.store.in_state("pending") == [] and len(w.store.reviews()) == 1


def test_open_proposal(scored):
    assert scored.score()[0] == {"error": "open_proposal"}
    assert scored.score(confidence=0.1)[0] == {"error": "open_proposal"}
    assert scored.task()[0]["status"] == "queued"


def test_unseen_gone_no_owner(w):
    w.hs.add("55")
    assert w.task("55")[0] == {"error": "blocked"}
    w.seen("101", hubspot_owner_id=None)
    assert w.task()[0] == {"error": "blocked"}
    del w.hs.contacts["101"]
    assert w.task()[0] == {"error": "blocked"}


def test_flood(w):
    w.seen()
    out = [w.task(due_date="2020-01-01")[0] for _ in range(61)]
    assert out[59] == {"error": "invalid"} and out[60] == {"error": "limit"}


def test_number():
    assert number("82") == number("82.0") == number(" 82.00 ") == "82" and number("") is None and number(None) is None
    assert number("100") == number("1E2") == "100" and number("x") == "?x"
    for big in ("1e1000000", "1e999999", "-1e-999999", "9" * 5000, "1." + "0" * 5000, "NaN", "Infinity", "sNaN"):
        assert number(big) == "?" + big
