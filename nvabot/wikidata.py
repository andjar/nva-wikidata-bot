"""Read access to Wikidata: SPARQL (WDQS) and the Action API (no login needed).

Since the 2025 WDQS graph split, scholarly articles live in a separate graph
(query-scholarly.wikidata.org), so lookups of works query both endpoints.
Entity JSON for planning is always read live from the API, never from WDQS,
so de-duplication is done against the current state of the item.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .nva import make_session

log = logging.getLogger("nvabot.wikidata")

ENTITY_URI = re.compile(r"^https?://www\.wikidata\.org/entity/([QP]\d+)$")
API_URLS = {"wikidata": "https://www.wikidata.org/w/api.php",
            "test": "https://test.wikidata.org/w/api.php"}


def chunks(seq: Sequence, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class SparqlClient:
    def __init__(self, endpoints: Dict[str, str], user_agent: str, min_interval: float = 1.0, timeout: int = 70):
        self.endpoints = endpoints
        self.session = make_session(user_agent)
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0

    def select(self, query: str, endpoint: str = "main") -> List[Dict[str, str]]:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.session.post(self.endpoints[endpoint], data={"query": query, "format": "json"},
                              headers={"Accept": "application/sparql-results+json"}, timeout=self.timeout)
        r.raise_for_status()
        rows = []
        for b in r.json()["results"]["bindings"]:
            row = {}
            for k, v in b.items():
                m = ENTITY_URI.match(v.get("value", ""))
                row[k] = m.group(1) if (m and v.get("type") == "uri") else v.get("value")
            rows.append(row)
        return rows

    def ask(self, query: str, endpoint: str = "main") -> bool:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self.session.post(self.endpoints[endpoint], data={"query": query, "format": "json"},
                              headers={"Accept": "application/sparql-results+json"}, timeout=self.timeout)
        r.raise_for_status()
        return bool(r.json().get("boolean"))

    def select_all(self, query: str, endpoints: Iterable[str]) -> List[Dict[str, str]]:
        out = []
        for ep in endpoints:
            out += self.select(query, ep)
        return out


class WikidataReader:
    def __init__(self, sparql: SparqlClient, api_url: str, user_agent: str, maxlag: Optional[int] = None):
        self.sparql = sparql
        self.api_url = api_url
        self.session = make_session(user_agent)
        self.maxlag = maxlag
        self._value_cache: Dict[Tuple[str, str], Set[str]] = {}

    # -- Action API
    def _api(self, **params) -> dict:
        params.update(format="json", formatversion=2)
        if self.maxlag:   # reads don't need maxlag; writes always use it (Pywikibot write_maxlag)
            params["maxlag"] = self.maxlag
        for attempt in range(6):
            r = self.session.get(self.api_url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            err = data.get("error", {})
            if err.get("code") == "maxlag":
                delay = int(r.headers.get("Retry-After", 5)) * (attempt + 1)
                log.info("maxlag, sleeping %ss", delay)
                time.sleep(delay)
                continue
            if err:
                raise RuntimeError(f"API error {err.get('code')}: {err.get('info')}")
            return data
        raise RuntimeError("API still lagged after retries")

    def get_entities(self, qids: Sequence[str]) -> Dict[str, Optional[dict]]:
        """QID -> entity JSON; None for missing, deleted or redirected items."""
        out: Dict[str, Optional[dict]] = {}
        for chunk in chunks(list(dict.fromkeys(qids)), 50):
            data = self._api(action="wbgetentities", ids="|".join(chunk), props="info|claims|labels")
            for qid in chunk:
                e = (data.get("entities") or {}).get(qid)
                if not e or "missing" in e or e.get("id") != qid or e.get("redirects"):
                    out[qid] = None
                else:
                    out[qid] = e
        return out

    def search_titles(self, title: str, limit: int = 10) -> List[str]:
        """Full-text search of item labels (CirrusSearch) - catches items lacking DOI/handle."""
        text = re.sub(r'["\\]', " ", title).strip()
        if not text:
            return []
        data = self._api(action="query", list="search", srsearch=f'"{text[:250]}"', srnamespace=0,
                         srlimit=limit)
        return [h["title"] for h in data.get("query", {}).get("search", []) if re.fullmatch(r"Q\d+", h["title"])]

    def search_entities(self, text: str, language: str = "en", limit: int = 3) -> List[dict]:
        data = self._api(action="wbsearchentities", search=text, language=language, uselang=language,
                         type="item", limit=limit)
        return data.get("search", [])

    def current_lag(self) -> Tuple[Optional[float], str]:
        """Wikidata's current maxlag value in seconds and what causes it (maxlag=-1 always reports it).

        On Wikidata this includes Query Service update lag, not only database replication.
        Returns (None, "") if it cannot be measured.
        """
        try:
            r = self.session.get(self.api_url, params={"action": "query", "titles": "Q1", "format": "json",
                                                       "formatversion": 2, "maxlag": -1}, timeout=30)
            err = (r.json() or {}).get("error") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("could not measure Wikidata lag: %s", e)
            return None, ""
        if err.get("code") != "maxlag":
            return 0.0, ""
        lag = err.get("lag")
        if lag is None:
            m = re.search(r"([\d.]+) seconds? lagged", str(err.get("info", "")))
            lag = m.group(1) if m else None
        kind = str(err.get("type") or err.get("host") or "")
        return (float(lag) if lag is not None else None), kind

    def page_text(self, title: str) -> Optional[str]:
        data = self._api(action="query", prop="revisions", titles=title, rvprop="content", rvslots="main")
        pages = data.get("query", {}).get("pages") or []
        if not pages or pages[0].get("missing"):
            return None
        return pages[0]["revisions"][0]["slots"]["main"]["content"]

    # -- SPARQL
    def nva_person_map(self, prop: str) -> Tuple[Dict[str, str], List[Tuple[str, str, str]]]:
        """NVA person id -> QID (non-deprecated statements) plus a list of problems."""
        q = f"""SELECT ?item ?id WHERE {{
  ?item p:{prop} ?st . ?st ps:{prop} ?id ; wikibase:rank ?rank .
  FILTER(?rank != wikibase:DeprecatedRank)
}}"""
        rows = self.sparql.select(q, "main")
        by_id: Dict[str, Set[str]] = defaultdict(set)
        by_item: Dict[str, Set[str]] = defaultdict(set)
        for r in rows:
            by_id[r["id"].strip()].add(r["item"])
            by_item[r["item"]].add(r["id"].strip())
        problems = []
        mapping = {}
        for nid, items in by_id.items():
            if len(items) > 1:
                problems.append((sorted(items)[0], nid, f"NVA person ID on several items: {', '.join(sorted(items))}"))
            else:
                mapping[nid] = next(iter(items))
        for item, ids in by_item.items():
            if len(ids) > 1:
                problems.append((item, ", ".join(sorted(ids)), "item has several NVA person IDs"))
        log.info("loaded %d NVA person links (%d problems)", len(mapping), len(problems))
        return mapping, problems

    def lookup_values(self, prop: str, values: Iterable[str], normalizer: Callable[[str], Optional[str]],
                      endpoints: Sequence[str] = ("main",)) -> Dict[str, Set[str]]:
        """Normalised value -> set of QIDs holding it (any rank) - used for uniqueness checks."""
        wanted = {normalizer(v) for v in values} - {None}
        todo = [v for v in wanted if (prop, v) not in self._value_cache]
        variants = sorted({x for v in todo for x in (v, v.lower(), v.upper())})
        found: Dict[str, Set[str]] = defaultdict(set)
        for chunk in chunks(variants, 100):
            vals = " ".join(f'"{_escape(v)}"' for v in chunk)
            q = f"SELECT ?item ?v WHERE {{ VALUES ?v {{ {vals} }} ?item p:{prop}/ps:{prop} ?v . }}"
            for r in self.sparql.select_all(q, endpoints):
                key = normalizer(r["v"])
                if key:
                    found[key].add(r["item"])
        for v in todo:
            self._value_cache[(prop, v)] = set(found.get(v, set()))
        return {v: self._value_cache[(prop, v)] for v in wanted}

    def remember(self, prop: str, value: str, qid: str):
        self._value_cache.setdefault((prop, value), set()).add(qid)

    def applicable_stated_in(self, prop: str, applicable_prop: str) -> Optional[str]:
        rows = self.sparql.select(f"SELECT ?v WHERE {{ wd:{prop} wdt:{applicable_prop} ?v }}", "main")
        vals = sorted({r["v"] for r in rows if str(r.get("v", "")).startswith("Q")})
        return vals[0] if len(vals) == 1 else None

    def org_map(self, org_prop: str) -> Dict[str, str]:
        rows = self.sparql.select(f"SELECT ?item ?id WHERE {{ ?item wdt:{org_prop} ?id }}", "main")
        by_num: Dict[str, Set[str]] = defaultdict(set)
        for r in rows:
            num = str(r["id"]).split(".")[0].strip()
            if num.isdigit():
                by_num[num].add(r["item"])
        return {k: next(iter(v)) for k, v in by_num.items() if len(v) == 1}
