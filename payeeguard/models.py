"""Shared data contracts (spec section 5). Frozen: change only with team agreement."""

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Invoice:
    vendor_name: Optional[str]
    vendor_domain: Optional[str]          # domain claimed in the body, e.g. "freshworks.com"
    sender_email: Optional[str]
    sender_name: Optional[str]
    sender_title: Optional[str]
    remit_country: Optional[str]          # ISO-2
    vendor_address: Optional[str]
    amount: Optional[float]
    currency: Optional[str]               # ISO-4217
    bank_change_requested: bool = False
    urgency_language: bool = False
    raw_text: str = ""
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    reply_to_email: Optional[str] = None
    return_path_email: Optional[str] = None
    bank_iban: Optional[str] = None       # in memory only; never logged or persisted raw
    bank_account: Optional[str] = None    # non-IBAN account number; same rule
    bank_swift: Optional[str] = None      # or IFSC for India
    bank_account_last4: Optional[str] = None


Status = Literal["verified", "contradicted", "unchecked"]
Family = Literal["CHANNEL", "SIGNATORY", "COMPANY", "PAYMENT", "BEHAVIOUR", "INTEGRITY", "CONTEXT"]
Fact = Literal["who", "channel", "money"]


@dataclass
class Evidence:
    check: str                            # e.g. "C2_domain", "G3_account_flagged"
    family: Family
    claim: str                            # "Sender domain belongs to the vendor"
    status: Status
    detail: str                           # what we found, plain English
    source: str                           # citation (section 5.1)
    weight: int = 0                       # risk points, only when contradicted
    hard_block: bool = False
    unchecked_reason: Optional[str] = None  # "api_error" | "not_enabled" | "no_data" | "not_applicable"
    confirms: Optional[Fact] = None       # set on verified rows that confirm a critical fact


@dataclass
class Hypothesis:
    attack_type: str
    title: str
    narrative: str
    supporting: list[str] = field(default_factory=list)


@dataclass
class RuledOut:
    attack_type: str
    because: str
    source: str


@dataclass
class Action:
    kind: Literal["payment_instruction", "watcher", "callback_script", "verification_email_draft",
                  "exception_ticket", "fraud_case_file", "do_not_pay_notice", "kyb_onboarding_task"]
    title: str
    body: str
    path: Optional[str] = None
    simulated: bool = True


@dataclass
class Verdict:
    decision: Literal["PAY", "HOLD", "BLOCK"]
    risk_score: int
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    evidence: list[Evidence] = field(default_factory=list)
    hypothesis: Optional[Hypothesis] = None
    also_matched: list[Hypothesis] = field(default_factory=list)
    ruled_out: list[RuledOut] = field(default_factory=list)
    decision_rule: str = ""
    company_id: Optional[int] = None
    person_id: Optional[int] = None
    callback: Optional[str] = None
    summary: str = ""
    case_id: str = ""
    actions: list[Action] = field(default_factory=list)
    graph_edges: list[dict] = field(default_factory=list)
    crustdata_calls: int = 0
    crustdata_credits: float = 0.0
