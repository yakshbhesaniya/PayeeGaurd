"""Tamper-evident audit log (spec section 11): reports/audit.jsonl.

Each line is an event plus `prev_hash` and `hash = sha256(prev_hash + canonical_json(event))`.
Editing, deleting or reordering any line breaks the chain; `verify()` reports the first broken line.
Raw email text, full IBANs/account numbers and API keys are never written (Y1-Y3).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
GENESIS = "0" * 64
_DROP = {"raw_text", "body", "api_key", "authorization", "crustdata_api_key", "fp_secret"}
_MASK = {"bank_iban", "bank_account", "iban", "account", "account_number"}


def default_path() -> Path:
    return REPO_ROOT / "reports" / "audit.jsonl"


def canonical_json(event: Any) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _hash(prev_hash: str, event: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(event)).encode("utf-8")).hexdigest()


def _mask(value: Any) -> str:
    s = re.sub(r"[^0-9A-Za-z]", "", str(value))
    return f"••{s[-4:]}" if len(s) >= 4 else "••"


def sanitise(event: Any) -> Any:
    """Drop raw text / secrets and reduce account numbers to the last 4, recursively."""
    if isinstance(event, dict):
        out = {}
        for k, v in event.items():
            key = str(k).lower()
            if key in _DROP:
                continue
            full_number = key in _MASK and re.search(r"\d{5,}", re.sub(r"[\s-]", "", str(v or "")))
            out[str(k)] = _mask(v) if full_number else sanitise(v)
        return out
    if isinstance(event, (list, tuple)):
        return [sanitise(v) for v in event]
    return event


@contextmanager
def _locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Cross-process lock (M6) via an exclusive lock file; stale locks (> 30 s) are broken."""
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"audit log locked: {lock}") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def _last_hash(path: Path) -> str:
    if not path.exists():
        return GENESIS
    last = GENESIS
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                last = json.loads(line).get("hash", last) if line.strip() else last
            except (ValueError, AttributeError):
                continue      # a damaged line never stops logging; verify() reports it
    return last


def append(event: dict, path: str | Path | None = None) -> str:
    """Append one event (e.g. {"type": "verdict", "case_id": ..., "decision": ...}); returns its hash."""
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = sanitise(dict(event))
    body.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with _locked(p):
        prev = _last_hash(p)
        h = _hash(prev, body)
        with open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(canonical_json({**body, "prev_hash": prev, "hash": h}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return h


def last_hash(path: str | Path | None = None) -> str:
    """Hash of the newest event (for the case file's chain of custody)."""
    return _last_hash(Path(path) if path else default_path())


def verify(path: str | Path | None = None) -> tuple[bool, int, str]:
    """(ok, events checked, message). On failure the message names the first broken line."""
    p = Path(path) if path else default_path()
    if not p.exists():
        return True, 0, f"no audit log at {p} (0 events)"
    prev, n = GENESIS, 0
    with open(p, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                stored, claimed_prev = rec.pop("hash"), rec.pop("prev_hash")
            except (ValueError, KeyError, AttributeError, TypeError):
                return False, n, f"line {lineno}: not a valid audit record"
            if claimed_prev != prev:
                return False, n, f"line {lineno}: chain broken (prev_hash does not match line before)"
            if _hash(prev, rec) != stored:
                return False, n, f"line {lineno}: event edited (hash mismatch)"
            prev, n = stored, n + 1
    return True, n, f"audit log intact: {n} events, last hash {prev[:12]}"


def main(argv: Optional[list[str]] = None) -> int:
    """`python -m payeeguard.audit verify [PATH]` until cli.py wires `audit verify` in."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "verify":
        print("usage: python -m payeeguard.audit verify [PATH]", file=sys.stderr)
        return 2
    ok, _, msg = verify(args[1] if len(args) > 1 else None)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
