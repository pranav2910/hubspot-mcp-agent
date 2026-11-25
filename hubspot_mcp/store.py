"""F3. Store: SQLite state and limits shared by both processes."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .common import Busy, Clock, StartupError, ensure_state_dir, file_lock

SCHEMA_VERSION = 1
SEEN_TTL = 24 * 3600
OPEN_STATES = ("pending", "approved", "executing", "ambiguous")

TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected", "expired", "stale"},
    "approved": {"executing", "pending", "expired", "stale"},
    "executing": {"done", "failed", "ambiguous", "pending"},
    "ambiguous": {"done", "failed", "executing"},
}

_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE seen(contact_id TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
CREATE TABLE sources(source_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, name_domain_sha TEXT NOT NULL,
  url TEXT NOT NULL, title TEXT NOT NULL, fact TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE research(company_id TEXT PRIMARY KEY, name_domain_sha TEXT NOT NULL, status TEXT NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE proposals(id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('score','task')),
  contact_id TEXT NOT NULL, payload TEXT NOT NULL, payload_sha TEXT NOT NULL, snapshot TEXT NOT NULL,
  display TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  done_at INTEGER, retries INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX one_open ON proposals(contact_id, kind)
  WHERE state IN ('pending','approved','executing','ambiguous');
CREATE TABLE review(contact_id TEXT PRIMARY KEY, reason TEXT NOT NULL, at INTEGER NOT NULL);
CREATE TABLE counters(day TEXT NOT NULL, key TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY(day, key));
CREATE TABLE spend(id INTEGER PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL, settled INTEGER,
  status TEXT NOT NULL);
CREATE TABLE slack(day TEXT PRIMARY KEY, state TEXT NOT NULL, attempts INTEGER NOT NULL);
"""


class StoreError(Exception):
    """State is unavailable; callers must deny."""


class LimitReached(Exception):
    pass


class OpenProposal(Exception):
    pass


class Conflict(Exception):
    """Compare-and-swap lost, or the move is not allowed."""


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    contact_id: str
    payload: str
    payload_sha: str
    snapshot: dict[str, Any]
    display: dict[str, Any]
    state: str
    created_at: int
    expires_at: int
    done_at: int | None
    retries: int
    version: int


def _proposal(row: sqlite3.Row) -> Proposal:
    d = dict(row)
    d["snapshot"] = json.loads(d["snapshot"])
    d["display"] = json.loads(d["display"])
    return Proposal(**d)


