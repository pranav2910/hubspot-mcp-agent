"""F9. Slack summary: server-built, at most one post a day."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2

from .audit import Audit, AuditError
from .common import Busy, Clock, Denied, StartupError, file_lock
from .hubspot import retry_after
from .policy import Policy
from .store import Conflict, LimitReached, Store, StoreError

PREFIX = "https://hooks.slack.com/services/"
TIMEOUT = 10.0
MAX_WAIT = 20.0


def _esc(text: str, limit: int) -> str:
    """Escape, then cut between characters so an entity is never split."""
    out, size = [], 0
    for c in text:
        piece = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(c, c)
        if size + len(piece) > limit - 1:
            return "".join(out) + "…"
        out.append(piece)
        size += len(piece)
    return "".join(out)


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "plain_text", "text": _esc(text, 2900), "emoji": False}}


class SlackTool:
    def __init__(self, store: Store, policy: Policy, audit: Audit, state: Path, webhook: str, portal_id: str,
                 transport: httpx2.BaseTransport | None = None, clock: Clock | None = None, lock_wait: float = 1.0):
        if not webhook.startswith(PREFIX) or not webhook.isascii() or any(c in webhook for c in "@?# \t\r\n"):
            raise StartupError("SLACK_WEBHOOK_URL must start with " + PREFIX)
        if not (portal_id.isascii() and portal_id.isdigit()):
            raise StartupError("HUBSPOT_PORTAL_ID must be digits")
        self.store, self.policy, self.audit = store, policy, audit
        self.lock = state / "slack.lock"
        self.webhook, self.portal_id = webhook, portal_id
        self.clock, self.lock_wait = clock or Clock(), lock_wait
        self._http = httpx2.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False, transport=transport)

    def build(self, day: str) -> dict[str, Any]:
        """Content for the same day whose slot was taken, even if midnight passes meanwhile."""
        seen = self.store.seen_on(day)
        scored = sorted(self.store.done_on(day), key=lambda p: (-p.display["score"], p.id))
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "Daily lead summary", "emoji": False}},
            _section(f"Leads seen today: {seen}" if seen else "No leads today."),
        ]
        for n, p in enumerate(scored[:5], 1):
            d = p.display
            blocks.append(_section(
                f"{n}. {d['company']}: {d['score']} (tier {d['tier']})\n" + "\n".join(f"- {r}" for r in d["reasons"])
                + f"\nhttps://app.hubspot.com/contacts/{self.portal_id}/record/0-1/{p.contact_id}"))
        blocks.append(_section(f"Needs review: {len(self.store.reviews())}. "
                               f"Waiting for approval: {len(self.store.in_state('pending'))}."))
        blocks.append({"type": "context", "elements": [
            {"type": "plain_text", "text": f"Audit anchor {self.audit.head()}", "emoji": False}]})
        return {"text": "Daily lead summary", "unfurl_links": False, "unfurl_media": False, "blocks": blocks}

    def post_slack_summary(self, args) -> dict[str, Any]:
        rules = self.policy.tool("post_slack_summary")
        try:
            with file_lock(self.lock, self.lock_wait):
                day = self.store.slack_begin(rules.per_day)
                try:
                    payload = self.build(day)
                except (StoreError, AuditError):
                    self.store.slack_end(day, "failed")  # nothing was sent
                    raise Denied("unavailable", "build") from None
                outcome = self._send(payload)
                self.store.slack_end(day, outcome)
        except Busy:
            raise Denied("busy", "slack_lock") from None
        except (Conflict, StoreError):
            raise Denied("unavailable", "slack_state") from None
        except LimitReached:
            raise Denied("limit", "slack_slot") from None
        if outcome != "posted":
            raise Denied("unavailable", f"slack_{outcome}")
        return {"status": "ok"}

    def _send(self, payload: dict[str, Any]) -> str:
        for attempt in (1, 2):
            try:
                r = self._http.post(self.webhook, json=payload)
            except httpx2.HTTPError:
                return "ambiguous"
            if 200 <= r.status_code < 300:
                return "posted"
            if r.status_code == 429:
                wait = retry_after(r)
                if attempt == 1 and wait <= MAX_WAIT:
                    self.clock.sleep(wait)
                    continue
                return "failed"
            return "failed" if 400 <= r.status_code < 500 else "ambiguous"
        return "failed"
