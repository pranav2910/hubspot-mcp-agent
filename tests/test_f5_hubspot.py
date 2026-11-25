import httpx2
import pytest
from hubspot_mcp.common import clean, mask, valid_domain


@pytest.mark.parametrize("n,truncated", [(0, False), (1, False), (50, False), (51, True)])
def test_search_sizes(w, n, truncated):
    for i in range(n):
        w.hs.add(str(1000 + i))
    out, err = w.call("search_new_leads", {"days": 30})
    assert not err and len(out["leads"]) == min(n, 50) and out["truncated"] is truncated and out["untrusted"] is True


def test_search_masks_and_hides(w):
    w.hs.add("1", message="Mail joe@corp.com, +1 (415) 555-1234.\x1b[31m‮​ " + "x" * 900, phone="555")
    lead = w.call("search_new_leads", {"days": 1})[0]["leads"][0]
    assert set(lead) == {"contact_id", "job_title", "message"}
    m = lead["message"]
    assert len(m) == 500 and "joe" not in m and "555" not in m and "\x1b" not in m and "‮" not in m


def test_company(w):
    w.seen("1")
    out, err = w.call("get_company", {"contact_id": "1"})
    assert not err and out["data_complete"] and out["domain"] == "acme1.com" and out["employees"] == 4000


@pytest.mark.parametrize("links", [[], ["91", "92"]])
def test_company_must_be_exactly_one(w, links):
    w.seen("1")
    w.hs.links["1"] = links
    assert w.call("get_company", {"contact_id": "1"})[0]["status"] == "missing"


def test_more_pages_means_not_one(w):
    w.seen("1")
    w.hs.fail(httpx2.Response(200, json={"results": [{"toObjectId": 91}], "paging": {"next": {"after": "1"}}}),
              part="/associations/")
    assert w.call("get_company", {"contact_id": "1"})[0]["status"] == "missing"


@pytest.mark.parametrize("field,value", [("domain", "10.0.0.1"), ("domain", "a.com/x"), ("domain", "u:p@a.com"),
    ("domain", "a.com:80"), ("domain", "localhost"), ("domain", None), ("name", ""), ("industry", None),
    ("numberofemployees", "lots"), ("numberofemployees", "0"), ("numberofemployees", None)])
def test_incomplete(w, field, value):
    w.seen("1")
    w.hs.companies["91"][field] = value
    out = w.call("get_company", {"contact_id": "1"})[0]
    assert out["status"] == "ok" and out["data_complete"] is False


def test_unseen_and_old(w):
    w.hs.add("1")
    assert w.call("get_company", {"contact_id": "1"})[0] == {"error": "blocked"}
    w.seen("2")
    w.clock.advance(hours=25)
    assert w.call("get_company", {"contact_id": "2"})[0] == {"error": "blocked"}


def test_retries(w):
    w.hs.add("1")
    w.hs.fail(httpx2.Response(429, headers={"retry-after": "2"}), part="search")
    w.hs.fail(httpx2.Response(503), part="search")
    assert not w.call("search_new_leads", {"days": 1})[1]
    assert 2.0 in w.clock.slept


@pytest.mark.parametrize("fault", [httpx2.Response(500), httpx2.Response(429), "timeout"])
def test_gives_up(w, fault):
    w.boot()
    w.hs.fail(fault, times=3)
    assert w.call("search_new_leads", {"days": 1})[0] == {"error": "unavailable"}
    assert len(w.hs.requests) == 3


def test_long_retry_after_is_not_waited(w):
    w.boot()
    w.hs.fail(httpx2.Response(429, headers={"retry-after": "3600"}))
    assert w.call("search_new_leads", {"days": 1})[1] and sum(w.clock.slept) < 20


@pytest.mark.parametrize("r", [httpx2.Response(200, text="<html>"), httpx2.Response(200, json=[]),
                               httpx2.Response(200, json={"total": 1}), httpx2.Response(401), httpx2.Response(302),
                               httpx2.Response(200, text="[" * 200000)])
def test_bad_answers(w, r):
    w.boot()
    w.hs.fail(r)
    assert w.call("search_new_leads", {"days": 1})[0] == {"error": "unavailable"} and len(w.hs.requests) == 1


def test_huge_messages_do_not_stall(w):
    import time
    for i in range(50):
        w.hs.add(str(1000 + i), message="x" * 65536)
    w.hs.add("2000", message="a@" * 30000)
    w.boot()
    start = time.monotonic()
    assert not w.call("search_new_leads", {"days": 1})[1]
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("text", ["joe\u200b@corp.com", "joe\uff20corp.com", "j\u00adoe@corp.com", "joe@co\u2060rp.com",
                                  "415\u2013555\u20131234", "\uff14\uff11\uff15 555 1234", "(415)\u200b555-1234", "+44 20 7946 0958"])
def test_masking_is_not_fooled(text):
    out = clean("call " + text + " now", 200)
    assert out in ("call [email] now", "call [phone] now"), out


def test_email_cut_by_the_window_is_not_leaked():
    long = "x" * 60 + "@" + "d" * 60 + ".com "          # masking shrinks these, pulling the cut into view
    for pad in range(0, 130):
        out = clean(long * 18 + "y" * pad + " secret.person@corp.com tail" + " z" * 3000, 500)
        assert "secret" not in out or "secret.person@" not in out, pad


def test_year_ranges_and_counts_survive():
    assert clean("Grew from 2019-2024 to 440,000 staff", 100) == "Grew from 2019-2024 to 440,000 staff"


def test_read_budget_is_enforced(w):
    w.boot()
    import httpx2 as h
    def handle(req):
        w.hs.requests.append((req.method, req.url.path))
        limits.append(req.extensions["timeout"]["read"])
        w.clock.advance(seconds=limits[-1])
        raise h.ReadTimeout("t", request=req)
    w.guard.tools["search_new_leads"].run.__self__.hs._http = h.Client(base_url="https://api.hubapi.com", transport=h.MockTransport(handle))
    limits, t = [], w.clock.mono
    assert w.call("search_new_leads", {"days": 1})[1]
    assert w.clock.mono - t <= 20 and limits[0] == 10 and 0 < limits[1] < 10


def test_no_attempt_starts_without_time_left(w):
    from hubspot_mcp.hubspot import HubSpot, Unavailable
    n = []
    def handle(req):
        n.append(1)
        w.clock.advance(seconds=19.2 if len(n) == 1 else 0)
        return httpx2.Response(503)
    hs = HubSpot("k", httpx2.MockTransport(handle), w.clock)
    with pytest.raises(Unavailable):
        hs.contact("1", ["x"])
    assert len(n) == 1


def test_superscript_ids_are_refused(w):
    w.seen("1")
    w.hs.fail(httpx2.Response(200, json={"results": [{"toObjectId": "\u00b2"}]}), part="/associations/")
    assert w.call("get_company", {"contact_id": "1"})[0] == {"error": "unavailable"}


def test_text_helpers():
    assert mask("a@b.co x 415.555.1234") == "[email] x [phone]"
    assert clean("a\u0000b\n c", 10) == "a b c" and clean(None, 5) == ""
    assert valid_domain("Bücher.de") == "xn--bcher-kva.de" and valid_domain("a..b") is None
