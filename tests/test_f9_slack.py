import json
import httpx2
import pytest
from hubspot_mcp import slack as sl
from hubspot_mcp.common import StartupError, file_lock


def post(w):
    return w.call("post_slack_summary", {})


def slack_text(p):
    return json.dumps(p["blocks"])


def test_contents(scored):
    scored.approve("y")
    assert post(scored) == ({"status": "ok"}, False)
    p = scored.slack.posts[0]
    t = slack_text(p)
    assert p["text"] == "Daily lead summary" and p["unfurl_links"] is False
    assert "Acme 101: 82 (tier A)" in t and "120 stores" in t and "/contacts/123/record/0-1/101" in t
    assert all(b["text"]["type"] == "plain_text" for b in p["blocks"] if "text" in b)
    anchor = p["blocks"][-1]["elements"][0]["text"].split()[-1]
    assert scored.audit.verify(anchor).ok


def test_unapproved_text_never_appears(scored):
    scored.hs.add("102", message="EVILTEXT")
    scored.hs.companies["9102"]["name"] = "EVILCO"
    scored.call("search_new_leads", {"days": 7})
    scored.score("102", confidence=0.1)
    scored.task(subject="EVILSUBJECT")
    post(scored)
    t = slack_text(scored.slack.posts[0])
    assert "EVIL" not in t and "Acme" not in t and "Needs review: 1. Waiting for approval: 2." in t


def test_empty_day(w):
    post(w)
    assert "No leads today." in slack_text(w.slack.posts[0])


def test_top_five_order(w):
    for i in range(7):
        w.hs.add(str(300 + i))
    w.call("search_new_leads", {"days": 1})
    for i in range(7):
        assert w.score(str(300 + i), score=50 + i)[0]["status"] == "queued"
    w.approve(*["y"] * 7)
    post(w)
    lines = [b["text"]["text"] for b in w.slack.posts[0]["blocks"][2:7]]
    assert [x.split(":")[1].split()[0] for x in lines] == ["56", "55", "54", "53", "52"]


def test_escaping(scored):
    import sqlite3
    c = sqlite3.connect(scored.root / "state" / "store.sqlite3")
    c.execute("UPDATE proposals SET state='done', done_at=?, display=?", (scored.clock.epoch(), json.dumps(
        {"company": "<!channel> & <http://x|y> @here " + "z" * 4000, "tier": "A", "score": 1, "reasons": ["<b>"], "confidence": 1, "sources": []})))
    c.commit()
    c.close()
    post(scored)
    text = scored.slack.posts[0]["blocks"][2]["text"]["text"]
    assert "<" not in text and ">" not in text and "&lt;!channel&gt;" in text and len(text) <= 3000


def test_midnight_between_slot_and_content(scored, monkeypatch):
    scored.approve("y")
    real = type(scored.store).slack_begin
    def begin(self, *a):
        day = real(self, *a)
        scored.clock.advance(hours=20)
        return day
    monkeypatch.setattr(type(scored.store), "slack_begin", begin)
    assert not post(scored)[1]
    t = slack_text(scored.slack.posts[0])
    assert "Acme 101" in t and "Leads seen today: 1" in t


def test_escape_never_splits_an_entity():
    for n in range(20, 60):
        out = sl._esc("<&>" * 50, n)
        assert len(out) <= n
        assert not out.rstrip("…").replace("&lt;", "").replace("&amp;", "").replace("&gt;", "")


def test_one_post_a_day(w):
    post(w)
    w.boot()
    assert post(w)[0] == {"error": "limit"} and len(w.slack.posts) == 1
    w.clock.advance(days=1)
    assert not post(w)[1] and len(w.slack.posts) == 2


def test_policy_zero(w):
    w.edit_policy("approval: false, per_day: 1}", "approval: false, per_day: 0}")
    assert post(w)[0] == {"error": "limit"} and w.slack.posts == []


def test_4xx_can_be_retried_three_times(w):
    w.slack.script = [400, 403, 404]
    assert [post(w)[0]["error"] for _ in range(4)] == ["unavailable"] * 3 + ["limit"]
    w.clock.advance(days=1)
    w.slack.script = [400]
    assert post(w)[1] and not post(w)[1] and len(w.slack.posts) == 1


def test_429(w):
    w.slack.script = [httpx2.Response(429, headers={"retry-after": "5"})]
    assert not post(w)[1] and 5.0 in w.clock.slept
    w.clock.advance(days=1)
    w.slack.script = [httpx2.Response(429, headers={"retry-after": "60"})]
    assert post(w)[1] and w.store.slack_state() == "failed"


@pytest.mark.parametrize("b", ["timeout", "accept_then_timeout", 500, 302])
def test_ambiguous_is_never_reposted(w, b):
    w.slack.script = [b]
    assert post(w)[0] == {"error": "unavailable"} and w.store.slack_state() == "ambiguous"
    n = len(w.slack.posts)
    assert post(w)[0] == {"error": "limit"} and len(w.slack.posts) == n


def test_crash_after_send(w, monkeypatch):
    class Crash(BaseException):
        pass
    w.boot()
    monkeypatch.setattr(sl.SlackTool, "_send", lambda self, p: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        post(w)
    monkeypatch.undo()
    assert w.store.slack_state() == "reserved"
    w.boot()
    assert post(w)[0] == {"error": "limit"} and w.store.slack_state() == "ambiguous" and w.slack.posts == []


def test_other_process_in_flight_is_busy(w):
    g = w.boot()
    g.tools["post_slack_summary"].run.__self__.lock_wait = 0.05
    w.store.slack_begin(1)
    with file_lock(w.root / "state" / "slack.lock", 0):
        assert post(w)[0] == {"error": "busy"}
    assert w.store.slack_state() == "reserved"


def test_call_limit(w):
    out = [post(w)[0] for _ in range(11)]
    assert out[10] == {"error": "limit"} and w.log()[-1]["rule"] == "per_day"


@pytest.mark.parametrize("url", ["http://hooks.slack.com/services/x", "https://evil.com/services/x",
                                 "https://hooks.slack.com/services/x@evil.com", "https://hooks.slack.com.evil.com/services/"])
def test_webhook_host(w, url):
    p = (w.root / ".env.server").read_text().splitlines()
    w.env(".env.server", "\n".join(f"SLACK_WEBHOOK_URL={url}" if x.startswith("SLACK") else x for x in p))
    with pytest.raises(StartupError):
        w.boot()
