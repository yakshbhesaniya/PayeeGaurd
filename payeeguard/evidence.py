"""Small helpers shared by every check module."""

from .client import ApiError, CrustdataError, NotEnabled, OfflineMiss
from .models import Evidence


def ev(check, family, claim, status, detail, source, weight=0, hard=False, reason=None, confirms=None) -> Evidence:
    return Evidence(check, family, claim, status, detail, source, weight, hard, reason, confirms)


def unchecked(check, family, claim, detail, source, reason="no_data") -> Evidence:
    return Evidence(check, family, claim, "unchecked", detail, source, 0, False, reason)


def reason_for(err: Exception) -> str:
    if isinstance(err, NotEnabled):
        return "not_enabled"
    if isinstance(err, (OfflineMiss, ApiError, CrustdataError)):
        return "api_error"
    return "api_error"


def describe(err: Exception) -> str:
    if isinstance(err, NotEnabled):
        return "not enabled on our key"
    if isinstance(err, OfflineMiss):
        return "offline and not in cache"
    return str(err)[:160] or type(err).__name__


def first_match(resp) -> tuple[dict | None, list[dict]]:
    """Identify/Enrich return a top-level array; never index matches[0] blindly (edge case D6)."""
    if not isinstance(resp, list) or not resp:
        return None, []
    matches = resp[0].get("matches") or []
    matches = sorted(matches, key=lambda m: m.get("confidence_score") or 0, reverse=True)
    return (matches[0] if matches else None), matches
