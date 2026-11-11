"""Orchestrates a run: discover items, fetch NVA data, plan, edit (or report), record."""
from __future__ import annotations

import csv
import json
import logging
import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from . import stats as statsmod
from .config import Config
from .normalize import handle_key, normalize_doi, normalize_orcid
from .nva import (NvaClient, NvaError, NvaNotPublic, institution_number, normalize_issn, record_doi, record_handles,
                  record_issns)
from dataclasses import replace

from .normalize import title_similarity, wikidata_year
from .planner import (REVIEW_KINDS, Action, Context, CreateSettings, Policy, plan_link, plan_new_work,
                      plan_person, plan_work)
from . import wbjson as wb
from .safety import BudgetExhausted, CircuitOpen, LagStop, Safety, StopRequested
from .state import State
from .wikidata import API_URLS, SparqlClient, WikidataReader, chunks
from .writer import EditConflict, ServerLagged, build_claims_payload

log = logging.getLogger("nvabot.runner")

VERB = {"add_claim": "add", "add_reference": "reference", "update_claim": "update",
        "convert_author_string": "author string→item", "create_item": "create"}
NICE = {"orcid": "ORCID", "doi": "DOI", "handle": "Handle", "nva_person_id": "NVA person ID",
        "publication_date": "publication date", "author": "author", "title": "title", "employer": "employer"}


