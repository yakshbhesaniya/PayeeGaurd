"""PayeeGuard MCP server (FastMCP, stdio). Claude extracts and orchestrates; Python decides.

stdout is the MCP protocol channel: nothing here may print to it. Logs go to stderr.
"""
from __future__ import annotations

import functools
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))   # M3: started by Claude Desktop from any directory

from mcp.server.fastmcp import FastMCP  # noqa: E402

from payeeguard import gate  # noqa: E402
from payeeguard.pipeline import analyse, finish, onboard  # noqa: E402
from payeeguard.report import chat_report, render_onboarding  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("payeeguard.server")
mcp = FastMCP("payeeguard")


def _safe(fn):
    """Tools never raise into the client: an internal error is a fail-safe message, not a crash."""
    @functools.wraps(fn)   # keeps the signature FastMCP builds the tool schema from
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", fn.__name__)
            return f"PayeeGuard internal error ({type(e).__name__}). Do not pay: treat this request as HOLD."
    return wrapper


@mcp.tool()
@_safe
def verify_payee(raw_text: str, vendor_name: Optional[str] = None, vendor_domain: Optional[str] = None,
                 sender_email: Optional[str] = None, sender_name: Optional[str] = None,
                 sender_title: Optional[str] = None, reply_to_email: Optional[str] = None,
                 remit_country: Optional[str] = None, bank_iban: Optional[str] = None,
                 bank_account: Optional[str] = None, bank_swift: Optional[str] = None,
                 amount: Optional[float] = None, currency: Optional[str] = None,
                 invoice_number: Optional[str] = None, bank_change_requested: Optional[bool] = None,
                 urgency_language: Optional[bool] = None) -> str:
    """Verify an invoice or bank-change email before paying. Pass the FULL raw email text (with From/Reply-To
    headers) as raw_text; optional fields are cross-checked against the text and the text always wins.
    Returns PAY / HOLD / BLOCK with a cited evidence ledger and the case_id for pay_invoice / resolve_case."""
    fields = {k: v for k, v in locals().items() if k != "raw_text" and v is not None}
    case = analyse(raw_text, fields)
    finish(case)
    return chat_report(case.verdict, case.invoice, case.ctx)


@mcp.tool()
@_safe
def onboard_vendor(vendor_name: str, website: str, claimed_country: Optional[str] = None,
                   claimed_headcount: Optional[int] = None, claimed_offices: Optional[int] = None) -> str:
    """Mode 2 vendor onboarding (KYB): is this a real, trading business, who runs it, how long has it existed,
    and is anything off? Returns a Vendor Trust Report: APPROVE / REVIEW / REJECT."""
    return render_onboarding(onboard(vendor_name, website, claimed_country, claimed_headcount, claimed_offices))


@mcp.tool()
@_safe
def pay_invoice(case_id: str) -> str:
    """Release a SIMULATED payment through the gate. Allowed only for PAY cases or HOLD cases a human approved;
    otherwise refused with the decision rule and top evidence. No real money ever moves."""
    return gate.pay_invoice(case_id)


@mcp.tool()
@_safe
def resolve_case(case_id: str, decision: str, reviewer: str, note: str = "") -> str:
    """Human decision on a case: 'approve' or 'reject' a HOLD; 'override' a wrong BLOCK (note required, audited).
    Only call this when a human reviewer has actually made this decision."""
    return gate.resolve_case(case_id, decision, reviewer, note)


@mcp.tool()
@_safe
def case_status(case_id: str) -> str:
    """Current state of a case: decision, rule, risk, confidence, approvals."""
    return gate.case_status(case_id)


if __name__ == "__main__":
    mcp.run()
