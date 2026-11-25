import shutil
import httpx2
import pytest
from conftest import PROJECT, READ_KEY, WRITE_KEY, CLAUDE_KEY
from hubspot_mcp import check_setup


@pytest.fixture
def good(w):
    (w.root / "data").mkdir()
    shutil.copy(PROJECT / "data" / "leads.csv", w.root / "data" / "leads.csv")
    for i, r in enumerate(check_setup.read_leads(w.root / "data" / "leads.csv")):
        w.hs.add(str(100 + i), email=r["Email"])
        w.hs.companies["9" + str(100 + i)]["domain"] = r["Company Domain Name"]
    w.hs.props = {"lead_score": {"name": "lead_score", "label": "Lead score", "type": "number", "fieldType": "number"},
                  "lead_score_reason": {"name": "lead_score_reason", "label": "Lead score reason", "type": "string", "fieldType": "textarea"}}
    return w


def run(w):
    out = []
    return check_setup.run(w.root, w.hs.transport, w.clock, out.append), "\n".join(out)


def test_good(good):
    ok, out = run(good)
    assert ok, out
    assert good.hs.writes == [] and {m for m, _ in good.hs.requests} <= {"GET", "POST"}
    assert not any(k in out for k in (READ_KEY, WRITE_KEY, CLAUDE_KEY))


@pytest.mark.parametrize("break_it,expect", [
    (lambda w: w.hs.props.pop("lead_score"), "FAIL  field lead_score"),
    (lambda w: w.hs.props["lead_score_reason"].update(fieldType="text"), "FAIL  field lead_score_reason"),
    (lambda w: w.hs.props["lead_score"].update(hubspotDefined=True), "FAIL  field lead_score"),
    (lambda w: w.hs.contacts.pop("100"), "FAIL  contacts imported"),
    (lambda w: w.hs.contacts["101"].update(hubspot_owner_id=None), "FAIL  every contact has an owner"),
    (lambda w: w.hs.links.update({"102": []}), "FAIL  every contact has one company"),
    (lambda w: w.hs.links.update({"102": ["9102", "9103"]}), "FAIL  every contact has one company"),
    (lambda w: w.hs.companies["9103"].update(domain="example.com"), "FAIL  every contact has one company"),
    (lambda w: (w.root / ".env.approver").unlink(), "FAIL  .env.approver"),
    (lambda w: (w.root / ".env.approver").chmod(0o644), "FAIL  .env.approver"),
    (lambda w: w.edit_policy("version: 1", "version: 3"), "FAIL  policy.yaml"),
    (lambda w: w.hs.props.update(x={"name": "x", "label": "LEAD SCORE", "type": "string", "fieldType": "text"}), "FAIL  no other field"),
    (lambda w: (w.root / "data" / "leads.csv").unlink(), "FAIL  data/leads.csv"),
    (lambda w: (w.root / "data" / "leads.csv").write_text("a,b\n1,2\n"), "FAIL  data/leads.csv"),
    (lambda w: (w.root / "data" / "leads.csv").write_bytes(b"\xff\xfe\x00"), "FAIL  data/leads.csv"),
    (lambda w: w.hs.fail(httpx2.Response(401)), "FAIL  HubSpot read"),
])
def test_bad(good, break_it, expect):
    break_it(good)
    ok, out = run(good)
    assert not ok and expect in out


def test_key_separation(w):
    out = []
    assert check_setup.key_separation(w.root, w.hs.transport, out.append)
    for r in (httpx2.Response(404), httpx2.Response(200, json={}), httpx2.Response(403, json={"category": "OTHER"}), "timeout"):
        w.hs.fail(r, "PATCH")
        assert not check_setup.key_separation(w.root, w.hs.transport, out.append)


def test_csv():
    rows = check_setup.read_leads(PROJECT / "data" / "leads.csv")
    assert len(rows) == 30 == len({r["Email"] for r in rows}) == len({r["Company Domain Name"] for r in rows})
    assert all(r["Email"].endswith("@example.com") for r in rows)
    assert sum(any(x in r["Message"].lower() for x in ("ignore your", "system:", "note to ai")) for r in rows) == 3


def test_gitignore_covers_secrets():
    t = (PROJECT / ".gitignore").read_text()
    assert ".env*" in t and "state/" in t
