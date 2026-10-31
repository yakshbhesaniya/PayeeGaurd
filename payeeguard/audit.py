"""Tamper-evident audit log: each line carries the hash of the previous one (spec section 11)."""

import hashlib
import json
from datetime import datetime, timezone

from . import config


def _path():
    p = config.reports_dir() / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _last_hash() -> str:
    p = _path()
    if not p.exists():
        return "0" * 64
    last = ""
    with open(p, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last)["hash"] if last else "0" * 64


def append(event: str, **fields) -> str:
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
    prev = "0" * 64
    with open(p, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "hash"}
            expect = hashlib.sha256((prev + _canon(body)).encode()).hexdigest()
            if e.get("prev_hash") != prev or e.get("hash") != expect:
                return False, f"tampering detected at line {n}"
            prev = e["hash"]
    return True, f"audit chain intact ({n} events)"
