import pytest
from hubspot_mcp.common import StartupError
from hubspot_mcp.policy import load_policy


def bad(w, old, new):
    w.edit_policy(old, new)
    with pytest.raises(StartupError):
        load_policy(w.root / "policy.yaml")


@pytest.mark.parametrize("old,new", [
    ("version: 1", "version: 2"),
    ("version: 1", "version: 1\nextra: 1"),
    ("version: 1", "version: 1\nversion: 1"),
    ("writes_per_day: 20", "writes_per_day: 21"),
    ("writes_per_day: 20", "writes_per_day: '20'"),
    ("writes_per_day: 20", "writes_per_day: -1"),
    ("writes_enabled: true", "writes_enabled: 1"),
    ("min_confidence: 0.6", "min_confidence: 0.1"),
    ("min_confidence: 0.6", "min_confidence: .nan"),
    ("{A: 70, B: 40}", "{A: 40, B: 70}"),
    ("jobs_per_day: 30", "jobs_per_day: 31"),
    ("dollars_per_day: 3.0", "dollars_per_day: 3.5"),
    ("contact: [lead_score, lead_score_reason]", "contact: [lead_score, email]"),
    ("task: [hs_task_subject", "task: [hs_task_priority, hs_task_subject"),
    ("update_lead_score:    {allowed: true, approval: true", "update_lead_score:    {allowed: true, approval: false"),
    ("create_followup_task: {allowed: true, approval: true", "create_followup_task: {allowed: true, approval: false"),
    ("approval: false, per_day: 1}", "approval: false, per_day: 2}"),
    ("max_days: 30", "max_days: 31"),
    ("per_day: 200, max_days: 30}", "per_day: 200, max_days: 30, x: 1}"),
    ("America/Los_Angeles", "Mars/Base"),
    ("tools:", "tools:\n  delete_contact: {allowed: true}"),
])
def test_rejected(w, old, new):
    bad(w, old, new)


@pytest.mark.parametrize("text", ["version: 2026-13-45", "version: !!int x", "version: !!float abc", "version: !!bool zz",
                                  "[" * 5000, "? [a]\n: 1", "5", "", "version: &a [*a]", "version: !!int ''", "!!int '': 1", "version: !!float ''", "version: !!int '_'", "version: !!int '-'"])
def test_odd_yaml_is_a_clear_error(w, text):
    (w.root / "policy.yaml").write_text(text)
    with pytest.raises(StartupError, match="policy.yaml"):
        load_policy(w.root / "policy.yaml")


def test_missing_and_garbage(w):
    (w.root / "policy.yaml").write_text("- [")
    with pytest.raises(StartupError):
        load_policy(w.root / "policy.yaml")
    (w.root / "policy.yaml").unlink()
    with pytest.raises(StartupError):
        load_policy(w.root / "policy.yaml")


def test_tightening_and_absent_tools(w):
    w.edit_policy("  research_company:     {allowed: true, approval: false, per_day: 100}\n", "")
    w.edit_policy("get_company:          {allowed: true", "get_company:          {allowed: false")
    p, h = load_policy(w.root / "policy.yaml")
    assert p.tool("research_company") is None and p.tool("get_company") is None and p.tool("nope") is None
    assert "update_lead_score" in p.allowed_tools() and len(h) == 64
    assert w.boot().tools.keys() == set(p.allowed_tools())
    assert w.call("get_company", {"contact_id": "1"}) == ({"error": "blocked"}, True)


def test_tiers(w):
    p, _ = load_policy(w.root / "policy.yaml")
    assert [p.tier(x) for x in (100, 70, 69, 40, 39, 0)] == ["A", "A", "B", "B", "C", "C"]


def test_bad_policy_stops_startup(w):
    w.edit_policy("version: 1", "version: 9")
    with pytest.raises(StartupError):
        w.boot()
