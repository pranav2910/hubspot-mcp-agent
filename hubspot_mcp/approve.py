"""F8. Approval: the only process that can write to HubSpot, and only after a person says yes."""

from __future__ import annotations

import json
import os
import pwd
import sys
import termios
import uuid
from pathlib import Path
from typing import Any, Callable

from .audit import Audit, AuditError
from .common import ROOT, Busy, Clock, StartupError, ensure_state_dir, file_lock, load_env, quiet_loggers, visible
from .hubspot import TASK_TO_CONTACT, VERSION, HubSpot, Unavailable, retry_after
from .policy import TASK_FIELDS, Policy, load_policy
from .proposals import SNAPSHOT_FIELDS, marker, number, payload_sha, snapshot
from .store import Conflict, LimitReached, Proposal, Store, StoreError

TOOL = {"score": "update_lead_score", "task": "create_followup_task"}
TRIES_429 = 4          # first request plus three retries
MAX_RETRY_WAIT = 10.0


def terminal_ask(prompt: str) -> str:
    """Anything typed before this prompt appeared is thrown away: it can't answer what wasn't on screen."""
    sys.stdout.flush()
    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    return input(prompt)


class Stop(Exception):
    """Finish now with a non-zero exit."""


class Approver:
    def __init__(self, root: Path, hs: HubSpot, store: Store, audit: Audit, clock: Clock,
                 ask: Callable[[str], str], say: Callable[[str], None]):
        self.root, self.hs, self.store, self.audit, self.clock = root, hs, store, audit, clock
        self.ask, self.say = ask, say
        self.unsure: list[str] = []  # became ambiguous during this run

    # -- audit: any failure ends the run -----------------------------------

    def record(self, p: Proposal | None, phase: str, call_id: str, **fields: Any) -> None:
        args = {"proposal_id": p.id, "contact_id": p.contact_id, "payload_sha": p.payload_sha} if p else {}
        try:
            self.audit.append(phase, call_id=call_id, tool=TOOL[p.kind] if p else "review",
                              args=fields.pop("args", args), **fields)
        except AuditError:
            raise Stop(f"audit log failed at proposal {p.id if p else '-'}") from None

    def move(self, p: Proposal, to: str) -> Proposal:
        try:
            return self.store.move(p, to)
        except (Conflict, StoreError) as e:
            raise Stop(f"could not update proposal {p.id}: {type(e).__name__}") from None

    def policy(self) -> Policy:
        policy, self.audit.policy_hash = load_policy(self.root / "policy.yaml")
        return policy

    # -- checks ------------------------------------------------------------

    @staticmethod
    def intact(p: Proposal) -> bool:
        """The stored payload is the one proposed, and has exactly the shape this program builds."""
        try:
            if payload_sha(p.kind, p.contact_id, p.payload) != p.payload_sha:
                return False
            body = json.loads(p.payload)
            props = body["properties"]
            if not all(isinstance(v, str) for v in props.values()):
                return False
            if p.kind == "score":
                return set(body) == {"properties"} and set(props) == {"lead_score", "lead_score_reason"}
            link = {"to": {"id": p.contact_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": TASK_TO_CONTACT}]}
            return (set(body) == {"properties", "associations"} and set(props) == TASK_FIELDS
                    and body["associations"] == [link] and props["hs_task_body"].endswith(marker(p.id))
                    and props["hubspot_owner_id"] == p.snapshot["owner"])
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return False

    @staticmethod
    def allowed_fields(p: Proposal, policy: Policy) -> bool:
        allowed = policy.write_fields.contact if p.kind == "score" else policy.write_fields.task
        return set(json.loads(p.payload)["properties"]) <= set(allowed)

    def current(self, p: Proposal) -> dict[str, Any] | None:
        """Contact values now; None when the contact is gone. Raises Unavailable."""
        props = self.hs.contact(p.contact_id, SNAPSHOT_FIELDS)
        return None if props is None else snapshot(props)

    @staticmethod
    def unchanged(p: Proposal, now: dict[str, Any]) -> bool:
        keys = ("score", "reason") if p.kind == "score" else ("owner",)
        return all(now[k] == p.snapshot[k] for k in keys)

    def check(self, p: Proposal, policy: Policy) -> tuple[str, str]:
        """('ok', '') or (state to move to, rule)."""
        if not policy.writes_enabled:
            return "pending", "writes_disabled"
        if policy.tool(TOOL[p.kind]) is None:
            return "pending", "tool_not_allowed"
        if not self.intact(p):
            return "stale", "payload_changed"
        if not self.allowed_fields(p, policy):
            return "stale", "field_not_allowed"
        if p.expires_at <= self.clock.epoch():
            return "expired", "expired"
        try:
            now = self.current(p)
        except Unavailable:
            return "pending", "cannot_read_contact"
        if now is None:
            return "stale", "contact_gone"
        if not self.unchanged(p, now):
            return "stale", "contact_changed"
        return "ok", ""

    # -- one write ---------------------------------------------------------

    def execute(self, p: Proposal, call_id: str, recovery: bool) -> str:
        """p is approved (or ambiguous, for the one score retry). Returns the final state."""
        policy = self.policy()
        target, rule = self.check(p, policy)
        if target != "ok":
            return self._refuse(p, call_id, target, rule, recovery)
        try:
            p = self.store.start_write(p, policy.writes_per_day)
        except LimitReached:
            return self._refuse(p, call_id, "pending", "writes_per_day", recovery)
        except (Conflict, StoreError) as e:
            raise Stop(f"could not start proposal {p.id}: {type(e).__name__}") from None
        try:
            self.record(p, "intent", call_id, decision="send", rule="retry" if recovery else "approved")
        except Stop:
            self.move(p, "ambiguous" if recovery else "pending")  # nothing was sent in this attempt
            raise
        outcome = self._send(p, recovery)
        p = self.move(p, outcome)
        self.record(p, "result", call_id, result=outcome)
        return outcome

    def _refuse(self, p: Proposal, call_id: str, target: str, rule: str, recovery: bool) -> str:
        if recovery:  # an earlier request may have landed: hold it for the person
            target = "ambiguous"
        else:
            p = self.move(p, target)
        self.record(p, "result", call_id, result=target, rule=rule)
        self.say(f"  not sent: {rule}")
        return target

    def _send(self, p: Proposal, recovery: bool) -> str:
        uncertain = "ambiguous" if recovery else "failed"
        if p.kind == "score":
            method, path = "PATCH", f"/crm/objects/{VERSION}/contacts/{p.contact_id}"
        else:
            method, path = "POST", f"/crm/objects/{VERSION}/tasks"
        for attempt in range(1, TRIES_429 + 1):
            r = self.hs.send(method, path, p.payload)
            if r is None:
                return "ambiguous"
            if 200 <= r.status_code < 300:
                return "done"
            if r.status_code != 429:
                return uncertain if 400 <= r.status_code < 500 else "ambiguous"
            if attempt == TRIES_429:
                break
            self.clock.sleep(min(max(retry_after(r), 1.0), MAX_RETRY_WAIT))
            if p.kind == "score":
                try:
                    now = self.current(p)
                except Unavailable:
                    break
                if now is None or not self.unchanged(p, now):
                    break
        return uncertain

    # -- recovery ----------------------------------------------------------

    def reconcile(self, p: Proposal) -> str:
        """Settle an ambiguous proposal from what HubSpot shows. Never re-sends a task."""
        call_id = uuid.uuid4().hex
        try:
            if not self.intact(p):
                verdict = "held"
            elif p.kind == "task":
                found = any(marker(p.id) in body for body in self.hs.task_bodies(p.contact_id))
                verdict = "done" if found else "held"
            else:
                now, want = self.current(p), json.loads(p.payload)["properties"]
                if now is None:
                    verdict = "held"
                elif now["score"] == number(want["lead_score"]) and now["reason"] == want["lead_score_reason"]:
                    verdict = "done"
                else:
                    verdict = "retry" if self.unchanged(p, now) and p.retries == 0 else "held"
        except Unavailable:
            verdict = "held"
        if verdict == "done":
            p = self.move(p, "done")
            self.record(p, "result", call_id, result="done", rule="found_in_hubspot")
        elif verdict == "retry":
            self.record(p, "decision", call_id, decision="approve", rule="automatic_retry", actor="recovery")
            return self.execute(p, call_id, recovery=True)
        return "done" if verdict == "done" else "ambiguous"

    def recover(self) -> None:
        for p in self.store.in_state("approved"):
            self.move(p, "pending")
        for p in self.store.in_state("executing"):
            self.move(p, "ambiguous")
        for p in self.store.in_state("ambiguous"):
            self.reconcile(p)

    # -- screen ------------------------------------------------------------

    def show(self, p: Proposal, n: int, total: int) -> None:
        d = p.display
        try:
            now = self.current(p)
            shown = "contact not found" if now is None else \
                f"score={now['score']!r} reason={now['reason']!r} owner={now['owner']!r}"
        except Unavailable:
            shown = "could not be read"
        age = max(0, self.clock.epoch() - p.created_at) // 60
        lines = [
            "", f"--- Proposal {n} of {total}: {p.kind} for contact {p.contact_id} ({age} min old) ---",
            f"Company:  {d.get('company') or '(none)'}",
            f"In HubSpot now:  {shown}",
            f"Will send:  {p.payload}",
        ]
        if p.kind == "score":
            lines.append(f"Tier {d.get('tier')}, model's own confidence {d.get('confidence')}")
        lines += [f"Source:  {s.get('url')}  -  {s.get('fact')}" for s in d.get("sources", [])]
        for line in lines:
            self.say(visible(line))

    # -- run ---------------------------------------------------------------

    def run(self) -> int:
        try:
            return self._run()
        except Stop as e:
            self.say(f"STOPPED: {e}")
            return 1
        except StartupError as e:
            self.say(f"STOPPED: {e}")
            return 1
        except StoreError as e:
            self.say(f"STOPPED: state unavailable ({e})")
            return 1

    def _run(self) -> int:
        self.recover()
        pending = self.store.in_state("pending")
        if not pending:
            self.say("No proposals are waiting.")
        for n, p in enumerate(pending, 1):
            self.show(p, n, len(pending))
            answer = self.ask("Approve? [y/n/q] ").strip().lower()
            if answer == "q":
                break
            if answer not in ("y", "n"):
                self.say("  no decision made")
                continue
            call_id = uuid.uuid4().hex
            self.record(p, "decision", call_id, decision="approve" if answer == "y" else "reject", rule="person")
            try:
                p = self.store.move(p, "approved" if answer == "y" else "rejected")
            except Conflict:
                # expired while it was on screen
                self.record(p, "result", call_id, result="not_open", rule="expired")
                self.say("  no longer open")
                continue
            if answer == "n":
                self.say("  rejected")
                continue
            outcome = self.execute(p, call_id, recovery=False)
            self.say(f"  {outcome}")
            if outcome == "ambiguous":
                self.unsure.append(p.id)
        for pid in self.unsure:
            self.say(f"  checked {pid}: {self.reconcile(self.store.proposal(pid))}")
        self._held()
        self._reviews()
        return 0

    def _held(self) -> None:
        for p in self.store.in_state("ambiguous"):
            self.show(p, 1, 1)
            self.say("  The outcome is unknown. Look in HubSpot, then record what you found.")
            answer = self.ask("Did it happen? [done/failed/skip] ").strip().lower()
            if answer not in ("done", "failed"):
                continue
            call_id = uuid.uuid4().hex
            self.record(p, "decision", call_id, decision=f"resolve_{answer}", rule="person")
            p = self.move(p, answer)
            self.record(p, "result", call_id, result=answer, rule="resolved_by_person")

    def _reviews(self) -> None:
        for r in self.store.reviews():
            age = max(0, self.clock.epoch() - r["at"]) // 60
            answer = self.ask(f"Needs review: contact {r['contact_id']}, {r['reason']}, {age} min old. [d=dismiss/Enter] ")
            if answer.strip().lower() != "d":
                continue
            call_id = uuid.uuid4().hex
            args = {"contact_id": r["contact_id"]}
            self.record(None, "decision", call_id, decision="dismiss", rule="person", args=args)
            self.store.dismiss_review(r["contact_id"])
            self.record(None, "result", call_id, result="dismissed", args=args)


def start(root: Path = ROOT, *, hs_transport: Any = None, clock: Clock | None = None,
          ask: Callable[[str], str] = terminal_ask, say: Callable[[str], None] = print,
          is_tty: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty()) -> int:
    quiet_loggers()
    clock = clock or Clock()
    try:
        if not is_tty():
            raise StartupError("approve needs an interactive terminal")
        state = ensure_state_dir(root / "state")
        env = load_env(root / ".env.approver", ["HUBSPOT_WRITE_KEY"], forbidden=["HUBSPOT_READ_KEY", "ANTHROPIC_API_KEY"])
        policy, policy_hash = load_policy(root / "policy.yaml")
        if not policy.writes_enabled:
            raise StartupError("writes are disabled in policy.yaml")
        with file_lock(state / "approve.lock", 0):
            audit = Audit(state, "approve", actor=pwd.getpwuid(os.getuid()).pw_name, policy_hash=policy_hash, clock=clock)
            audit.check_tail()
            store = Store(state, policy.tz, clock)
            try:
                audit.append("start", result="approve started")
            except AuditError:
                raise StartupError("audit log cannot be written") from None
            return Approver(root, HubSpot(env["HUBSPOT_WRITE_KEY"], hs_transport, clock), store, audit, clock, ask, say).run()
    except Busy:
        say("STOPPED: another approve is already running")
        return 1
    except StartupError as e:
        say(f"STOPPED: {e}")
        return 1
    except OSError as e:
        say(f"STOPPED: {type(e).__name__} while opening the state folder")
        return 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")  # a limited terminal shows escapes, it doesn't crash
    try:
        return start()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Anything unfinished is checked on the next start.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
