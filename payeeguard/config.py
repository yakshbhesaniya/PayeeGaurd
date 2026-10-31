"""Paths, environment and policy. Everything resolves relative to the repo root (edge case M3)."""

import copy
import logging
import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

log = logging.getLogger("payeeguard")

DEFAULT_POLICY: dict = {
    "decision": {"block_risk": 60, "hold_risk": 25, "auto_pay_limit_usd": 5000, "dual_approval_above_usd": 25000},
    "fx_to_usd": {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012},
    "channel": {"trusted_processors": [], "free_mail": ["gmail.com", "outlook.com", "yahoo.com", "hotmail.com"],
                "lookalike_min_label_len": 5},
    "payment": {"known_shared_accounts": [], "amount_anomaly_multiple": 3.0, "duplicate_window_days": 14},
    "signatory": {"payments_title_keywords": ["finance", "accounts receivable", "accounts payable", "billing"],
                  "min_headcount_for_absence_note": 50},
    "buyer": {"buyer_domain": ""},
    "tactics": {},
}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _path_from_env(name: str, default: str) -> Path:
    p = Path(env(name, default))
    return p if p.is_absolute() else ROOT / p


def cache_dir() -> Path:
    # Read on every call: eval switches it per sample (sample 10 simulates an outage).
    return _path_from_env("PAYEEGUARD_CACHE_DIR", ".cache")


def data_dir() -> Path:
    return _path_from_env("PAYEEGUARD_DATA_DIR", "data")


def reports_dir() -> Path:
    return ROOT / "reports"


def offline() -> bool:
    return env("PAYEEGUARD_OFFLINE", "0") == "1"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache(maxsize=1)
def policy() -> dict:
    path = ROOT / "policy.toml"
    try:
        with open(path, "rb") as f:
            return _merge(DEFAULT_POLICY, tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError) as e:  # edge case M7
        log.warning("policy.toml unreadable (%s); using defaults", e)
        return copy.deepcopy(DEFAULT_POLICY)


def setup_logging() -> None:
    fmt = '{"t":"%(asctime)s","lvl":"%(levelname)s","mod":"%(name)s","msg":"%(message)s"}' \
        if env("PAYEEGUARD_LOG") == "json" else "%(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.WARNING, format=fmt)
