"""Repo-relative paths, .env loading and policy.toml with built-in defaults (M3, M7)."""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
log = logging.getLogger(__name__)

DEFAULT_POLICY: dict[str, Any] = {
    "decision": {"block_risk": 60, "hold_risk": 25, "auto_pay_limit_usd": 5000, "dual_approval_above_usd": 25000},
    "fx_to_usd": {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012},
    "channel": {"trusted_processors": [], "free_mail": ["gmail.com", "outlook.com", "yahoo.com"],
                "lookalike_min_label_len": 5},
    "payment": {"known_shared_accounts": [], "amount_anomaly_multiple": 3.0, "duplicate_window_days": 14},
    "signatory": {"payments_title_keywords": ["finance", "accounts receivable", "accounts payable", "billing"],
                  "min_headcount_for_absence_note": 50},
    "buyer": {"buyer_domain": ""},
    "tactics": {"secrecy": [], "channel_switch": [], "authority": [], "threat": [], "payment_method": [],
                "urgency": ["urgent", "today", "immediately", "asap"]},
}

_policy: Optional[dict[str, Any]] = None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or ROOT / "policy.toml"
    try:
        with open(path, "rb") as f:
            return _merge(DEFAULT_POLICY, tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("policy.toml unusable (%s); using built-in defaults", type(e).__name__)
        return copy.deepcopy(DEFAULT_POLICY)


def policy() -> dict[str, Any]:
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


def _dir(env: str, default: str) -> Path:
    p = Path(os.environ.get(env) or default)
    return p if p.is_absolute() else ROOT / p


def data_dir() -> Path:
    return _dir("PAYEEGUARD_DATA_DIR", "data")


def reports_dir() -> Path:
    return _dir("PAYEEGUARD_REPORTS_DIR", "reports")


def payments_dir() -> Path:
    return _dir("PAYEEGUARD_PAYMENTS_DIR", "payments")


def to_usd(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    """Static FX; None when the amount or the currency is unknown (E9)."""
    if amount is None or not currency:
        return None
    rate = policy()["fx_to_usd"].get(currency.upper())
    return None if rate is None else amount * float(rate)
