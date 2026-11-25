"""F10. Setup check: reads only, prints pass/fail, changes nothing."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .common import ROOT, Clock, StartupError, load_env, valid_domain
from .hubspot import VERSION, HubSpot, Unavailable
from .policy import load_policy

FIELDS = {"lead_score": ("number", "number"), "lead_score_reason": ("string", "textarea")}


def read_leads(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(root: Path, hs_transport: Any = None, clock: Clock | None = None, say: Callable[[str], None] = print) -> bool:
    clock = clock or Clock()
    ok = True

    def report(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        say(f"{'PASS' if good else 'FAIL'}  {name}{': ' + detail if detail else ''}")

    try:
        env = load_env(root / ".env.server", ["HUBSPOT_READ_KEY", "ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL",
                                              "HUBSPOT_PORTAL_ID"], forbidden=["HUBSPOT_WRITE_KEY"])
        report(".env.server", True)
    except StartupError as e:
        report(".env.server", False, str(e))
        return False
    try:
        load_env(root / ".env.approver", ["HUBSPOT_WRITE_KEY"], forbidden=["HUBSPOT_READ_KEY", "ANTHROPIC_API_KEY"])
        report(".env.approver", True)
    except StartupError as e:
        report(".env.approver", False, str(e))
    try:
        load_policy(root / "policy.yaml")
        report("policy.yaml", True)
    except StartupError as e:
        report("policy.yaml", False, str(e))
    portal = env["HUBSPOT_PORTAL_ID"]
    report("portal ID is digits", portal.isascii() and portal.isdigit())

    hs = HubSpot(env["HUBSPOT_READ_KEY"], hs_transport, clock)
    try:
        labels = [x.label.strip().lower() for x in hs.contact_properties()]
        for name, (type_, field_type) in FIELDS.items():
            d = hs.contact_property(name)
            good = d is not None and (d.type, d.fieldType) == (type_, field_type) and not d.hubspotDefined
            report(f"field {name}", good, "" if good else f"expected {type_}/{field_type}, "
                   + ("missing" if d is None else f"found {d.type}/{d.fieldType}"))

            if d is not None:
                twins = labels.count(d.label.strip().lower())
                report(f"no other field is labelled like {name}", twins <= 1, f"{twins} share the label" if twins > 1 else "")

        leads = read_leads(root / "data" / "leads.csv")
        wanted = {"Email", "Company Domain Name"}
        if not leads or not all(wanted <= set(r) and r["Email"] and r["Company Domain Name"] for r in leads):
            report("data/leads.csv", False, "missing rows or columns")
            return False
        found = hs.search_contacts({
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "IN",
                                           "values": [r["Email"].lower() for r in leads]}]}],
            "properties": ["email", "hubspot_owner_id"], "limit": 200})
        by_email = {(o.properties.get("email") or "").lower(): o for o in found.results}
        report("contacts imported", len(by_email) == len(leads), f"{len(by_email)} of {len(leads)}")
        bad_owner, bad_company = [], []
        for r in leads:
            o = by_email.get(r["Email"].lower())
            if o is None:
                continue
            if not o.properties.get("hubspot_owner_id"):
                bad_owner.append(o.id)
            ids = hs.company_ids(o.id)
            company = hs.company(ids[0]) if len(ids) == 1 and ids[0].isdigit() else None
            if company is None or valid_domain(company.get("domain")) != valid_domain(r["Company Domain Name"]):
                bad_company.append(o.id)
            clock.sleep(0.15)
        report("every contact has an owner", not bad_owner, f"missing on {len(bad_owner)}" if bad_owner else "")
        report("every contact has one company with the expected domain", not bad_company,
               f"wrong on {len(bad_company)}" if bad_company else "")
    except (OSError, UnicodeError, csv.Error) as e:
        report("data/leads.csv", False, type(e).__name__)
    except Unavailable as e:
        report("HubSpot read", False, str(e))
    return ok


def key_separation(root: Path, hs_transport: Any = None, say: Callable[[str], None] = print) -> bool:
    """Tries one write with the READ key on a contact that doesn't exist. Run only with the owner's OK."""
    env = load_env(root / ".env.server", ["HUBSPOT_READ_KEY"], forbidden=["HUBSPOT_WRITE_KEY"])
    r = HubSpot(env["HUBSPOT_READ_KEY"], hs_transport).send(
        "PATCH", f"/crm/objects/{VERSION}/contacts/99999999999999999", json.dumps({"properties": {"lead_score": "0"}}))
    text = "" if r is None else r.text.lower()
    good = r is not None and r.status_code == 403 and ("missing_scopes" in text or "missingscopes" in text
                                                       or "requiredgranularscopes" in text)
    say(f"{'PASS' if good else 'FAIL'}  read key cannot write (HTTP {r.status_code if r is not None else 'none'})")
    return good


def main() -> int:
    p = argparse.ArgumentParser(prog="hubspot_mcp.check_setup")
    p.add_argument("--key-separation", action="store_true", help="attempt one write with the read key; expects 403")
    a = p.parse_args()
    try:
        return 0 if (key_separation(ROOT) if a.key_separation else run(ROOT)) else 1
    except StartupError as e:
        print(f"FAIL  {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
