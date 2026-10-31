"""Offline test harness: a mock Crustdata/DNS/RDAP transport behind the real client."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payeeguard import config  # noqa: E402
from payeeguard.client import CrustdataClient  # noqa: E402
from payeeguard.graph import Graph  # noqa: E402

FW = 6042422
FRESHWORKS = {
    "crustdata_company_id": FW,
    "basic_info": {"name": "Freshworks", "crustdata_company_id": FW, "primary_domain": "freshworks.com",
                   "all_domains": ["freshworks.com"], "website": "http://freshworks.com", "year_founded": 2010,
                   "company_type": "Public Company"},
    "headcount": {"total": 7859, "growth_percent": {"yoy": 3}},
    "locations": {"country": "USA", "headquarters": "San Mateo, California, United States",
                  "all_office_addresses": ["Bellevue, Washington, US", "Chennai, Tamil Nadu, IN",
                                           "79 Hatton Garden, London, England, EC1N 8JS, GB"]},
    "web_traffic": {"domain_traffic": {"monthly_visitors": 1000000}},
    "funding": {"phone_number": "+1 (855) 747 6767", "contact_email": "support@freshworks.com", "num_funding_rounds": 9},
    "hiring": {"openings_count": 11},
    "people": {"founders": [{"crustdata_person_id": 1, "basic_profile": {"name": "Founder One"},
                             "experience": {"employment_details": {"current": [
                                 {"crustdata_company_id": FW, "title": "CEO", "start_date": "2010-01-01"}]}}}],
               "cxos": []},
}


def match(data, conf=1.0, key="company_data"):
    return [{"matched_on": "x", "match_type": "domain", "matches": [{"confidence_score": conf, key: data}]}]


class Mock:
    """Routes requests; tests mutate .people, .status_overrides etc."""

    def __init__(self):
        self.people: list[dict] = []
        self.overrides: dict[str, tuple[int, object]] = {}
        self.calls: list[str] = []
        self.dns_status = 0
        self.rdap: tuple[int, object] = (200, {"events": [{"eventAction": "registration",
                                                           "eventDate": "2010-01-01T00:00:00Z"}]})
        self.extra_domains: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path in self.overrides:
            code, body = self.overrides[path]
            return httpx.Response(code, json=body) if not isinstance(body, str) else httpx.Response(code, text=body)
        host = request.url.host
        if host in ("cloudflare-dns.com", "dns.google"):
            ans = [] if self.dns_status else [{"data": "10 mx.example."}]
            return httpx.Response(200, json={"Status": self.dns_status, "Answer": ans} if ans else {"Status": self.dns_status})
        if host == "rdap.org":
            return httpx.Response(self.rdap[0], json=self.rdap[1])
        body = json.loads(request.content or b"{}")
        if path == "/company/identify":
            dom = (body.get("domains") or [None])[0]
            name = (body.get("names") or [""])[0].lower()
            if dom == "freshworks.com" or "freshworks" in name:
                return httpx.Response(200, json=match(FRESHWORKS))
            if dom in self.extra_domains:
                return httpx.Response(200, json=match(self.extra_domains[dom]))
            return httpx.Response(200, json=[{"matched_on": dom or name, "matches": []}])
        if path == "/company/enrich":
            return httpx.Response(200, json=match(FRESHWORKS))
        if path == "/person/identify":
            return httpx.Response(403, json={"error": {"type": "permission_error"}})
        if path == "/person/search":
            return httpx.Response(200, json={"profiles": self.people, "total_count": len(self.people)})
        if path == "/web/enrich/live":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": "route"})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYEEGUARD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("PAYEEGUARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAYEEGUARD_OFFLINE", "0")
    monkeypatch.setenv("PAYEEGUARD_FP_SECRET", "test-secret")
    monkeypatch.setenv("CRUSTDATA_API_KEY", "test-key")
    monkeypatch.setattr(config, "reports_dir", lambda: tmp_path / "reports")
    monkeypatch.setattr("time.sleep", lambda s: None)
    return tmp_path


@pytest.fixture
def mock():
    return Mock()


@pytest.fixture
def client(env, mock):
    return CrustdataClient(api_key="test-key", transport=httpx.MockTransport(mock))


@pytest.fixture
def graph(env):
    g = Graph(env / "graph.db")
    yield g
    g.close()


def seed_freshworks(graph, n=14, account="000123456789", code="CHASUS33"):
    fp = graph.fingerprint(account=account, code=code)
    comp = graph.node(f"company:{FW}", "company", "Freshworks")
    graph.edge(comp, "OWNS_DOMAIN", graph.node("domain:freshworks.com", "domain"), "test")
    for i in range(n):
        graph.record_payment(f"seed-{i}", comp, f"INV-2026-{1029 + i}", 4200.0, "USD", fp, f"2026-0{1 + i % 9}-02",
                             "test", "freshworks.com")
    return fp


def email(sender="Freshworks Billing <billing@freshworks.com>", body="", extra_headers=""):
    return (f"From: {sender}\n{extra_headers}Subject: Invoice INV-2026-1043\n\n"
            f"Please pay invoice INV-2026-1043 for USD 4,200.00.\nVendor: Freshworks Inc, freshworks.com\n{body}")
