"""F2. Audit log: hash-chained JSONL shared by the server and the approver."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import ROOT, Busy, Clock, StartupError, canonical, ensure_state_dir, file_lock, visible

ZERO = "0" * 64
MAX_RECORD = 8 * 1024
LOCK_WAIT = 5.0


class AuditError(Exception):
    """The record was not written. Callers must fail closed."""


class TornLog(AuditError):
    pass


@dataclass
class Report:
    records: int = 0
    head: str = ZERO
    errors: list[str] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    torn: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.torn


_TEXT = ("time", "process", "phase", "call_id", "tool", "decision", "rule", "actor", "result", "policy_hash",
         "prev_hash")
_FIELDS = {*_TEXT, "seq", "args"}
NEEDS_RESULT = ("allow", "approve", "resolve_done", "resolve_failed", "dismiss")


def _parse(raw: bytes) -> tuple[dict[str, Any], str]:
    """(record, hash). ValueError unless these are exactly the bytes this module would have written."""
    try:
        rec = json.loads(raw)
        if not isinstance(rec, dict):
            raise ValueError("not an object")
        stored = rec.pop("hash", None)
        if set(rec) != _FIELDS or not isinstance(stored, str):
            raise ValueError("wrong fields")
        if type(rec["seq"]) is not int or rec["seq"] < 0:
            raise ValueError("wrong seq")
        # an unregistered tool name is logged as {"hmac","len"}; everything else here is text
        if not all(isinstance(rec[k], str) or (k == "tool" and isinstance(rec[k], dict)) for k in _TEXT):
            raise ValueError("wrong types")
        if canonical({**rec, "hash": stored}).encode() != raw:
            raise ValueError("not canonical")
        return rec, stored
    except (RecursionError, UnicodeError, TypeError) as e:
        raise ValueError(type(e).__name__) from None


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)


def _digest(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + canonical(body)).encode()).hexdigest()


class Audit:
    def __init__(self, state: Path, process: str, actor: str = "model", policy_hash: str = "",
                 clock: Clock | None = None):
        self.dir = ensure_state_dir(state)
        self.path = self.dir / "audit.jsonl"
        self.lock_path = self.dir / "audit.lock"
        self.process = process
        self.actor = actor
        self.policy_hash = policy_hash
        self.clock = clock or Clock()
        self._key = self._load_key()

    # -- redaction ---------------------------------------------------------

    def _load_key(self) -> bytes:
        path = self.dir / "hmac.key"
        try:
            with file_lock(self.lock_path, LOCK_WAIT):
                if not path.exists():
                    tmp = self.dir / f".hmac.{secrets.token_hex(6)}"
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        os.write(fd, secrets.token_bytes(32))
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.link(tmp, path)  # atomic, and fails if the key appeared meanwhile
                    os.unlink(tmp)
                key = path.read_bytes()
        except (Busy, OSError) as e:
            raise StartupError(f"cannot prepare hmac.key: {type(e).__name__}") from None
        if len(key) != 32:
            raise StartupError("hmac.key is damaged")
        return key

    def secret(self, data: str | bytes) -> dict[str, Any]:
        raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
        return {"hmac": hmac.new(self._key, raw, hashlib.sha256).hexdigest(), "len": len(raw)}

    # -- appending ---------------------------------------------------------

    def _tail(self) -> tuple[int, str]:
        """(next seq, previous hash). Raises TornLog when the last line is not a valid record."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return 0, ZERO
        if size == 0:
            return 0, ZERO
        with open(self.path, "rb") as f:
            f.seek(max(0, size - 2 * MAX_RECORD))
            data = f.read()
        return self._tail_of(data)

    @staticmethod
    def _tail_of(data: bytes) -> tuple[int, str]:
        if not data:
            return 0, ZERO
        if not data.endswith(b"\n"):
            raise TornLog("last line is incomplete")
        try:
            rec, stored = _parse(data[:-1].rsplit(b"\n", 1)[-1])
            if _digest(rec["prev_hash"], rec) != stored:
                raise ValueError
            return rec["seq"] + 1, stored
        except ValueError:
            raise TornLog("last line is not a valid record") from None

    def _write(self, line: bytes) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            before = os.fstat(fd).st_size
            try:
                _write_all(fd, line)
            except OSError:
                try:
                    os.ftruncate(fd, before)  # best effort; a torn line blocks startup otherwise
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)

    def _append_locked(self, phase: str, **fields: Any) -> str:
        seq, prev = self._tail()
        body = {
            "seq": seq,
            "time": self.clock.now().isoformat(timespec="milliseconds"),
            "process": self.process,
            "phase": phase,
            "call_id": fields.get("call_id", ""),
            "tool": fields.get("tool", ""),
            "args": fields.get("args", {}),
            "decision": fields.get("decision", ""),
            "rule": fields.get("rule", ""),
            "actor": fields.get("actor") or self.actor,
            "result": fields.get("result", ""),
            "policy_hash": self.policy_hash,
            "prev_hash": prev,
        }
        digest = _digest(prev, body)
        line = (canonical({**body, "hash": digest}) + "\n").encode()
        if len(line) > MAX_RECORD:
            raise AuditError("record too large")
        self._write(line)
        return digest

    def append(self, phase: str, **fields: Any) -> str:
        try:
            with file_lock(self.lock_path, LOCK_WAIT):
                return self._append_locked(phase, **fields)
        except AuditError:
            raise
        except Exception as e:  # whatever went wrong, the record is not there: callers fail closed
            raise AuditError(type(e).__name__) from None

    # -- integrity ---------------------------------------------------------

    def head(self) -> str:
        try:
            with file_lock(self.lock_path, LOCK_WAIT):
                return self._tail()[1]
        except (Busy, OSError) as e:
            raise AuditError(type(e).__name__) from None

    def check_tail(self) -> None:
        try:
            self.head()
        except TornLog as e:
            fix = "; run: python -m hubspot_mcp.audit repair" if "incomplete" in str(e) else \
                "; run: python -m hubspot_mcp.audit verify, then move the state folder aside to start a new log"
            raise StartupError(f"audit log: {e}{fix}") from None
        except AuditError as e:
            raise StartupError(f"audit log unavailable: {e}") from None

    def verify(self, anchor: str | None = None) -> Report:
        with file_lock(self.lock_path, LOCK_WAIT):
            data = self.path.read_bytes() if self.path.exists() else b""
        report = Report()
        report.torn = bool(data) and not data.endswith(b"\n")
        lines = data.split(b"\n")[:-1]
        open_calls: dict[str, str] = {}
        found = anchor is None
        for n, raw in enumerate(lines):
            try:
                rec, stored = _parse(raw)
                if rec["prev_hash"] != report.head:
                    report.errors.append(f"line {n + 1}: chain broken")
                if rec["seq"] != n:
                    report.errors.append(f"line {n + 1}: expected seq {n}, found {rec['seq']}")
                if _digest(rec["prev_hash"], rec) != stored:
                    report.errors.append(f"line {n + 1}: content does not match its hash")
                if n == 0 and rec["phase"] != "start":
                    report.errors.append("line 1: not a start record")
            except ValueError as e:
                report.errors.append(f"line {n + 1}: not a record this program wrote ({e})")
                continue
            report.head = stored
            found = found or stored == anchor
            key = rec["call_id"]
            if rec["phase"] == "intent" or (rec["phase"] == "decision" and rec["decision"] in NEEDS_RESULT):
                open_calls[key] = f"line {n + 1}: {rec['phase']} {rec['tool'] if isinstance(rec['tool'], str) else '?'} has no result"
            elif rec["phase"] == "result":
                open_calls.pop(key, None)
        report.records = len(lines)
        report.unfinished = list(open_calls.values())
        if not found:
            report.errors.append("anchor not found: the log was rewritten or cut before it")
        return report

    def repair(self) -> int:
        """Move a torn last line aside and record that it happened. Returns bytes moved."""
        with file_lock(self.lock_path, LOCK_WAIT):
            data = self.path.read_bytes() if self.path.exists() else b""
            if not data or data.endswith(b"\n"):
                return 0  # complete but invalid lines are evidence, not damage: never moved
            keep = data.rfind(b"\n") + 1
            torn = data[keep:]
            self._tail_of(data[:keep])  # refuse before touching anything if the line before is bad too
            side = self.dir / f"audit.torn.{self.clock.epoch()}.{secrets.token_hex(8)}"
            fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(fd, torn)
            finally:
                os.close(fd)
            fd = os.open(self.path, os.O_WRONLY)
            try:
                os.ftruncate(fd, keep)
                os.fsync(fd)
            finally:
                os.close(fd)
            if keep == 0:
                self._append_locked("start", result="new log after a torn first line")
            self._append_locked("repair", args=self.secret(torn), result="torn line moved")
            return len(torn)


def main(argv: list[str] | None = None) -> int:
    import pwd

    p = argparse.ArgumentParser(prog="hubspot_mcp.audit")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--anchor")
    sub.add_parser("repair")
    a = p.parse_args(argv)
    try:
        audit = Audit(ROOT / "state", "audit-cli", actor=pwd.getpwuid(os.getuid()).pw_name)
    except (StartupError, OSError) as e:
        print(f"cannot open the audit log: {e}")
        return 1
    if a.cmd == "repair":
        try:
            moved = audit.repair()
        except (AuditError, Busy, OSError) as e:
            print(f"cannot repair: {e}")
            return 1
        print(f"moved {moved} bytes aside" if moved else "nothing to repair")
        return 0
    try:
        r = audit.verify(a.anchor)
    except (Busy, OSError) as e:
        print(f"cannot read the audit log: {type(e).__name__}")
        return 1
    print(f"{r.records} records, head {r.head}")
    for line in r.errors + (["last line is torn"] if r.torn else []):
        print("FAIL", visible(line))
    for line in r.unfinished:
        print("NOTE", visible(line))
    print("OK" if r.ok else "NOT OK")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
