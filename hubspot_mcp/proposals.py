"""F7. Proposals: the model's only way to ask for a CRM change."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from decimal import Decimal
from typing import Any

from .common import Clock, Denied, canonical
from .hubspot import TASK_TO_CONTACT, Company, HubSpot, ReadTools, Unavailable
from .policy import Policy
from .store import OpenProposal, Store

TTL = 24 * 3600
SNAPSHOT_FIELDS = ["lead_score", "lead_score_reason", "hubspot_owner_id"]


def number(value: Any) -> str | None:
    """HubSpot returns numbers as text; compare them by value."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if len(text) > 40:
        return f"?{text}"
    try:
        d = Decimal(text)
        if not d.is_finite() or abs(d.adjusted()) > 30:
            return f"?{text}"
        return format(d.normalize(), "f")
    except ArithmeticError:
        return f"?{text}"


def snapshot(props: dict[str, str | None]) -> dict[str, Any]:
    return {"score": number(props.get("lead_score")), "reason": props.get("lead_score_reason") or "",
            "owner": props.get("hubspot_owner_id") or ""}


def payload_sha(kind: str, contact_id: str, payload: str) -> str:
    return hashlib.sha256(f"{kind}\n{contact_id}\n{payload}".encode()).hexdigest()


def marker(proposal_id: str) -> str:
    return f"[mcp:{proposal_id}]"


class ProposalTools:
    def __init__(self, reads: ReadTools, hs: HubSpot, store: Store, policy: Policy, clock: Clock | None = None):
        self.reads, self.hs, self.store, self.policy = reads, hs, store, policy
        self.clock = clock or Clock()

    def _contact(self, contact_id: str) -> dict[str, Any]:
        try:
            props = self.hs.contact(contact_id, SNAPSHOT_FIELDS)
        except Unavailable:
            raise Denied("unavailable", "hubspot") from None
        if props is None:
            raise Denied("blocked", "contact_gone")
        return snapshot(props)

    def _add(self, proposal_id: str, kind: str, contact_id: str, body: dict[str, Any], snap: dict[str, Any],
             display: dict[str, Any], expires: int) -> dict[str, Any]:
        payload = canonical(body)
        try:
            self.store.add_proposal(id=proposal_id, kind=kind, contact_id=contact_id, payload=payload,
                                    payload_sha=payload_sha(kind, contact_id, payload), snapshot=snap,
                                    display=display, expires_at=expires)
        except OpenProposal:
            raise Denied("open_proposal") from None
        return {"status": "queued", "proposal_id": proposal_id,
                "note": "A person must approve this. Nothing has changed in HubSpot."}

    def _review(self, contact_id: str, reason: str) -> dict[str, Any]:
        try:
            self.store.add_review(contact_id, reason)
        except OpenProposal:
            raise Denied("open_proposal") from None
        return {"status": "needs_review", "reason": reason}

    def update_lead_score(self, args) -> dict[str, Any]:
        company: Company | None = self.reads.company(args.contact_id)
        snap = self._contact(args.contact_id)
        if company is None or not company.complete:
            return self._review(args.contact_id, "company_data_missing")
        if args.confidence < self.policy.min_confidence:
            return self._review(args.contact_id, "low_confidence")
        found = self.store.sources(list(args.source_ids), company.company_id, company.sha)
        if not args.source_ids or len(found) != len(args.source_ids):
            raise Denied("invalid", "sources")
        tier = self.policy.tier(args.score)
        display = {"company": company.name, "tier": tier, "score": args.score, "reasons": list(args.reasons),
                   "confidence": args.confidence, "sources": [{"url": f["url"], "fact": f["fact"]} for f in found]}
        body = {"properties": {"lead_score": str(args.score),
                               "lead_score_reason": f"[Tier {tier}] " + "; ".join(args.reasons)}}
        return self._add(secrets.token_hex(8), "score", args.contact_id, body, snap, display,
                         self.clock.epoch() + TTL)

    def create_followup_task(self, args) -> dict[str, Any]:
        rules = self.policy.tool("create_followup_task")
        today = self.clock.now().astimezone(self.policy.tz).date()
        due_day = dt.date.fromisoformat(args.due_date)
        if not 1 <= (due_day - today).days <= rules.max_due_days:
            raise Denied("invalid", "due_date")
        company = self.reads.company(args.contact_id)
        snap = self._contact(args.contact_id)
        if not snap["owner"]:
            raise Denied("blocked", "no_owner")
        due = dt.datetime.combine(due_day, dt.time(17), self.policy.tz).astimezone(dt.timezone.utc)
        proposal_id = secrets.token_hex(8)
        body = {
            "properties": {
                "hs_task_subject": args.subject,
                "hs_task_body": f"{args.body}\n\n{marker(proposal_id)}",
                "hs_timestamp": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "hs_task_status": "NOT_STARTED",
                "hubspot_owner_id": snap["owner"],
            },
            "associations": [{"to": {"id": args.contact_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": TASK_TO_CONTACT}]}],
        }
        display = {"company": company.name if company else "", "subject": args.subject, "due": args.due_date,
                   "sources": []}
        return self._add(proposal_id, "task", args.contact_id, body, snap, display,
                         min(self.clock.epoch() + TTL, int(due.timestamp())))