class Store:
    def __init__(self, state: Path, tz: ZoneInfo, clock: Clock | None = None, busy_ms: int = 5000):
        self.path = ensure_state_dir(state) / "store.sqlite3"
        self.tz = tz
        self.clock = clock or Clock()
        self.busy_ms = busy_ms
        self._open()

    # -- connections -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=self.busy_ms / 1000, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute(f"PRAGMA busy_timeout={int(self.busy_ms)}")
        c.execute("PRAGMA synchronous=FULL")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            c.close()
            raise StoreError("store was written by a newer version")
        return c

    def _open(self) -> None:
        # SQLite's busy timeout doesn't cover the switch to WAL, so first opens take turns.
        try:
            with file_lock(self.path.parent / "store.lock", self.busy_ms / 1000):
                c = self._connect()
                try:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("BEGIN IMMEDIATE")
                    if c.execute("PRAGMA user_version").fetchone()[0] == 0:
                        for statement in _SCHEMA.split(";\n"):
                            if statement.strip():
                                c.execute(statement)
                        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    c.execute("COMMIT")
                finally:
                    c.close()
                os.chmod(self.path, 0o600)
        except (sqlite3.Error, StoreError, Busy, OSError) as e:
            raise StartupError(f"store unavailable: {e}") from None

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Never held across the audit lock or network calls (S1)."""
        try:
            c = self._connect()
        except sqlite3.Error as e:
            raise StoreError(type(e).__name__) from None
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error as e:
            raise StoreError(type(e).__name__) from None
        finally:
            c.close()

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        try:
            c = self._connect()
            try:
                return c.execute(sql, args).fetchall()
            finally:
                c.close()
        except sqlite3.Error as e:
            raise StoreError(type(e).__name__) from None

    # -- days and caps -----------------------------------------------------

    def _today(self, c: sqlite3.Connection) -> str:
        """Today in the policy timezone, but never earlier than a day already seen (S4)."""
        today = self.clock.now().astimezone(self.tz).date().isoformat()
        row = c.execute("SELECT value FROM meta WHERE key='day'").fetchone()
        if row and row[0] >= today:
            return row[0]
        c.execute("INSERT INTO meta VALUES('day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
        return today

    @staticmethod
    def _take(c: sqlite3.Connection, day: str, key: str, limit: int) -> None:
        row = c.execute("SELECT n FROM counters WHERE day=? AND key=?", (day, key)).fetchone()
        if (row[0] if row else 0) >= limit:
            raise LimitReached(key)
        c.execute("INSERT INTO counters VALUES(?,?,1) ON CONFLICT(day,key) DO UPDATE SET n=n+1", (day, key))

    def today(self) -> str:
        with self._tx() as c:
            return self._today(c)

    def reserve(self, key: str, limit: int) -> str:
        with self._tx() as c:
            day = self._today(c)
            self._take(c, day, key, limit)
            return day

    def release(self, day: str, key: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE counters SET n=n-1 WHERE day=? AND key=? AND n>0", (day, key))

    def count(self, key: str) -> int:
        with self._tx() as c:
            row = c.execute("SELECT n FROM counters WHERE day=? AND key=?", (self._today(c), key)).fetchone()
            return row[0] if row else 0

    # -- leads -------------------------------------------------------------

    def mark_seen(self, contact_ids: list[str]) -> None:
        now = self.clock.epoch()
        with self._tx() as c:
            c.executemany("INSERT INTO seen VALUES(?,?) ON CONFLICT(contact_id) DO UPDATE SET seen_at=excluded.seen_at",
                          [(i, now) for i in contact_ids])

    def is_seen(self, contact_id: str) -> bool:
        rows = self._all("SELECT seen_at FROM seen WHERE contact_id=?", contact_id)
        return bool(rows) and self.clock.epoch() - rows[0][0] <= SEEN_TTL

    def _bounds(self, day: str) -> tuple[int, int]:
        d = dt.date.fromisoformat(day)
        return tuple(int(dt.datetime.combine(x, dt.time(), self.tz).timestamp())  # type: ignore[return-value]
                     for x in (d, d + dt.timedelta(days=1)))

    def seen_on(self, day: str) -> int:
        return self._all("SELECT COUNT(*) FROM seen WHERE seen_at>=? AND seen_at<?", *self._bounds(day))[0][0]

    # -- research ----------------------------------------------------------

    def get_research(self, company_id: str, sha: str) -> tuple[str, list[dict[str, str]]] | None:
        now = self.clock.epoch()
        rows = self._all("SELECT status FROM research WHERE company_id=? AND name_domain_sha=? AND expires_at>?",
                         company_id, sha, now)
        if not rows:
            return None
        facts = self._all("SELECT source_id, url, title, fact FROM sources WHERE company_id=? AND name_domain_sha=?"
                          " AND expires_at>? ORDER BY rowid", company_id, sha, now)
        return rows[0][0], [dict(f) for f in facts]

    def put_research(self, company_id: str, sha: str, status: str, facts: list[dict[str, str]], ttl: int) -> None:
        expires = self.clock.epoch() + ttl
        with self._tx() as c:
            c.execute("DELETE FROM sources WHERE company_id=?", (company_id,))
            c.execute("INSERT OR REPLACE INTO research VALUES(?,?,?,?)", (company_id, sha, status, expires))
            c.executemany("INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                          [(f["source_id"], company_id, sha, f["url"], f["title"], f["fact"], expires) for f in facts])

    def sources(self, ids: list[str], company_id: str, sha: str) -> list[dict[str, str]]:
        """Rows for these IDs that belong to this company now; anything else is left out."""
        out = []
        for sid in ids:
            out += [dict(r) for r in self._all(
                "SELECT source_id, url, title, fact FROM sources WHERE source_id=? AND company_id=?"
                " AND name_domain_sha=? AND expires_at>?", sid, company_id, sha, self.clock.epoch())]
        return out

    def reserve_spend(self, amount: int, limit: int) -> int:
        """Reserve microdollars against the day's limit; returns a spend id."""
        with self._tx() as c:
            day = self._today(c)
            used = c.execute("SELECT COALESCE(SUM(COALESCE(settled, reserved)),0) FROM spend WHERE day=?",
                             (day,)).fetchone()[0]
            if used + amount > limit:
                raise LimitReached("research_dollars")
            return c.execute("INSERT INTO spend(day,reserved,settled,status) VALUES(?,?,NULL,'reserved')",
                             (day, amount)).lastrowid

    def settle_spend(self, spend_id: int, actual: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE spend SET settled=?, status='settled' WHERE id=?", (actual, spend_id))

    def spent_today(self) -> int:
        with self._tx() as c:
            return c.execute("SELECT COALESCE(SUM(COALESCE(settled, reserved)),0) FROM spend WHERE day=?",
                             (self._today(c),)).fetchone()[0]

    # -- reviews -----------------------------------------------------------

    def add_review(self, contact_id: str, reason: str) -> None:
        with self._tx() as c:
            self._expire(c)
            if c.execute(f"SELECT 1 FROM proposals WHERE contact_id=? AND kind='score' AND state IN {OPEN_STATES}",
                         (contact_id,)).fetchone():
                raise OpenProposal
            c.execute("INSERT OR REPLACE INTO review VALUES(?,?,?)", (contact_id, reason, self.clock.epoch()))

    def reviews(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM review ORDER BY at, contact_id")]

    def dismiss_review(self, contact_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM review WHERE contact_id=?", (contact_id,))

    # -- proposals ---------------------------------------------------------

    def _expire(self, c: sqlite3.Connection) -> None:
        c.execute("UPDATE proposals SET state='expired', version=version+1 WHERE state='pending' AND expires_at<=?",
                  (self.clock.epoch(),))

    def add_proposal(self, *, id: str, kind: str, contact_id: str, payload: str, payload_sha: str,
                     snapshot: dict[str, Any], display: dict[str, Any], expires_at: int) -> None:
        try:
            with self._tx() as c:
                self._expire(c)
                c.execute("INSERT INTO proposals(id,kind,contact_id,payload,payload_sha,snapshot,display,state,"
                          "created_at,expires_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                          (id, kind, contact_id, payload, payload_sha, json.dumps(snapshot), json.dumps(display),
                           self.clock.epoch(), expires_at))
                if kind == "score":
                    c.execute("DELETE FROM review WHERE contact_id=?", (contact_id,))
        except sqlite3.IntegrityError:
            raise OpenProposal from None

    def proposal(self, id: str) -> Proposal:
        rows = self._all("SELECT * FROM proposals WHERE id=?", id)
        if not rows:
            raise Conflict("unknown proposal")
        return _proposal(rows[0])

    def in_state(self, state: str) -> list[Proposal]:
        with self._tx() as c:
            self._expire(c)
            rows = c.execute("SELECT * FROM proposals WHERE state=? ORDER BY created_at, id", (state,)).fetchall()
        return [_proposal(r) for r in rows]

    def done_on(self, day: str, kind: str = "score") -> list[Proposal]:
        return [_proposal(r) for r in self._all(
            "SELECT * FROM proposals WHERE state='done' AND kind=? AND done_at>=? AND done_at<?",
            kind, *self._bounds(day))]

    def _move(self, c: sqlite3.Connection, p: Proposal, to: str) -> None:
        if to not in TRANSITIONS.get(p.state, set()):
            raise Conflict(f"{p.state} -> {to} is not allowed")
        retry = p.state == "ambiguous" and to == "executing"
        if retry and p.kind != "score":
            raise Conflict("tasks are never re-sent")
        n = c.execute(
            "UPDATE proposals SET state=?, version=version+1, retries=retries+?, "
            "done_at=CASE WHEN ?='done' THEN ? ELSE done_at END "
            "WHERE id=? AND state=? AND version=?" + (" AND retries=0" if retry else ""),
            (to, int(retry), to, self.clock.epoch(), p.id, p.state, p.version)).rowcount
        if n != 1:
            raise Conflict("proposal changed")

    def move(self, p: Proposal, to: str) -> Proposal:
        with self._tx() as c:
            self._move(c, p, to)
        return self.proposal(p.id)

    def start_write(self, p: Proposal, limit: int) -> Proposal:
        """Take one daily write and move to executing, together or not at all."""
        with self._tx() as c:
            self._take(c, self._today(c), "writes", limit)
            self._move(c, p, "executing")
        return self.proposal(p.id)

    # -- slack slot --------------------------------------------------------

    def slack_begin(self, limit: int, attempts: int = 3) -> str:
        """Caller holds the Slack file lock. Returns the day whose slot was reserved."""
        with self._tx() as c:
            day = self._today(c)
            row = c.execute("SELECT state, attempts FROM slack WHERE day=?", (day,)).fetchone()
            if limit < 1:
                outcome = "blocked"
            elif row is None:
                c.execute("INSERT INTO slack VALUES(?,'reserved',1)", (day,))
                outcome = "ok"
            elif row["state"] == "failed" and row["attempts"] < attempts:
                c.execute("UPDATE slack SET state='reserved', attempts=attempts+1 WHERE day=?", (day,))
                outcome = "ok"
            else:
                # A reserved row seen under the lock was left by a crash: it may have posted.
                c.execute("UPDATE slack SET state='ambiguous' WHERE day=? AND state='reserved'", (day,))
                outcome = "blocked"
        if outcome != "ok":
            raise LimitReached("slack")
        return day

    def slack_end(self, day: str, state: str) -> None:
        with self._tx() as c:
            if c.execute("UPDATE slack SET state=? WHERE day=? AND state='reserved'", (state, day)).rowcount != 1:
                raise Conflict("slack slot changed")

    def slack_state(self) -> str | None:
        with self._tx() as c:
            row = c.execute("SELECT state FROM slack WHERE day=?", (self._today(c),)).fetchone()
            return row[0] if row else None
