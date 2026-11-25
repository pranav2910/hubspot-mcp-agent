"""F4. Guard: the one path from a client to any tool (S1)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from .audit import Audit, AuditError
from .common import Denied, log_exception
from .models import Args
from .policy import Policy
from .store import LimitReached, Store, StoreError

MAX_ARGS = 16 * 1024
SLACK_CALLS_PER_DAY = 10  # posts are limited by the slot; this only bounds calls


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    model: type[Args]
    run: Callable[[Any], dict[str, Any]]
    read_only: bool


class Guard:
    def __init__(self, policy: Policy, audit: Audit, store: Store, tools: dict[str, Tool]):
        missing = set(policy.allowed_tools()) - set(tools)
        if missing:
            raise ValueError(f"no handler for {sorted(missing)}")
        self.policy, self.audit, self.store = policy, audit, store
        self.tools = {n: tools[n] for n in policy.allowed_tools()}
        self.halted = False

    def call(self, name: Any, arguments: Any) -> tuple[dict[str, Any], bool]:
        """Returns (payload, is_error). Never raises."""
        call_id = uuid.uuid4().hex
        try:
            return self._call(call_id, name, arguments)
        except Exception:
            # Only reachable before the decision record: the tool itself has its own catch below.
            log_exception("guard")
            return self._deny(call_id, name, None, "unavailable", "internal")

    def busy(self, name: Any, arguments: Any) -> tuple[dict[str, Any], bool]:
        """The pool is full: refuse, but still on the record."""
        try:
            raw = self._raw({} if arguments is None else arguments)
            return self._deny(uuid.uuid4().hex, name, raw, "busy", "pool_full")
        except Exception:
            log_exception("guard")
            return {"error": "busy"}, True

    # ----------------------------------------------------------------------

    @staticmethod
    def _raw(arguments: Any) -> bytes | None:
        try:
            return json.dumps(arguments, ensure_ascii=False).encode("utf-8", "surrogatepass")
        except (TypeError, ValueError, RecursionError):
            return None

    def _tool_label(self, name: Any) -> Any:
        return name if isinstance(name, str) and name in self.tools else self.audit.secret(str(name))

    def _deny(self, call_id: str, name: Any, raw: bytes | None, code: str, rule: str) -> tuple[dict[str, Any], bool]:
        if not self.halted:
            try:
                self.audit.append("decision", call_id=call_id, tool=self._tool_label(name),
                                  args=self.audit.secret(raw or b""), decision="deny", rule=rule)
            except AuditError:
                log_exception("audit")
        return {"error": code}, True

    def _call(self, call_id: str, name: Any, arguments: Any) -> tuple[dict[str, Any], bool]:
        if self.halted:
            return {"error": "unavailable"}, True
        if arguments is None:
            arguments = {}
        raw = self._raw(arguments)
        if raw is None:
            return self._deny(call_id, name, raw, "invalid", "unserializable")
        if len(raw) > MAX_ARGS:
            return self._deny(call_id, name, raw, "invalid", "too_large")

        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._deny(call_id, name, raw, "blocked", "unknown_tool")
        rules = self.policy.tool(name)
        try:
            args = tool.model.model_validate(arguments)
        except (ValidationError, RecursionError, ValueError):
            return self._deny(call_id, name, raw, "invalid", "arguments")
        if name == "search_new_leads" and args.days > rules.max_days:
            return self._deny(call_id, name, raw, "invalid", "max_days")

        key = f"tool:{name}"
        limit = SLACK_CALLS_PER_DAY if name == "post_slack_summary" else rules.per_day
        try:
            day = self.store.reserve(key, limit)
        except LimitReached:
            return self._deny(call_id, name, raw, "limit", "per_day")
        except StoreError:
            return self._deny(call_id, name, raw, "unavailable", "store")

        try:
            self.audit.append("decision", call_id=call_id, tool=name, args=args.log_args(self.audit.secret),
                              decision="allow", rule="policy")
        except AuditError:
            log_exception("audit")
            try:
                self.store.release(day, key)
            except StoreError:
                pass  # stays spent: fails closed
            return {"error": "unavailable"}, True

        try:
            payload, is_error = tool.run(args), False
            result = str(payload.get("status", "ok"))
        except Denied as e:
            payload, is_error, result = {"error": e.code}, True, f"{e.code}:{e.rule}"
        except LimitReached as e:
            payload, is_error, result = {"error": "limit"}, True, f"limit:{e}"
        except Exception:
            log_exception(name)
            payload, is_error, result = {"error": "unavailable"}, True, "unavailable:exception"

        try:
            self.audit.append("result", call_id=call_id, tool=name, result=result)
        except AuditError:
            log_exception("audit")
            self.halted = True  # nothing more runs until restart; `audit verify` shows the gap
            return {"error": "unavailable"}, True
        return payload, is_error