class Runner:
    def __init__(self, cfg: Config, mode: str, *, live: bool, max_edits: Optional[int] = None,
                 max_lag_wait: Optional[float] = None):
        self.cfg, self.mode, self.live = cfg, mode, live
        bot, lim, wd = cfg["bot"], cfg["limits"], cfg["wikidata"]
        ua = bot["user_agent"]
        self.state = State(cfg.path("state_db"))
        self.nva = NvaClient(cfg["nva"]["api_base"], ua, lim["nva_min_interval_seconds"], cfg["nva"]["page_size"],
                             max_pages=cfg["nva"]["max_pages"])
        self.sparql = SparqlClient(wd["sparql_endpoints"], ua, lim["sparql_min_interval_seconds"])
        self.reader = WikidataReader(self.sparql, API_URLS[bot["site"]], ua, lim["read_maxlag"])
        self.writer = None
        if live:
            from .writer import PywikibotWriter
            self.writer = PywikibotWriter(bot["site"], bot.get("assert_user", True), lim["write_max_retries"])
        self.safety = Safety(self.reader, self.writer, live=live,
                             max_edits=min(max_edits or lim["max_edits_per_run"], lim["max_edits_per_run"]),
                             stop_page=bot["stop_page"], stop_on_talk=bot["stop_on_talk_message"],
                             check_every=lim["stop_check_every"],
                             max_consecutive_failures=lim["max_consecutive_failures"],
                             max_failure_ratio=lim["max_failure_ratio"], stop_file=str(cfg.path("stop_file")),
                             unflagged_max_edits=lim["unflagged_max_edits"],
                             maxlag=(self.writer.maxlag if self.writer and self.writer.maxlag else lim["maxlag"]),
                             lag_poll_seconds=lim["lag_poll_seconds"],
                             max_lag_wait_minutes=(max_lag_wait if max_lag_wait is not None
                                                   else lim["max_lag_wait_minutes"]))
        self._nva_errors_in_row = 0
        self._not_public_in_row = 0
        self.batch_id = secrets.token_hex(5)
        self.run_id = self.state.start_run(mode, live, self.batch_id)
        self.counters: Counter = Counter()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_dir, report_dir = cfg.path("log_dir"), cfg.path("report_dir")
        log_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        self.audit = open(log_dir / f"audit-run{self.run_id}-{stamp}.jsonl", "a", encoding="utf-8")
        self.report_path = report_dir / f"{'live' if live else 'dryrun'}-run{self.run_id}-{mode}-{stamp}.csv"
        self._files = [self.audit]
        self._files.append(open(self.report_path, "w", newline="", encoding="utf-8"))
        self.report = csv.writer(self._files[-1])
        self.report.writerow(["status", "qid", "kind", "property", "value", "reason", "subject", "revid"])
        self.missing_path = report_dir / f"not-on-wikidata-run{self.run_id}-{stamp}.csv"
        self._missing = None
        self.created_this_run = 0
        self.ctx = self._context()

    # ------------------------------------------------------------------ setup
    def _context(self) -> Context:
        cfg, p = self.cfg, self.cfg.props
        nva_item = cfg["wikidata"]["nva_item"] or self.reader.applicable_stated_in(p.nva_person_id,
                                                                                  p.applicable_stated_in)
        if not nva_item:
            raise SystemExit("Could not resolve the NVA item for 'stated in'; set wikidata.nva_item in the config.")
        log.info("references will use stated in (%s) = %s", p.stated_in, nva_item)
        return Context(props=p, policy=cfg.policy, nva_item=nva_item, human_item=cfg["wikidata"]["human_item"],
                       value_owner=self._value_owner, landing_page=cfg["nva"]["landing_page"],
                       person_page=cfg["nva"]["person_page"], author_roles=tuple(cfg["nva"]["author_roles"]),
                       positions={str(k).strip().lower(): v for k, v in (cfg["positions"] or {}).items() if v},
                       create=CreateSettings.from_mapping(cfg["create"]))

    def _value_owner(self, prop: str, value: str):
        p = self.cfg.props
        if prop == p.orcid:
            return self.reader.lookup_values(prop, [value], normalize_orcid, ("main",)).get(value, set())
        if prop == p.doi:
            return self.reader.lookup_values(prop, [value], normalize_doi, ("main", "scholarly")).get(value, set())
        if prop == p.handle:
            return self.reader.lookup_values(prop, [value], handle_key, ("main", "scholarly")).get(value, set())
        if prop == p.nva_person_id:
            return self.reader.lookup_values(prop, [value], lambda v: str(v).strip(), ("main",)).get(value, set())
        return set()

    def _load_person_map(self) -> Dict[str, str]:
        mapping, problems = self.reader.nva_person_map(self.cfg.props.nva_person_id)
        for qid, value, detail in problems:
            self.state.add_review(qid, "conflict", self.cfg.props.nva_person_id, value, detail, "Wikidata")
            self.counters["conflict"] += 1
        self.ctx.person_map = mapping
        if self.cfg.policy.add_employer:
            org_map = self.reader.org_map(self.cfg.props.org_id) if self.cfg.props.org_id else {}
            org_map.update({str(k).split(".")[0]: v for k, v in (self.cfg["org_items"] or {}).items() if v})
            self.ctx.org_map = org_map
            if not org_map and not self.mode.startswith("suggest"):
                log.warning("add_employer is on but no institutions are mapped, so employer/position edits are "
                            "skipped. Run `python -m nvabot suggest-mappings`, check %s and point 'org_items' "
                            "in the config to it.", self.cfg.path("report_dir") / "org-items-suggestions.yaml")
        return mapping

    # ------------------------------------------------------------------ recording
    def _record(self, a: Action, status: str, subject: str, revid: Optional[int] = None, message: str = ""):
        self.counters[a.kind if status in ("planned", "done", "seen") else status] += 1
        self.audit.write(json.dumps({**a.row(), "status": status, "subject": subject, "revid": revid,
                                     "run": self.run_id, "message": message}, ensure_ascii=False) + "\n")
        if a.kind != "skip":
            self.report.writerow([status, a.qid, a.kind, a.prop, a.value, message or a.reason, subject, revid or ""])
        if status in ("done", "failed", "planned"):
            self.state.log_action(self.run_id, a, status, subject, revid, message)
        if a.kind in REVIEW_KINDS:
            self.state.add_review(a.qid, a.kind, a.prop, a.value, a.reason, subject)

    def _note_missing(self, record: dict):
        if self._missing is None:
            self._files.append(open(self.missing_path, "w", newline="", encoding="utf-8"))
            self._missing = csv.writer(self._files[-1])
            self._missing.writerow(["nva_identifier", "doi", "handle", "year", "title"])
        ed = record.get("entityDescription") or {}
        self._missing.writerow([record.get("identifier"), normalize_doi(record_doi(record)) or "",
                                (record_handles(record) or [""])[0],
                                (ed.get("publicationDate") or {}).get("year", ""), ed.get("mainTitle", "")])

    def _summary(self, edits: List[Action], subject: str) -> str:
        parts = Counter(f"{VERB[a.kind]} {NICE.get(self.cfg.props.name_of(a.prop), a.prop)}" for a in edits)
        text = ", ".join(k + (f" ×{n}" if n > 1 else "") for k, n in parts.items())
        s = f"[[{self.cfg['bot']['run_info_page']}|NVA import]]: {text} ({subject})"
        if self.cfg["bot"].get("editgroups"):
            s += f" ([[:toollabs:editgroups/b/CB/{self.batch_id}|details]])"
        return s

    # ------------------------------------------------------------------ the one place that edits
    def process(self, qid: str, entity: Optional[dict], plan: Callable[[dict], List[Action]], subject: str):
        if entity is None:
            entity = self.reader.get_entities([qid]).get(qid)
        if entity is None:
            self._record(Action("conflict", qid, "", subject, "item missing, deleted or redirected"),
                         "seen", subject)
            return
        conflicts, first = 0, True
        while True:
            actions = plan(entity)
            edits = [a for a in actions if a.is_edit]
            if first:
                for a in actions:
                    if not a.is_edit:
                        self._record(a, "seen", subject)
                first = False
            if not edits:
                return
            if not self.live:
                for a in edits:
                    self._record(a, "planned", subject)
                    self._remember(a)
                self.counters["edits"] += 1
                return
            self.safety.before_edit()
            try:
                revid = self.writer.edit_entity(qid, build_claims_payload(entity, edits), entity["lastrevid"],
                                                self._summary(edits, subject))
            except ServerLagged:
                # Not a failure: Wikidata refused the write and nothing changed. Wait, re-read, re-plan.
                self.counters["lag_refusals"] += 1
                self.safety.lag_pause()
                entity = self.reader.get_entities([qid]).get(qid)
                if entity is None:
                    return
                continue
            except EditConflict:
                conflicts += 1
                log.warning("edit conflict on %s, reloading and re-planning", qid)
                entity = self.reader.get_entities([qid]).get(qid)
                if entity is None or conflicts >= 2:
                    for a in edits:
                        self._record(a, "failed", subject, message="edit conflict")
                    self.safety.failure()
                    return
                continue
            except Exception as e:  # noqa: BLE001 - every failure is logged and counted
                log.exception("edit on %s failed", qid)
                for a in edits:
                    self._record(a, "failed", subject, message=f"{type(e).__name__}: {e}"[:300])
                self.safety.failure()
                return
            self.safety.success()
            self.counters["edits"] += 1
            log.info("edited %s rev %s: %s", qid, revid, ", ".join(f"{a.kind} {a.prop}" for a in edits))
            for a in edits:
                self._record(a, "done", subject, revid)
                self._remember(a)
            return

    # ------------------------------------------------------------------ NVA person fetch
    def _fetch_person(self, nid: str, qid: str = "") -> Optional[dict]:
        """Fetch one NVA person; a single bad response never aborts the run.

        Hidden profiles (401/403) are skipped. Many in a row, or repeated other errors, mean NVA is
        down or blocking us - then the run stops instead of hammering the API.
        """
        lim = self.cfg["limits"]
        subject = f"NVA person {nid}"
        try:
            person = self.nva.person(str(nid))
        except NvaNotPublic:
            self._not_public_in_row += 1
            self.counters["nva_profile_not_public"] += 1
            self._record(Action("skip", qid, self.cfg.props.nva_person_id, str(nid),
                                "NVA profile not public (401)"), "seen", subject)
            if self._not_public_in_row >= lim["max_consecutive_not_public"]:
                raise CircuitOpen(f"{self._not_public_in_row} NVA profiles in a row refused (401/403) - "
                                  "is NVA blocking the bot?")
            return None
        except NvaError as e:
            self._nva_errors_in_row += 1
            self.counters["nva_errors"] += 1
            log.warning("NVA error for person %s: %s", nid, e)
            if self._nva_errors_in_row >= lim["max_consecutive_nva_errors"]:
                raise CircuitOpen(f"{self._nva_errors_in_row} NVA errors in a row - NVA may be down")
            return None
        self._nva_errors_in_row = 0
        self._not_public_in_row = 0
        if person is None:
            self._record(Action("conflict", qid, self.cfg.props.nva_person_id, str(nid),
                                "NVA person ID not found in NVA"), "seen", subject)
        return person

    def _remember(self, a: Action):
        """Make values added in this run count for uniqueness checks immediately."""
        p = self.cfg.props
        norm = {p.orcid: normalize_orcid, p.doi: normalize_doi, p.handle: handle_key,
                p.nva_person_id: lambda v: str(v).strip()}.get(a.prop)
        if norm and a.kind == "add_claim" and norm(a.value):
            self.reader.remember(a.prop, norm(a.value), a.qid)

    # ------------------------------------------------------------------ modes
    def run_persons(self, items: Sequence[str] = (), limit: Optional[int] = None, resume: bool = False):
        mapping = self._load_person_map()
        pairs = sorted((int(n), q) for n, q in mapping.items() if n.isdigit())
        if items:
            wanted = set(items)
            pairs = [x for x in pairs if x[1] in wanted]
        elif resume:
            start = int(self.state.get_cursor("persons") or 0)
            pairs = [x for x in pairs if x[0] > start]
        if limit:
            pairs = pairs[:limit]
        log.info("persons: %d items to check", len(pairs))
        for chunk in chunks(pairs, 50):
            entities = self.reader.get_entities([q for _, q in chunk])
            for nid, qid in chunk:
                subject = f"NVA person {nid}"
                person = self._fetch_person(str(nid), qid)
                if person is not None:
                    self.process(qid, entities.get(qid), lambda e, p=person: plan_person(e, p, self.ctx), subject)
                if not items:
                    self.state.set_cursor("persons", nid)
        if not items and not limit:
            self.state.set_cursor("persons", 0)  # full sweep completed

    def run_link_orcid(self, limit: Optional[int] = None):
        mapping = self._load_person_map()
        query = self.cfg["link_by_orcid"]["candidate_query"]
        if not query.strip():
            raise SystemExit("link_by_orcid.candidate_query is empty")
        rows = self.sparql.select(query, "main")
        candidates = sorted({(r["item"], r["orcid"]) for r in rows if r.get("item") and r.get("orcid")})
        if limit:
            candidates = candidates[:limit]
        log.info("link-orcid: %d candidate items", len(candidates))
        for chunk in chunks(candidates, 50):
            entities = self.reader.get_entities([q for q, _ in chunk])
            for qid, orcid in chunk:
                nid = self.nva.find_person_by_orcid(orcid)
                if not nid:
                    self.counters["no_nva_person"] += 1
                    continue
                subject = f"NVA person {nid}"
                if mapping.get(nid) and mapping[nid] != qid:
                    self._record(Action("conflict", qid, self.cfg.props.nva_person_id, nid,
                                        f"NVA person already linked to {mapping[nid]}"), "seen", subject)
                    continue
                person = self._fetch_person(nid, qid)
                if person:
                    self.process(qid, entities.get(qid), lambda e, p=person: plan_link(e, p, self.ctx), subject)

    def run_works(self, persons: Sequence[str] = (), since: Optional[str] = None, incremental: bool = False,
                  limit: Optional[int] = None, create: bool = False):
        mapping = self._load_person_map()
        if create:
            self._validate_create_config()
        started = datetime.now(timezone.utc).date().isoformat()
        if incremental and not since:
            since = self.state.get_cursor("works_modified_since")
        targets = list(persons) or sorted(mapping, key=lambda n: int(n) if n.isdigit() else 0)
        log.info("works: %d persons, modified since %s, create=%s", len(targets), since or "(any time)", create)
        seen, batch = set(), []
        for nid in targets:
            for rec in self.nva.publications_by_person(nid, modified_since=since):
                rid = rec.get("identifier")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                batch.append(rec)
                if len(batch) >= 100:
                    self._works_batch(batch, create=create)
                    batch = []
                if limit and len(seen) >= limit:
                    break
            if limit and len(seen) >= limit:
                break
        if batch:
            self._works_batch(batch, create=create)
        if incremental and not persons and not limit:
            self.state.set_cursor("works_modified_since", started)

    def run_doi_handles(self, institution: Optional[str] = None, since: Optional[str] = None,
                        year_since: Optional[int] = None, incremental: bool = False, limit: Optional[int] = None):
        """Add NVA handles to Wikidata items found by DOI (handle + reference only, nothing else)."""
        started = datetime.now(timezone.utc).date().isoformat()
        cursor_key = f"doi_handles_modified_since:{institution or 'all'}"
        if incremental and not since:
            since = self.state.get_cursor(cursor_key)
        params = {}
        if institution:
            params["top_level_organization"] = f"{self.nva.base}/cristin/organization/{institution}"
        if since:
            params["modified_since"] = since
        if year_since:
            params["publication_year_since"] = year_since
        if not params:
            log.warning("doi-handles without filters walks all of NVA - consider --institution/--since")
        ctx = replace(self.ctx, policy=replace(self.ctx.policy, add_doi=False, add_title=False,
                                                add_publication_date=False, add_authors=False))
        batch, n = [], 0
        for rec in self.nva.search_resources(**params):
            if not (normalize_doi(record_doi(rec)) and record_handles(rec)):
                continue
            batch.append(rec)
            n += 1
            if len(batch) >= 100:
                self._works_batch(batch, ctx=ctx, by_doi_only=True)
                batch = []
            if limit and n >= limit:
                break
        if batch:
            self._works_batch(batch, ctx=ctx, by_doi_only=True)
        if incremental and not limit:
            self.state.set_cursor(cursor_key, started)

    def _works_batch(self, records: List[dict], *, create: bool = False, ctx: Optional[Context] = None,
                     by_doi_only: bool = False):
        p, ctx = self.cfg.props, ctx or self.ctx
        dois = {r["identifier"]: normalize_doi(record_doi(r)) for r in records}
        handles = {r["identifier"]: ([] if by_doi_only else [handle_key(h) for h in record_handles(r)])
                   for r in records}
        endpoints = ("main", "scholarly")
        doi_owner = self.reader.lookup_values(p.doi, [d for d in dois.values() if d], normalize_doi, endpoints)
        handle_owner = self.reader.lookup_values(p.handle, [h for hs in handles.values() for h in hs],
                                                 handle_key, endpoints)
        matched, missing = [], []
        for r in records:
            rid = r["identifier"]
            qids = set(doi_owner.get(dois[rid], set()) if dois[rid] else set())
            for h in handles[rid]:
                qids |= handle_owner.get(h, set())
            created = self.state.created_qid(rid)
            if created:
                qids.add(created)   # created by us earlier; WDQS may not show it yet
            if "NEW" in qids:    # dry run: an item for this DOI/handle is already planned
                self.counters["duplicate_in_run"] += 1
                continue
            if not qids:
                self.counters["work_not_on_wikidata"] += 1
                missing.append(r)
            elif len(qids) > 1:
                self._record(Action("conflict", sorted(qids)[0], "", rid,
                                    f"NVA record matches several items: {', '.join(sorted(qids))}"),
                             "seen", f"NVA {rid}")
            else:
                matched.append((qids.pop(), r))
        entities = self.reader.get_entities([q for q, _ in matched])
        for qid, rec in matched:
            self.process(qid, entities.get(qid), lambda e, r=rec: plan_work(e, r, ctx),
                         f"NVA {rec['identifier']}")
        if create and missing:
            self._create_batch(missing)
        else:
            for r in missing:
                self._note_missing(r)

    # ------------------------------------------------------------------ item creation
    def _validate_create_config(self):
        """Refuse to create items unless every configured class/language QID is sane."""
        cs, work = self.ctx.create, self.cfg["create"]["work_class"]
        if not cs.types:
            raise SystemExit("create.types is empty")
        for nva_type, spec in cs.types.items():
            if not self.sparql.ask(f"ASK {{ wd:{spec['class']} wdt:P279* wd:{work} }}"):
                raise SystemExit(f"create.types.{nva_type}: {spec['class']} is not a subclass of {work}")
        for code, item in cs.languages.items():
            if not self.sparql.ask(f"ASK {{ wd:{item} wdt:P31/wdt:P279* wd:Q34770 }}"):
                raise SystemExit(f"create.languages.{code}: {item} is not a language")
        log.info("create: %d types and %d languages validated", len(cs.types), len(cs.languages))

    def _possible_duplicate(self, record: dict) -> Optional[str]:
        """An existing item with (almost) the same title and year but no DOI/handle match."""
        ed = record.get("entityDescription") or {}
        title = ed.get("mainTitle") or ""
        y = str((ed.get("publicationDate") or {}).get("year") or "").strip()
        year = int(y) if y.isdigit() else 0
        my_doi = normalize_doi(record_doi(record))
        candidates = self.reader.search_titles(title)
        if not candidates:
            return None
        p = self.cfg.props
        for qid, e in self.reader.get_entities(candidates).items():
            if not e:
                continue
            titles = wb.labels(e) + [v.get("text", "") for v in (wb.main_value(c) for c in wb.claims(e, p.title))
                                     if isinstance(v, dict)]
            sim = max([s for s in (title_similarity(title, t) for t in titles) if s is not None] or [0])
            if sim < self.ctx.create.duplicate_title_similarity:
                continue
            years = {wikidata_year(wb.main_value(c)) for c in wb.claims(e, p.publication_date)} - {None}
            if years and year and min(abs(y - year) for y in years) > self.cfg.policy.year_tolerance:
                continue
            their_dois = {normalize_doi(wb.main_value(c)) for c in wb.claims(e, p.doi)} - {None}
            if their_dois and my_doi and my_doi not in their_dois:
                continue  # same title, different paper (e.g. corrigendum)
            return qid
        return None

    def _create_batch(self, records: List[dict]):
        p = self.cfg.props
        issns = sorted({i for r in records for i in record_issns(r)})
        venue_owner = self.reader.lookup_values(p.issn, issns, normalize_issn, ("main",)) if issns else {}
        max_new = self.cfg["create"]["max_per_run"]
        for r in records:
            rid = r["identifier"]
            subject = f"NVA {rid}"
            venues = set().union(*[venue_owner.get(i, set()) for i in record_issns(r)]) if record_issns(r) else set()
            action = plan_new_work(r, self.ctx, next(iter(venues)) if len(venues) == 1 else None)
            if action.kind != "create_item":
                self._record(action, "seen", subject)
                self._note_missing(r)
                continue
            dup = self._possible_duplicate(r)
            if dup:
                self._record(Action("proposal", dup, "", rid, "possible existing item for this NVA record "
                                    "(same title/year, no DOI/handle match); not created"), "seen", subject)
                continue
            if self.created_this_run >= max_new:
                self.counters["create_budget_reached"] += 1
                self._note_missing(r)
                continue
            self._create(action, r, subject)

    def _create(self, action: Action, record: dict, subject: str):
        rid = record["identifier"]
        if not self.live:
            action.qid = "NEW"
            self._record(action, "planned", subject)
            self.created_this_run += 1
            self._remember_record(record, "NEW")
            return
        self.safety.before_edit()
        summary = (f"[[{self.cfg['bot']['run_info_page']}|NVA import]]: {action.reason} from NVA record {rid}")
        if self.cfg["bot"].get("editgroups"):
            summary += f" ([[:toollabs:editgroups/b/CB/{self.batch_id}|details]])"
        while True:
            try:
                qid, revid = self.writer.create_entity(action.claim, summary)
                break
            except ServerLagged:
                # maxlag is checked before anything is saved, so retrying cannot create a duplicate
                self.counters["lag_refusals"] += 1
                self.safety.lag_pause()
                self.safety.before_edit()
            except Exception as e:  # noqa: BLE001
                return self._create_failed(action, rid, subject, e)
        self._created_ok(action, record, rid, subject, qid, revid)

    def _create_failed(self, action: Action, rid: str, subject: str, e: Exception):
        """Called from inside an except block, so log.exception still has the traceback."""
        p = self.cfg.props
        msg = f"{type(e).__name__}: {e}"[:300]
        if "label-with-description-conflict" in msg or "label-conflict" in msg:
            self._record(Action("conflict", "", p.instance_of, rid,
                                "an item with the same label and description exists; not created"),
                         "seen", subject)
            return
        log.exception("creating item for %s failed", rid)
        self._record(action, "failed", subject, message=msg)
        self.safety.failure()

    def _created_ok(self, action: Action, record: dict, rid: str, subject: str, qid: str, revid: int):
        self.safety.success()
        self.created_this_run += 1
        self.counters["edits"] += 1
        self.state.add_created(rid, qid, self.run_id)
        action.qid = qid
        self._record(action, "done", subject, revid)
        self._remember_record(record, qid)
        log.info("created %s for NVA %s", qid, rid)

    def _remember_record(self, record: dict, qid: str):
        p = self.cfg.props
        doi = normalize_doi(record_doi(record))
        if doi:
            self.reader.remember(p.doi, doi, qid)
        for h in record_handles(record):
            self.reader.remember(p.handle, handle_key(h), qid)

    # ------------------------------------------------------------------ helpers for humans
    def run_suggest_mappings(self, limit: Optional[int] = 500):
        """Propose 'org_items' and 'positions' from the NVA profiles of linked people (no edits).

        Writes reports/org-items-suggestions.yaml and reports/positions-suggestions.yaml. A QID is
        only filled in when a Wikidata label matches the NVA name exactly; everything else is null
        with the candidates in a comment, so nothing unchecked can slip into the config.
        """
        mapping = self._load_person_map()
        institutions: Counter = Counter()
        roles: Counter = Counter()
        for nid in sorted(mapping, key=lambda n: int(n) if n.isdigit() else 0)[:limit or None]:
            for aff in (self._fetch_person(nid, mapping[nid]) or {}).get("affiliations") or []:
                if aff.get("active") is not True and not self.cfg.policy.add_former_employers:
                    continue
                num = institution_number(aff.get("organization"))
                if num:
                    institutions[num] += 1
                label = ((aff.get("role") or {}).get("labels") or {}).get("en")
                if label:
                    roles[label.strip()] += 1
        report_dir = self.cfg.path("report_dir")
        self._write_org_suggestions(institutions, report_dir / "org-items-suggestions.yaml")
        self._write_position_suggestions(roles, report_dir / "positions-suggestions.yaml")

    def _candidates(self, names: Dict[str, str]) -> List[dict]:
        """Search Wikidata for each (language, name); exact label matches first."""
        found: Dict[str, dict] = {}
        for lang, name in names.items():
            for h in self.reader.search_entities(name, lang, 5):
                exact = str(h.get("label", "")).strip().lower() == name.strip().lower() or \
                    str((h.get("match") or {}).get("text", "")).strip().lower() == name.strip().lower()
                c = found.setdefault(h["id"], {"id": h["id"], "label": h.get("label", ""),
                                              "description": h.get("description", ""), "exact": False})
                c["exact"] |= exact
        return sorted(found.values(), key=lambda c: not c["exact"])

    @staticmethod
    def _comment(text: str) -> str:
        return " ".join(str(text).split()).replace("#", "")

    def _write_org_suggestions(self, counts: Counter, out: Path):
        known = self.ctx.org_map
        lines = ["# Institutions (NVA/Cristin institution number -> Wikidata item) seen in NVA profiles of",
                 "# people on Wikidata, most frequent first. Open each QID and check it before use.",
                 "# 'null' = no exact name match; pick one of the candidates or leave it null (skipped).",
                 "# Use: point 'org_items' in config.yaml to this file, or copy the lines under 'org_items:'."]
        for num, n in counts.most_common():
            if num in known:
                lines.append(f'"{num}": {known[num]}   # already mapped; {n} affiliations')
                continue
            org = self.nva.institution(num) or {}
            labels = org.get("labels") or {}
            names = {lang: labels[lang] for lang in ("nb", "en", "nn") if labels.get(lang)}
            title = labels.get("en") or labels.get("nb") or labels.get("nn") or "?"
            if org.get("acronym"):
                title += f" ({org['acronym']})"
            cands = self._candidates(names) if names else []
            exact = [c for c in cands if c["exact"]]
            choice = exact[0]["id"] if len(exact) == 1 else "null"
            options = "; ".join(f"{c['id']} = {c['label']} – {c['description']}" for c in cands[:4]) or "none found"
            note = "" if choice != "null" or not exact else " [several exact matches]"
            lines.append(f'"{num}": {choice}   # {self._comment(title)}; {n} affiliations;{note} '
                         f'candidates: {self._comment(options)}')
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("wrote %d institutions to %s", len(counts), out)

    def _write_position_suggestions(self, counts: Counter, out: Path):
        known = self.ctx.positions
        lines = ["# NVA role labels (lower case) -> Wikidata item for the position, most frequent first.",
                 "# Open each QID and check it before use; 'null' = no exact match (skipped).",
                 "# Use: point 'positions' in config.yaml to this file, or copy the lines under 'positions:'."]
        for label, n in counts.most_common():
            key = label.lower()
            if key in known:
                lines.append(f'"{key}": {known[key]}   # already mapped; {n} affiliations')
                continue
            cands = self._candidates({"en": label})
            exact = [c for c in cands if c["exact"]]
            choice = exact[0]["id"] if len(exact) == 1 else "null"
            options = "; ".join(f"{c['id']} = {c['label']} – {c['description']}" for c in cands[:3]) or "none found"
            lines.append(f'"{self._comment(key)}": {choice}   # {n} affiliations; candidates: {self._comment(options)}')
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("wrote %d role labels to %s", len(counts), out)

    def run_handles(self, limit: Optional[int] = None):
        self._load_person_map()
        p = self.cfg.props
        prefixes = self.cfg["wikidata"]["handle_prefixes"]
        filt = " || ".join(f'STRSTARTS(?h, "{x}/")' for x in prefixes)
        q = f"SELECT ?item ?h WHERE {{ ?item p:{p.handle}/ps:{p.handle} ?h . FILTER({filt}) }}"
        rows = self.sparql.select_all(q, ("main", "scholarly"))
        pairs = sorted({(r["item"], r["h"]) for r in rows})
        if limit:
            pairs = pairs[:limit]
        log.info("handles: %d item/handle pairs", len(pairs))
        for chunk in chunks(pairs, 50):
            entities = self.reader.get_entities([q for q, _ in chunk])
            for qid, h in chunk:
                recs = self.nva.find_by_handle(h)
                if not recs:
                    self.counters["handle_not_in_nva"] += 1
                elif len(recs) > 1:
                    self._record(Action("conflict", qid, p.handle, h, "handle matches several NVA records"),
                                 "seen", f"handle {h}")
                else:
                    self.process(qid, entities.get(qid), lambda e, r=recs[0]: plan_work(e, r, self.ctx),
                                 f"NVA {recs[0].get('identifier')}")
                    entities.pop(qid, None)  # force a fresh read if the item has more handles

    # ------------------------------------------------------------------ wrapper
    def execute(self, fn: Callable, *args, **kwargs) -> str:
        status = "completed"
        try:
            self.safety.preflight()
            fn(*args, **kwargs)
        except StopRequested as e:
            status = f"stopped: {e}"
        except BudgetExhausted as e:
            status = f"budget: {e}"
        except CircuitOpen as e:
            status = f"circuit breaker: {e}"
        except LagStop as e:
            status = f"lagged: {e}"
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception as e:  # noqa: BLE001
            log.exception("run aborted")
            status = f"error: {type(e).__name__}: {e}"[:300]
        finally:
            self.counters["nva_requests"] = self.nva.requests_made
            if self.safety.lag_waits:
                self.counters["lag_waits"] = self.safety.lag_waits
            self.state.finish_run(self.run_id, status, dict(self.counters))
            for f in self._files:
                f.close()
            log.info("run %s %s: %s", self.run_id, status, dict(self.counters))
            log.info("report: %s", self.report_path)
            if self.live and self.cfg["stats"]["publish"]:
                publish_stats(self.cfg, self.state, self.writer)
        return status


def publish_stats(cfg: Config, state: State, writer) -> None:
    s = statsmod.collect(state)
    statsmod.write_json(s, cfg.path("report_dir") / "stats.json")
    if writer is None:
        return
    st = cfg["stats"]
    try:
        writer.save_page(st["stats_page"], statsmod.stats_wikitext(s, st["conflicts_page"], cfg["bot"]["source_url"]),
                         "Update NVA import statistics")
        writer.save_page(st["conflicts_page"], statsmod.review_wikitext(state, st["max_conflict_rows"]),
                         "Update NVA import review list")
    except Exception:  # noqa: BLE001 - stats must never break a run
        log.exception("publishing stats failed")
