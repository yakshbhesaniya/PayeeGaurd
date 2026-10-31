"""Shared offline fixtures: a fake Crustdata client, a seeded graph and the sample invoices
(fields as the extractor would produce them for samples/01-10)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from payeeguard.graph import Graph, case_id_for, load_seed  # noqa: E402
from payeeguard.models import Invoice  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-secret-0123456789abcdef0123456789abcdef"
FRESHWORKS, ZOHO = 6042422, 1032449


class NotEnabled(Exception):
    """Same class name as Yaksh's client raises on 403."""


class OfflineMiss(Exception):
    """Same class name as Yaksh's client raises on a cache miss in offline mode."""


class FakeClient:
    """`post(path, body)` answered by per-path handlers; records every call."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict) -> Any:
        self.calls.append((path, body))
        handler = self.routes.get(path)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(body)
        if handler is None:
            raise OfflineMiss(f"no fixture for {path}")
        return handler

    def count(self, path: str) -> int:
        return sum(1 for p, _ in self.calls if p == path)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def person(key: str) -> dict:
    return fixture("people.json")[key]


def identify_client() -> FakeClient:
    data = fixture("identify.json")
    return FakeClient({"/company/identify": lambda body: data.get(body["domains"][0], [{"matches": []}])})


def search_returning(*profiles: dict) -> Callable[[dict], dict]:
    return lambda body: {"profiles": list(profiles), "total_count": len(profiles), "next_cursor": None}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", SECRET)


@pytest.fixture
def graph(tmp_path: Path) -> Graph:
    g = Graph(tmp_path / "graph.db")
    yield g
    g.close()


@pytest.fixture
def seeded(graph: Graph) -> Graph:
    load_seed(graph, identify_client())
    return graph


def sample_text(name: str) -> str:
    return (ROOT / "samples" / name).read_text(encoding="utf-8")


def inv(**kw: Any) -> Invoice:
    base: dict[str, Any] = dict(vendor_name=None, vendor_domain=None, sender_email=None, sender_name=None,
                                sender_title=None, remit_country=None, vendor_address=None, amount=None,
                                currency=None)
    base.update(kw)
    return Invoice(**base)


# Extracted fields for the samples (what extract.py yields; Bahul's code). ctx carries C1's company id.
SAMPLES: dict[str, tuple[dict, dict]] = {
    "01_legit.txt": (dict(vendor_name="Freshworks Inc", vendor_domain="freshworks.com",
                          sender_email="billing@freshworks.com", sender_name="Freshworks Billing",
                          amount=4200.0, currency="USD", invoice_number="INV-2026-1043",
                          bank_swift="CHASUS33", bank_account="000123456789", remit_country="US"),
                     {"company_id": FRESHWORKS}),
    "02_lookalike.txt": (dict(vendor_name="Freshworks Inc", sender_email="rohan.verma@freshw0rks-billing.com",
                              sender_name="Rohan Verma", sender_title="Accounts Receivable", amount=18400.0,
                              currency="USD", invoice_number="INV-2026-1043", bank_swift="FCBKNGLA",
                              bank_change_requested=True, urgency_language=True), {}),
    "03_gmail_signatory.txt": (dict(vendor_name="Freshworks", vendor_domain="freshworks.com",
                                    sender_email="kavya.iyer.finance@gmail.com", sender_name="Kavya Iyer",
                                    sender_title="Finance Manager", amount=345000.0, currency="INR",
                                    invoice_number="FW-88213", remit_country="IN", bank_change_requested=True),
                               {"company_id": FRESHWORKS}),
    "04_new_account_real_domain.txt": (dict(vendor_name="Freshworks Inc", vendor_domain="freshworks.com",
                                            sender_email="billing@freshworks.com", sender_name="Freshworks Billing",
                                            amount=4200.0, currency="USD", invoice_number="INV-2026-1101",
                                            bank_iban="GB33 BUKB 2020 1555 5555 55", bank_swift="BUKBGB22"),
                                       {"company_id": FRESHWORKS}),
    "05_zoho_lookalike_mule.txt": (dict(vendor_name="Zoho Corporation", sender_email="ananya.rao@zoh0-billing.com",
                                        sender_name="Ananya Rao", sender_title="Accounts", amount=210000.0,
                                        currency="INR", invoice_number="ZH-55120",
                                        bank_iban="GB82 WEST 1234 5698 7654 32", bank_swift="WESTGB2L",
                                        bank_change_requested=True), {"company_id": ZOHO}),
    "06_hero_freshworks_mule.txt": (dict(vendor_name="Freshworks Inc", vendor_domain="freshworks.com",
                                         sender_email="billing@freshworks.com", sender_name="Freshworks Billing",
                                         amount=4350.0, currency="USD", invoice_number="INV-2026-1102",
                                         bank_iban="GB82 WEST 1234 5698 7654 32", bank_swift="WESTGB2L"),
                                    {"company_id": FRESHWORKS}),
    "07_unknown_vendor.txt": (dict(vendor_name="Zentrix Global Solutions Pvt Ltd",
                                   vendor_domain="zentrixglobal.example", sender_email="accounts@zentrixglobal.example",
                                   sender_name="Meera Joshi", sender_title="Accounts", amount=185000.0, currency="INR",
                                   invoice_number="ZGS-2026-0042", bank_swift="ICIC0001234",
                                   bank_account="123405000789", bank_change_requested=True), {}),
    "08_prompt_injection.txt": (dict(vendor_name="Freshworks Inc", sender_email="accounts@freshworks-invoices.com",
                                     sender_name="Freshworks Accounts", amount=9800.0, currency="EUR",
                                     invoice_number="INV-2026-1103", bank_iban="DE89 3704 0044 0532 0130 00",
                                     bank_swift="COBADEFFXXX"), {"company_id": FRESHWORKS}),
    "09_duplicate_invoice.txt": (dict(vendor_name="Freshworks Inc", vendor_domain="freshworks.com",
                                      sender_email="billing@freshworks.com", sender_name="Freshworks Billing",
                                      amount=4200.0, currency="USD", invoice_number="INV-2026-1031",
                                      bank_swift="CHASUS33", bank_account="000123456789", remit_country="US"),
                                 {"company_id": FRESHWORKS}),
}


def sample(name: str) -> tuple[Invoice, dict]:
    fields, ctx = SAMPLES[name]
    raw = sample_text(name)
    return inv(raw_text=raw, **fields), dict(ctx, case_id=case_id_for(raw))
