"""Read-only client for the public NVA API (https://api.nva.unit.no).

Polite by design: one shared session, a minimum interval between requests,
automatic retries with exponential back-off on 429/5xx (honouring
Retry-After), a descriptive User-Agent, and no following of off-site URLs.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator, List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .normalize import handle_key, normalize_handle, normalize_orcid

log = logging.getLogger("nvabot.nva")


class NvaError(RuntimeError):
    pass


# --------------------------------------------------------------------------- record helpers
def person_id_from_uri(uri) -> Optional[str]:
    """'https://api.nva.unit.no/cristin/person/15120' -> '15120'."""
    if uri is None:
        return None
    s = str(uri).strip()
    if s.isdigit():
        return s
    m = re.search(r"/cristin/person/(\d+)/?$", s)
    return m.group(1) if m else None


def institution_number(org_uri) -> Optional[str]:
    """'.../cristin/organization/185.53.44.12' -> '185' (top-level institution)."""
    if not org_uri:
        return None
    m = re.search(r"(\d+)(?:\.\d+){0,3}/?$", str(org_uri))
    return m.group(1) if m else None


def record_doi(record: dict) -> Optional[str]:
    ref = (record.get("entityDescription") or {}).get("reference") or {}
    return ref.get("doi") or record.get("doi")


ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dX]$")


def normalize_issn(value) -> Optional[str]:
    v = str(value or "").strip().upper()
    if re.fullmatch(r"\d{7}[\dX]", v):
        v = f"{v[:4]}-{v[4:]}"
    return v if ISSN_RE.match(v) else None


def record_issns(record: dict) -> List[str]:
    """ISSNs of the journal/series the record was published in (from the expanded context)."""
    ctx = ((record.get("entityDescription") or {}).get("reference") or {}).get("publicationContext") or {}
    out = []
    for key in ("onlineIssn", "printIssn"):
        n = normalize_issn(ctx.get(key))
        if n and n not in out:
            out.append(n)
    return out


def record_handles(record: dict) -> List[str]:
    """Primary NVA handle first, then legacy repository handles."""
    candidates = [record.get("handle")]
    for ai in record.get("additionalIdentifiers") or []:
        value = str(ai.get("value") or "")
        if "handle" in str(ai.get("type", "")).lower() or "hdl.handle.net" in value:
            candidates.append(value)
    out: List[str] = []
    for c in candidates:
        h = normalize_handle(c)
        if h and handle_key(h) not in {handle_key(x) for x in out}:
            out.append(h)
    return out


# --------------------------------------------------------------------------- client
def make_session(user_agent: str, total_retries: int = 5) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=total_retries, connect=3, read=3, status=total_retries, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(["GET", "POST"]),
                  respect_retry_after_header=True, raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = user_agent
    return s


class NvaClient:
    def __init__(self, api_base: str, user_agent: str, min_interval: float = 0.25,
                 page_size: int = 100, timeout: int = 30, max_pages: int = 500,
                 session: Optional[requests.Session] = None):
        self.base = api_base.rstrip("/")
        self._host = urlparse(self.base).netloc
        self.session = session or make_session(user_agent)
        self.min_interval = min_interval
        self.page_size = page_size
        self.timeout = timeout
        self.max_pages = max_pages
        self._last = 0.0
        self.requests_made = 0

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
        if urlparse(url).netloc != self._host:
            raise NvaError(f"refusing to follow off-site URL {url}")
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests_made += 1
        r = self.session.get(url, params=params, timeout=self.timeout,
                             headers={"Accept": "application/json"})
        if r.status_code in (404, 410):
            return None
        if r.status_code >= 400:
            raise NvaError(f"NVA {r.status_code} for {r.url}")
        try:
            data = r.json()
        except ValueError as e:
            raise NvaError(f"non-JSON response from {r.url}") from e
        if not isinstance(data, dict):
            raise NvaError(f"unexpected JSON from {r.url}")
        return data

    # -- organisations
    def institution(self, number: str) -> Optional[dict]:
        """Top-level NVA/Cristin institution, e.g. '194' -> /cristin/organization/194.0.0.0."""
        if not str(number).isdigit():
            raise NvaError(f"invalid institution number {number!r}")
        return self._get(f"{self.base}/cristin/organization/{number}.0.0.0")

    # -- persons
    def person(self, person_id: str) -> Optional[dict]:
        if not str(person_id).isdigit():
            raise NvaError(f"invalid NVA person id {person_id!r}")
        data = self._get(f"{self.base}/cristin/person/{person_id}")
        if data is not None and person_id_from_uri(data.get("id")) != str(person_id):
            # NVA answered with another person (merged profile?) - never guess.
            log.warning("NVA person %s resolved to %s", person_id, data.get("id"))
            return None
        return data

    # -- publications
    def search_page(self, **params) -> dict:
        params.setdefault("aggregation", "none")
        params.setdefault("size", self.page_size)
        return self._get(f"{self.base}/search/resources", params) or {}

    def search_resources(self, **params) -> Iterator[dict]:
        params.setdefault("aggregation", "none")
        params.setdefault("size", self.page_size)
        params.setdefault("sort", "createdDate:asc")
        url: Optional[str] = f"{self.base}/search/resources"
        pages = 0
        while url:
            data = self._get(url, params)
            params = None  # the "next" links already carry all parameters
            if not data:
                return
            hits = data.get("hits") or []
            yield from hits
            if not hits:
                return
            url = data.get("nextSearchAfterResults") or data.get("nextResults")
            pages += 1
            if pages >= self.max_pages:
                log.warning("stopping pagination after %d pages", pages)
                return

    def publications_by_person(self, person_id: str, modified_since: Optional[str] = None) -> Iterator[dict]:
        params = {"contributor": f"{self.base}/cristin/person/{person_id}"}
        if modified_since:
            params["modified_since"] = modified_since
        yield from self.search_resources(**params)

    def find_by_handle(self, handle: str) -> List[dict]:
        """Exact-match lookup. The API's handle search is fuzzy, so hits are re-verified."""
        norm = normalize_handle(handle)
        if not norm:
            return []
        key = handle_key(norm)
        found = {}
        for query in (f"https://hdl.handle.net/{norm}", norm):
            for rec in self.search_page(handle=query, size=10).get("hits") or []:
                if key in {handle_key(h) for h in record_handles(rec)}:
                    found[rec.get("identifier")] = rec
            if found:
                break
        return list(found.values())

    def find_person_by_orcid(self, orcid: str) -> Optional[str]:
        """NVA person id of the contributor carrying `orcid`, if exactly one."""
        orcid = normalize_orcid(orcid)
        if not orcid:
            return None
        ids = set()
        for query in (f"https://orcid.org/{orcid}", orcid):
            for rec in self.search_page(orcid=query, size=10).get("hits") or []:
                for c in (rec.get("entityDescription") or {}).get("contributors") or []:
                    ident = c.get("identity") or {}
                    if normalize_orcid(ident.get("orcId")) == orcid:
                        pid = person_id_from_uri(ident.get("id"))
                        if pid:
                            ids.add(pid)
            if ids:
                break
        return ids.pop() if len(ids) == 1 else None
