"""Shared pieces: errors, clock, file locks, env files, text handling."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import html
import json
import logging
import os
import re
import stat
import sys
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Iterator

from dotenv import dotenv_values

ROOT = Path(os.environ.get("HUBSPOT_MCP_HOME") or Path(__file__).resolve().parent.parent)


class StartupError(Exception):
    """Configuration is unusable; the process must not start."""


class Denied(Exception):
    """A tool refused. `code` is the only thing the model sees (S6)."""

    def __init__(self, code: str, rule: str = ""):
        super().__init__(code)
        self.code = code
        self.rule = rule or code


class Busy(Exception):
    """A lock could not be taken in time."""


class Clock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def epoch(self) -> int:
        return int(self.now().timestamp())

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def log_exception(where: str) -> None:
    """stderr only, and never the message: it could carry a key or CRM text."""
    etype, _, tb = sys.exc_info()
    frames = traceback.extract_tb(tb)
    at = f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "?"
    print(f"{where}: {etype.__name__ if etype else 'error'} at {at}", file=sys.stderr)


@contextlib.contextmanager
def file_lock(path: Path, wait: float) -> Iterator[None]:
    """Exclusive flock. Each call opens its own descriptor, so it also excludes threads."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy(path.name) from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def ensure_state_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def load_env(path: Path, required: list[str], forbidden: list[str]) -> dict[str, str]:
    """Read one env file without touching os.environ (S5)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise StartupError(f"missing {path.name}") from None
    if not stat.S_ISREG(info.st_mode):
        raise StartupError(f"{path.name} must be a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise StartupError(f"{path.name} must be yours and mode 0600 (chmod 600 {path.name})")
    try:
        values = {k: v for k, v in dotenv_values(path, interpolate=False).items() if v}
    except (UnicodeError, OSError):
        raise StartupError(f"{path.name} can't be read as UTF-8 text") from None
    for name, value in values.items():
        if not (value.isascii() and value.isprintable()):
            raise StartupError(f"{path.name}: {visible(name)} has characters that don't belong in a key (smart quotes?)")
    for name in forbidden:
        if name in values:
            raise StartupError(f"{path.name} must not contain {name}")
    for name in required:
        if name not in values:
            raise StartupError(f"{path.name} is missing {name}")
    return values


def canonical(value: Any) -> str:
    """Canonical JSON. Floats are refused so hashes never depend on float formatting."""

    def check(v: Any) -> None:
        if isinstance(v, float):
            raise ValueError("floats are not allowed in canonical JSON")
        if isinstance(v, dict):
            for item in v.values():
                check(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                check(item)

    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- text -----------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+'-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_PHONE = re.compile(r"(?<!\d)(?!(?:19|20)\d\d[ \-\u2013](?:19|20)\d\d(?!\d))\+?\(?\d(?:[\s().\-/\u2010-\u2015\u2212]{0,4}\d){6,}")
_TAIL = 400  # longer than any email or phone the patterns above can match

# Code points that render as nothing or as blank space but are not in category C or Z.
_INVISIBLE = [
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x2800, 0x2800), (0x3164, 0x3164), (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFFC), (0x13430, 0x1345F), (0x16FE4, 0x16FE4),
    (0x1BCA0, 0x1BCA3), (0x1D159, 0x1D159), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
]


def invisible(c: str) -> bool:
    n = ord(c)
    return unicodedata.category(c) == "Cf" or any(a <= n <= b for a, b in _INVISIBLE)


def has_email(text: str) -> bool:
    return bool(_EMAIL.search(text))


def mask(text: str) -> str:
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text))


def clean(text: Any, limit: int) -> str:
    """Outside text on its way to the model: one line, no invisible characters, masked, capped.

    Work is bounded by `limit`, not by the input, so a huge message can't stall the server.
    """
    if not isinstance(text, str):
        return ""
    window = text[: 4 * limit + _TAIL]
    out = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in window if not invisible(c))
    out = unicodedata.normalize("NFKC", out)[: 8 * limit + _TAIL]
    out = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in out if not invisible(c))
    masked = mask(out)
    if len(text) > len(window) or len(out) == 8 * limit + _TAIL:
        masked = masked[:-_TAIL]  # an email or phone cut by the window is dropped, not leaked
    return " ".join(masked.split())[:limit]


_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Me"}
_FORBIDDEN_SNIPPETS = ("<", ">", "[mcp:", "://", "www.", "   ")


def check_text(text: str, newlines: bool = False) -> str:
    """Text rule for proposals (F7). Raises ValueError; never repairs."""
    if not text.strip():
        raise ValueError("empty")
    marks = 0
    previous = " "
    for c in text:
        cat = unicodedata.category(c)
        if newlines and c == "\n":
            marks, previous = 0, c
            continue
        if cat in _FORBIDDEN_CATEGORIES or invisible(c) or (cat == "Zs" and c != " "):
            raise ValueError("forbidden character")
        if cat in ("Mn", "Mc"):
            marks += 1
            if marks > 2 or unicodedata.category(previous)[0] not in "LM":
                raise ValueError("stray combining mark")
        else:
            marks = 0
        previous = c
    if unicodedata.normalize("NFC", text) != text:
        raise ValueError("not NFC")
    lowered = text.lower()
    if any(s in lowered for s in _FORBIDDEN_SNIPPETS) or html.unescape(text) != text:
        raise ValueError("forbidden markup")
    return text


_RTL = {"R", "AL", "AN"}  # right-to-left text can visually reorder the rest of the line


def visible(text: str) -> str:
    """For the approval screen: anything that might not be seen becomes an escape; nothing is stripped."""
    out = []
    for c in text:
        cat = unicodedata.category(c)
        if c == " " or (cat[0] in "LNPS" and not invisible(c) and unicodedata.bidirectional(c) not in _RTL):
            out.append(c)
        else:
            out.append(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}")
    return "".join(out)


def is_id(value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20


def quiet_loggers() -> None:
    """Library request logs can contain the webhook URL; keep them off whatever the environment says."""
    for name in ("httpx2", "httpcore2", "anthropic"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False


_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


def valid_domain(raw: Any) -> str | None:
    """Lower-cased IDNA hostname with a dot; no IP, port, path or credentials."""
    if not isinstance(raw, str):
        return None
    try:
        host = raw.strip().lower().encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        return None
    if labels[-1].isdigit():  # dotted-quad addresses
        return None
    return host
