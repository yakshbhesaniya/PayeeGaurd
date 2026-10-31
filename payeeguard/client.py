"""Crustdata client: one place for headers, cache, retries, rate limits and call counting (spec section 6).

Every response (including 403 and 404) is cached on disk, so re-runs are free and the demo works offline.
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from collections import defaultdict, deque
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
TIMEOUT_S = 10.0
BACKOFF_429 = (2, 4, 8)
RATE_LIMITS = {"/company/enrich": 15, "/person/search": 30, "/web/search/live": 30, "/web/enrich/live": 30}
# Approximate credits per successful call, for the report footer (exact costs depend on the plan).
CREDIT_ESTIMATE = {"/company/identify": 0.0, "/company/enrich": 2.0, "/person/search": 0.5,
                   "/web/enrich/live": 1.0, "/web/search/live": 1.0, "/watch/company": 0.0}


class CrustdataError(Exception):
    """Base error; every check turns it into an `unchecked` Evidence row."""


class NotEnabled(CrustdataError):
    """403: endpoint or field not enabled on our key (never retried)."""


class OfflineMiss(CrustdataError):
    """PAYEEGUARD_OFFLINE=1 and the response is not in the cache."""


class ApiError(CrustdataError):
    """Timeout, 5xx, 429 after retries, bad key, credits exhausted or malformed response."""


class BadRequest(ApiError):
    """400: e.g. Crustdata rejects an invalid domain ("Invalid company domains: x.example") = no match."""


DOH_PROVIDERS = ("https://cloudflare-dns.com/dns-query?name={name}&type={type}",
                 "https://dns.google/resolve?name={name}&type={type}")


def _cache_key(method: str, url: str, body: Any) -> str:
    raw = method + " " + url + " " + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_read(key: str) -> dict | None:
    path = config.cache_dir() / f"{key}.json"
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, entry: dict) -> None:
    d = config.cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file + rename, so concurrent runs never read half a file (edge case M6).
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(entry, f)
    os.replace(tmp, d / f"{key}.json")


class CrustdataClient:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key if api_key is not None else config.env("CRUSTDATA_API_KEY")
        self._http = httpx.Client(timeout=TIMEOUT_S, transport=transport, follow_redirects=True)
        self._recent: dict[str, deque] = defaultdict(deque)
        self._denied: set[str] = set()
        self.calls = 0          # live network calls
        self.cache_hits = 0
        self.credits = 0.0

    # ---- public API -------------------------------------------------------------------------------
    def post(self, path: str, body: dict) -> Any:
        return self._crustdata("POST", path, body)

    def get(self, path: str) -> Any:
        return self._crustdata("GET", path, None)

    def http_get_cached(self, url: str, headers: dict | None = None) -> tuple[int, Any]:
        """Cached GET for free non-Crustdata sources (RDAP, DNS-over-HTTPS). Returns (status, json|None)."""
        key = _cache_key("GET", url, None)
        hit = _cache_read(key)
        if hit is not None:
            self.cache_hits += 1
            return hit["status"], hit.get("data")
        if config.offline():
            raise OfflineMiss(url)
        try:
            r = self._http.get(url, headers=headers or {"accept": "application/json"})
        except httpx.HTTPError as e:
            raise ApiError(f"{url}: {type(e).__name__}") from e
        data = None
        try:
            data = r.json()
        except ValueError:
            pass
        if r.status_code < 500:
            _cache_write(key, {"status": r.status_code, "data": data})
        return r.status_code, data

    def dns(self, name: str, rtype: str = "MX") -> dict | None:
        """DNS-over-HTTPS with provider fallback (dns.google is blocked on some networks)."""
        last: Exception | None = None
        for tmpl in DOH_PROVIDERS:
            try:
                status, data = self.http_get_cached(tmpl.format(name=name, type=rtype),
                                                    headers={"accept": "application/dns-json"})
                if status == 200 and isinstance(data, dict):
                    return data
            except OfflineMiss:
                raise
            except ApiError as e:
                last = e
        if last:
            raise last
        return None

    # ---- internals --------------------------------------------------------------------------------
    def _crustdata(self, method: str, path: str, body: dict | None) -> Any:
        url = BASE_URL + path
        key = _cache_key(method, url, body)
        hit = _cache_read(key)
        if hit is not None:
            self.cache_hits += 1
            if hit["status"] == 403:
                raise NotEnabled(f"{path} not enabled on our key")
            if hit["status"] == 400:
                raise BadRequest(f"{path} HTTP 400: {hit['data']}")
            return hit["data"]
        if config.offline():
            raise OfflineMiss(path)
        if not self.api_key:
            raise ApiError("CRUSTDATA_API_KEY missing")
        if path in self._denied:
            raise NotEnabled(f"{path} not enabled on our key")

        r = self._send(method, url, path, body)
        if r.status_code == 403:
            self._denied.add(path)
            _cache_write(key, {"status": 403, "data": None})
            raise NotEnabled(f"{path} not enabled on our key")
        if r.status_code == 401:
            raise ApiError("Crustdata key invalid (401)")
        if r.status_code == 402 or "credit" in r.text[:300].lower() and r.status_code >= 400:
            raise ApiError("Crustdata credits exhausted")
        if r.status_code == 400:
            _cache_write(key, {"status": 400, "data": r.text[:300]})
            raise BadRequest(f"{path} HTTP 400: {r.text[:200]}")
        if r.status_code >= 400:
            raise ApiError(f"{path} HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise ApiError(f"{path}: malformed JSON") from e
        self.credits += CREDIT_ESTIMATE.get(path, 0.0)
        _cache_write(key, {"status": 200, "data": data})
        return data

    def _send(self, method: str, url: str, path: str, body: dict | None) -> httpx.Response:
        headers = {"authorization": f"Bearer {self.api_key}", "x-api-version": API_VERSION,
                   "content-type": "application/json"}
        retried_5xx = False
        backoffs = list(BACKOFF_429)
        while True:
            self._throttle(path)
            try:
                self.calls += 1
                r = self._http.request(method, url, headers=headers, json=body)
            except httpx.HTTPError as e:
                if retried_5xx:
                    raise ApiError(f"{path}: {type(e).__name__}") from e
                retried_5xx = True
                continue
            if r.status_code == 429 and backoffs:
                wait = backoffs.pop(0)
                try:
                    wait = max(wait, int(r.headers.get("retry-after", "0")))
                except ValueError:
                    pass
                log.warning("429 on %s; retrying in %ss", path, wait)
                time.sleep(wait)
                continue
            if r.status_code == 429:
                raise ApiError(f"{path}: rate limited (429) after retries")
            if r.status_code >= 500 and not retried_5xx:
                retried_5xx = True
                continue
            return r

    def _throttle(self, path: str) -> None:
        limit = RATE_LIMITS.get(path)
        if not limit:
            return
        q = self._recent[path]
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            time.sleep(60 - (now - q[0]) + 0.1)
        q.append(time.monotonic())
