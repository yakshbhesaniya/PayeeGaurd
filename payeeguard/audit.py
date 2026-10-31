"""Tamper-evident audit log: each line carries the hash of the previous one (spec section 11).

Robust by design: a damaged line is REPORTED by verify() but never stops new events being logged, and appends are
serialised with a lock so concurrent writers can't fork the chain.
"""

import hashlib
import json
import threading
from datetime import datetime, timezone

from . import config

GENESIS = "0" * 64
_LOCK = threading.Lock()


def _path():
    p = config.reports_dir() / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _last_hash() -> str:
    """Hash of the last *parseable* entry (a garbage line is skipped here and flagged by verify())."""
    p = _path()
    if not p.exists():
        return GENESIS
    last = GENESIS
    with open(p, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                h = json.loads(line).get("hash")
                if isinstance(h, str) and len(h) == 64:
                    last = h
            except (ValueError, AttributeError):
                continue
    return last


def append(event: str, **fields) -> str:
    with _LOCK:
        entry = {"event": event, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields,
                 "prev_hash": _last_hash()}
        entry["hash"] = hashlib.sha256((entry["prev_hash"] + _canon({k: v for k, v in entry.items() if k != "hash"}))
                                       .encode()).hexdigest()
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(_canon(entry) + "\n")
        return entry["hash"]


def verify() -> tuple[bool, str]:
    p = _path()
    if not p.exists():
        return True, "audit log empty"
    prev, n = GENESIS, 0
    with open(p, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                return False, f"tampering detected at line {n} (not valid JSON)"
            if not isinstance(e, dict):
                return False, f"tampering detected at line {n} (not an audit entry)"
            body = {k: v for k, v in e.items() if k != "hash"}
            expect = hashlib.sha256((prev + _canon(body)).encode()).hexdigest()
            if e.get("prev_hash") != prev or e.get("hash") != expect:
                return False, f"tampering detected at line {n}"
            prev = e["hash"]
    return True, f"audit chain intact ({n} events)"
