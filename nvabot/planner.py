"""Decide what to change on a Wikidata item, given fresh entity JSON and an NVA record.

This module is pure (no network) so every rule is unit-testable. It returns a
list of Actions; the runner decides whether to execute them (live) or only
report them (dry run).

Duplicate-avoidance rules, applied to every value:
  * values are normalised before comparison (DOI case, URL prefixes, ...);
  * a value already present in ANY rank counts as present - a value someone
    deprecated is never re-added;
  * if the property already holds a *different* value and the property is
    single-valued for this purpose, nothing is written - a conflict is
    reported for humans instead;
  * identifier values already used on *another* item are never added
    (uniqueness), they are reported as possible duplicates;
  * existing statements get an NVA reference added instead of a duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import wbjson as wb
from .normalize import (clean_title, handle_key, lexvo_to_wikidata, names_match,
                        normalize_doi, normalize_handle, normalize_orcid, nva_date,
                        title_similarity, wikidata_year)
from .nva import institution_number, person_id_from_uri, record_doi, record_handles

EDIT_KINDS = {"add_claim", "add_reference", "update_claim", "convert_author_string", "create_item"}
REVIEW_KINDS = {"conflict", "proposal"}


@dataclass
class Action:
    # add_claim | add_reference | update_claim | convert_author_string | create_item
    # | conflict | proposal | skip
    kind: str
    qid: str
    prop: str
    value: str
    reason: str = ""
    claim: Optional[dict] = None       # statement JSON to create (create_item: full entity data)
    claim_id: Optional[str] = None     # existing statement to reference / extend / replace
    reference: Optional[dict] = None
    qualifiers: List[dict] = field(default_factory=list)   # update_claim: qualifiers to add

    @property
    def is_edit(self) -> bool:
        return self.kind in EDIT_KINDS

    def row(self) -> dict:
        return {"kind": self.kind, "qid": self.qid, "prop": self.prop,
                "value": self.value, "reason": self.reason}


@dataclass
class Props:
    nva_person_id: str = "P2287"
    orcid: str = "P496"
    handle: str = "P1184"
    doi: str = "P356"
    title: str = "P1476"
    publication_date: str = "P577"
    author: str = "P50"
    author_name_string: str = "P2093"
    series_ordinal: str = "P1545"
    object_named_as: str = "P1932"
    instance_of: str = "P31"
    employer: str = "P108"
    stated_in: str = "P248"
    reference_url: str = "P854"
    retrieved: str = "P813"
    applicable_stated_in: str = "P9073"
    position_qualifier: str = "P2868"      # 'subject has role' on the employer statement
    end_time: str = "P582"
    published_in: str = "P1433"
    volume: str = "P478"
    issue: str = "P433"
    pages: str = "P304"
    number_of_pages: str = "P1104"
    language_of_work: str = "P407"
    issn: str = "P236"
    org_id: Optional[str] = None

    @classmethod
    def from_mapping(cls, m: dict) -> "Props":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (m or {}).items() if k in known})

    def name_of(self, pid: str) -> str:
        for f in fields(self):
            if getattr(self, f.name) == pid:
                return f.name
        return pid


@dataclass
class Policy:
    require_verified_person: bool = True
    add_orcid: bool = True
    add_employer: bool = True
    add_position: bool = True               # role from NVA as qualifier on the employer statement
    add_former_employers: bool = False      # inactive NVA affiliations, with end time = unknown value
    mark_ended_employment: bool = False     # add end time = unknown value to statements only NVA supports
    add_references_to_existing: bool = True
    add_handle: bool = True
    add_doi: bool = True
    add_title: bool = True
    add_publication_date: bool = True
    add_authors: bool = True
    convert_author_name_strings: bool = True
    add_author_at_free_ordinal: bool = True
    add_handle_when_other_handle_present: bool = False
    title_similarity_threshold: float = 0.8
    year_tolerance: int = 1

    @classmethod
    def from_mapping(cls, m: dict) -> "Policy":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (m or {}).items() if k in known})


@dataclass
class CreateSettings:
    # NVA publicationInstance.type -> {"class": QID, "en": ..., "nb": ..., "venue_required": bool}
    types: Dict[str, dict] = field(default_factory=dict)
    languages: Dict[str, str] = field(default_factory=dict)   # Wikidata language code -> language item
    max_authors: int = 300
    require_linked_author: bool = True
    require_identifier: bool = True
    duplicate_title_similarity: float = 0.9
    label_fallback_language: str = "mul"

    @classmethod
    def from_mapping(cls, m: dict) -> "CreateSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (m or {}).items() if k in known})


@dataclass
class Context:
    props: Props
    policy: Policy
    nva_item: str
    human_item: str = "Q5"
    today: date = field(default_factory=date.today)
    person_map: Dict[str, str] = field(default_factory=dict)   # NVA person id -> QID
    org_map: Dict[str, str] = field(default_factory=dict)      # institution number -> QID
    positions: Dict[str, str] = field(default_factory=dict)    # lower-case NVA role label -> QID
    create: CreateSettings = field(default_factory=CreateSettings)
    # (prop, normalised value) -> set of QIDs already holding it (any rank)
    value_owner: Callable[[str, str], Set[str]] = lambda prop, value: set()
    landing_page: str = "https://nva.sikt.no/registration/{identifier}"
    person_page: str = "https://nva.sikt.no/research-profile/{id}"
    author_roles: Tuple[str, ...] = ("Creator",)


# --------------------------------------------------------------------------- references
def _retrieved(ctx: Context) -> dict:
    return wb.time_snak(ctx.props.retrieved, wb.day_time(ctx.today), 11)


def person_reference(ctx: Context, nva_id: str) -> dict:
    p = ctx.props
    return wb.reference([wb.item_snak(p.stated_in, ctx.nva_item),
                         wb.string_snak(p.nva_person_id, nva_id), _retrieved(ctx)])


def link_reference(ctx: Context, nva_id: str) -> dict:
    p = ctx.props
    return wb.reference([wb.item_snak(p.stated_in, ctx.nva_item),
                         wb.string_snak(p.reference_url, ctx.person_page.format(id=nva_id)),
                         _retrieved(ctx)])


def work_reference(ctx: Context, record: dict) -> dict:
    p = ctx.props
    url = ctx.landing_page.format(identifier=record.get("identifier", ""))
    return wb.reference([wb.item_snak(p.stated_in, ctx.nva_item),
                         wb.string_snak(p.reference_url, url), _retrieved(ctx)])


def has_nva_reference(claim: dict, ctx: Context) -> bool:
    p = ctx.props
    for ref in claim.get("references") or []:
        if ctx.nva_item in wb.reference_values(ref, p.stated_in):
            return True
        if wb.reference_values(ref, p.nva_person_id):
            return True
        for url in wb.reference_values(ref, p.reference_url):
            if isinstance(url, str) and ("nva.sikt.no" in url or "api.nva.unit.no" in url):
                return True
    return False


# --------------------------------------------------------------------------- core rule
def ensure_value(entity: dict, ctx: Context, mainsnak: dict, display: str,
                 norm: Optional[Callable[[str], Optional[str]]], ref: dict, *,
                 single: bool, unique: bool, qualifiers=()) -> List[Action]:
    """Add `mainsnak` unless it (or a conflicting value) is already there."""
    qid, prop = entity["id"], mainsnak["property"]
    norm = norm or (lambda v: v)
    target = norm(wb.snak_value(mainsnak))
    existing = []
    for c in wb.claims(entity, prop):
        v = wb.main_value(c)
        if isinstance(v, str):
            existing.append((norm(v) or v, c))
    for value, c in existing:
        if value == target:
            if c.get("rank") == "deprecated":
                return [Action("skip", qid, prop, display, "value present with deprecated rank - not re-added")]
            if ctx.policy.add_references_to_existing and not has_nva_reference(c, ctx):
                return [Action("add_reference", qid, prop, display, "NVA reference for existing statement",
                               claim_id=c["id"], reference=ref)]
            return [Action("skip", qid, prop, display, "already present")]
    if existing and single:
        others = ", ".join(sorted({str(v) for v, _ in existing}))
        return [Action("conflict", qid, prop, display, f"Wikidata has a different value: {others}")]
    if unique:
        owners = set(ctx.value_owner(prop, target)) - {qid}
        if owners:
            return [Action("conflict", qid, prop, display,
                           f"value already used on {', '.join(sorted(owners))} (possible duplicate item)")]
    return [Action("add_claim", qid, prop, display, "missing on Wikidata",
                   claim=wb.statement(mainsnak, qualifiers, [ref]))]


# --------------------------------------------------------------------------- persons
def _nva_orcids(person: dict) -> Tuple[Set[str], bool]:
    raw = [i.get("value") for i in person.get("identifiers") or []
           if str(i.get("type", "")).upper() == "ORCID"]
    normed = {normalize_orcid(v) for v in raw}
    invalid = None in normed
    normed.discard(None)
    return normed, invalid


def _person_guard(entity: dict, person: dict, ctx: Context) -> Optional[Action]:
    p, qid = ctx.props, entity["id"]
    if ctx.human_item not in wb.item_values(entity, p.instance_of):
        return Action("skip", qid, p.instance_of, "", "item is not instance of human")
    if ctx.policy.require_verified_person and person.get("verified") is not True:
        return Action("skip", qid, p.nva_person_id, str(person.get("id")), "NVA person profile not verified")
    return None


def plan_person(entity: dict, person: dict, ctx: Context) -> List[Action]:
    """Enrich a person item that already carries the NVA person ID."""
    p, qid = ctx.props, entity["id"]
    nva_id = person_id_from_uri(person.get("id"))
    if not nva_id:
        return [Action("skip", qid, p.nva_person_id, "", "NVA record has no person id")]
    linked = {str(v) for v in wb.item_values(entity, p.nva_person_id)}
    if nva_id not in linked:
        return [Action("conflict", qid, p.nva_person_id, nva_id, "item does not carry this NVA person ID")]
    guard = _person_guard(entity, person, ctx)
    if guard:
        return [guard]

    ref = person_reference(ctx, nva_id)
    actions: List[Action] = []

    if ctx.policy.add_orcid:
        orcids, invalid = _nva_orcids(person)
        if invalid:
            actions.append(Action("skip", qid, p.orcid, "", "NVA ORCID failed validation"))
        if len(orcids) == 1:
            orcid = next(iter(orcids))
            actions += ensure_value(entity, ctx, wb.string_snak(p.orcid, orcid), orcid,
                                    normalize_orcid, ref, single=True, unique=True)
        elif len(orcids) > 1:
            actions.append(Action("conflict", qid, p.orcid, ", ".join(sorted(orcids)),
                                  "NVA lists several ORCIDs"))

    if ctx.policy.add_employer and ctx.org_map:
        actions += plan_employment(entity, person, ctx, ref)
    return actions


def _role_qid(aff: dict, ctx: Context) -> Optional[str]:
    labels = (aff.get("role") or {}).get("labels") or {}
    for lang in ("en", "nb", "nn"):
        label = str(labels.get(lang) or "").strip().lower()
        if label and label in ctx.positions:
            return ctx.positions[label]
    return None


def _only_nva_sourced(claim: dict, ctx: Context) -> bool:
    refs = claim.get("references") or []
    return bool(refs) and all(has_nva_reference({"references": [r]}, ctx) for r in refs)


def plan_employment(entity: dict, person: dict, ctx: Context, ref: dict) -> List[Action]:
    """Employer (P108) per institution, with the NVA position as qualifier.

    NVA has no start/end dates. Current affiliations are added without dates (the
    reference's 'retrieved' date says when it was true); former ones only on request,
    with end time = unknown value.
    """
    p, pol, qid = ctx.props, ctx.policy, entity["id"]
    groups: Dict[str, dict] = {}
    for aff in person.get("affiliations") or []:
        org_qid = ctx.org_map.get(institution_number(aff.get("organization")) or "")
        if not org_qid:
            continue
        g = groups.setdefault(org_qid, {"active": False, "roles": [], "old_roles": []})
        active = aff.get("active") is True
        g["active"] |= active
        role = _role_qid(aff, ctx)
        bucket = g["roles"] if active else g["old_roles"]
        if role and role not in bucket:
            bucket.append(role)

    actions: List[Action] = []
    for org_qid, g in groups.items():
        roles = (g["roles"] if g["active"] else g["old_roles"]) if pol.add_position else []
        same = [c for c in wb.claims(entity, p.employer) if wb.main_value(c) == org_qid]
        live = [c for c in same if c.get("rank") != "deprecated"]
        if same and not live:
            actions.append(Action("skip", qid, p.employer, org_qid, "employer present with deprecated rank"))
            continue
        if not live:
            if not g["active"] and not pol.add_former_employers:
                actions.append(Action("skip", qid, p.employer, org_qid, "former employer in NVA; not added"))
                continue
            quals = [wb.item_snak(p.position_qualifier, r) for r in roles]
            if not g["active"]:
                quals.append(wb.somevalue_snak(p.end_time))
            actions.append(Action("add_claim", qid, p.employer, org_qid, "employer missing on Wikidata",
                                  claim=wb.statement(wb.item_snak(p.employer, org_qid), quals, [ref])))
            continue
        open_claims = [c for c in live if p.end_time not in (c.get("qualifiers") or {})]
        if g["active"] and not open_claims:
            actions.append(Action("proposal", qid, p.employer, org_qid,
                                  "NVA lists a current affiliation, Wikidata has an end time"))
            continue
        target = (open_claims or live)[0]
        have = set(wb.qualifier_values(target, p.position_qualifier))
        new_quals = [wb.item_snak(p.position_qualifier, r) for r in roles if r not in have]
        if not g["active"] and target in open_claims:
            if pol.mark_ended_employment and _only_nva_sourced(target, ctx):
                new_quals.append(wb.somevalue_snak(p.end_time))
            else:
                actions.append(Action("proposal", qid, p.employer, org_qid,
                                      "NVA lists this affiliation as ended; Wikidata has no end time"))
        need_ref = pol.add_references_to_existing and not has_nva_reference(target, ctx)
        if new_quals or need_ref:
            # qualifiers taken from NVA always come with an NVA reference
            add_ref = not has_nva_reference(target, ctx)
            what = [q["property"] for q in new_quals] + (["reference"] if add_ref else [])
            actions.append(Action("update_claim", qid, p.employer, org_qid, "add " + ", ".join(what),
                                  claim_id=target["id"], qualifiers=new_quals,
                                  reference=ref if add_ref else None))
        else:
            actions.append(Action("skip", qid, p.employer, org_qid, "already present"))
    return actions


def plan_link(entity: dict, person: dict, ctx: Context) -> List[Action]:
    """Add the NVA person ID to an item whose ORCID matches the NVA profile."""
    p, qid = ctx.props, entity["id"]
    nva_id = person_id_from_uri(person.get("id"))
    if not nva_id:
        return [Action("skip", qid, p.nva_person_id, "", "NVA record has no person id")]
    guard = _person_guard(entity, person, ctx)
    if guard:
        return [guard]
    item_orcids = {normalize_orcid(v) for v in wb.item_values(entity, p.orcid)} - {None}
    nva_orcids, _ = _nva_orcids(person)
    if not item_orcids & nva_orcids:
        return [Action("conflict", qid, p.nva_person_id, nva_id, "ORCID on item does not match NVA profile")]
    return ensure_value(entity, ctx, wb.string_snak(p.nva_person_id, nva_id), nva_id,
                        lambda v: str(v).strip(), link_reference(ctx, nva_id), single=True, unique=True)


# --------------------------------------------------------------------------- works
def metadata_agrees(entity: dict, record: dict, ctx: Context) -> Tuple[bool, str]:
    """Second, independent check after an identifier match (guards against bad DOIs)."""
    p = ctx.props
    ed = record.get("entityDescription") or {}
    nva_title = ed.get("mainTitle") or ""
    wd_titles = [v.get("text", "") for v in (wb.main_value(c) for c in wb.claims(entity, p.title))
                 if isinstance(v, dict)] + wb.labels(entity)
    scores = [s for s in (title_similarity(nva_title, t) for t in wd_titles) if s is not None]
    if scores and max(scores) < ctx.policy.title_similarity_threshold:
        return False, f"title differs (best similarity {max(scores):.2f})"
    nva = nva_date(ed.get("publicationDate"), ctx.today.year + 1)
    nva_year = int(nva[0][1:5]) if nva else None
    wd_years = {wikidata_year(wb.main_value(c)) for c in wb.claims(entity, p.publication_date)
                if c.get("rank") != "deprecated"} - {None}
    if nva_year and wd_years and min(abs(y - nva_year) for y in wd_years) > ctx.policy.year_tolerance:
        return False, f"publication year differs (NVA {nva_year}, Wikidata {sorted(wd_years)})"
    return True, ""


def plan_work(entity: dict, record: dict, ctx: Context) -> List[Action]:
    p, pol, qid = ctx.props, ctx.policy, entity["id"]
    rid = record.get("identifier", "?")
    if record.get("status") not in (None, "PUBLISHED"):
        return [Action("skip", qid, "", rid, f"NVA record status {record.get('status')}")]
    ok, why = metadata_agrees(entity, record, ctx)
    if not ok:
        return [Action("conflict", qid, "", rid, f"matched NVA record {rid} by identifier but {why}")]

    ed = record.get("entityDescription") or {}
    ref = work_reference(ctx, record)
    actions: List[Action] = []

    if pol.add_handle:
        actions += _plan_handle(entity, record, ctx, ref)

    if pol.add_doi:
        doi = normalize_doi(record_doi(record))
        if doi:
            actions += ensure_value(entity, ctx, wb.string_snak(p.doi, doi), doi, normalize_doi, ref,
                                    single=True, unique=True)

    if pol.add_title and not wb.claims(entity, p.title):
        title = clean_title(ed.get("mainTitle"))
        lang = lexvo_to_wikidata(ed.get("language"))
        if title and lang:
            actions.append(Action("add_claim", qid, p.title, title, "missing on Wikidata",
                                  claim=wb.statement(wb.monolingual_snak(p.title, title, lang), (), [ref])))
        elif title:
            actions.append(Action("skip", qid, p.title, title, "title language unknown"))

    if pol.add_publication_date and not wb.claims(entity, p.publication_date):
        t = nva_date(ed.get("publicationDate"), ctx.today.year + 1)
        if t:
            actions.append(Action("add_claim", qid, p.publication_date, t[0], "missing on Wikidata",
                                  claim=wb.statement(wb.time_snak(p.publication_date, *t), (), [ref])))

    if pol.add_authors:
        actions += plan_authors(entity, record, ctx, ref)
    return actions


def _plan_handle(entity: dict, record: dict, ctx: Context, ref: dict) -> List[Action]:
    p, qid = ctx.props, entity["id"]
    primary = normalize_handle(record.get("handle"))
    if not primary:
        return []
    record_keys = {handle_key(h) for h in record_handles(record)}
    existing = {handle_key(wb.main_value(c)): c for c in wb.claims(entity, p.handle)
                if isinstance(wb.main_value(c), str)}
    if handle_key(primary) in existing:
        return ensure_value(entity, ctx, wb.string_snak(p.handle, primary), primary, handle_key, ref,
                            single=False, unique=True)
    if record_keys & set(existing):
        return [Action("skip", qid, p.handle, primary, "item has a legacy handle of the same NVA record")]
    if existing and not ctx.policy.add_handle_when_other_handle_present:
        return [Action("proposal", qid, p.handle, primary, "item has an unrelated handle; NVA handle not added")]
    return ensure_value(entity, ctx, wb.string_snak(p.handle, primary), primary, handle_key, ref,
                        single=False, unique=True)


def plan_authors(entity: dict, record: dict, ctx: Context, ref: dict) -> List[Action]:
    """Add P50 for contributors whose NVA person ID is linked to a Wikidata item."""
    p, qid = ctx.props, entity["id"]
    ed = record.get("entityDescription") or {}
    contributors = [c for c in ed.get("contributors") or []
                    if ((c.get("role") or {}).get("type") in ctx.author_roles)]
    author_items = {}
    for c in wb.claims(entity, p.author):
        v = wb.main_value(c)
        if isinstance(v, str):
            author_items[v] = c
    strings = [(c, wb.main_value(c), (wb.qualifier_values(c, p.series_ordinal) or [None])[0])
               for c in wb.claims(entity, p.author_name_string) if isinstance(wb.main_value(c), str)]
    has_author_list = bool(author_items or strings)
    used_ordinals = {str(o) for c in wb.claims(entity, p.author) + wb.claims(entity, p.author_name_string)
                     for o in wb.qualifier_values(c, p.series_ordinal) if o}

    actions: List[Action] = []
    used_string_claims: Set[str] = set()
    planned_items: Set[str] = set()
    for contrib in sorted(contributors, key=lambda c: c.get("sequence") or 10**6):
        identity = contrib.get("identity") or {}
        nva_id = person_id_from_uri(identity.get("id"))
        author_qid = ctx.person_map.get(nva_id or "")
        name = (identity.get("name") or "").strip()
        if not author_qid:
            continue  # contributor not (uniquely) linked to Wikidata
        if author_qid == qid or author_qid in planned_items:
            continue
        if author_qid in author_items:
            claim = author_items[author_qid]
            if ctx.policy.add_references_to_existing and not has_nva_reference(claim, ctx):
                actions.append(Action("add_reference", qid, p.author, author_qid,
                                      "NVA reference for existing author", claim_id=claim["id"], reference=ref))
            continue
        matches = [s for s in strings if s[0]["id"] not in used_string_claims and names_match(s[1], name)]
        if len(matches) > 1:
            actions.append(Action("conflict", qid, p.author, author_qid,
                                  f"'{name}' matches several author name strings"))
            continue
        if len(matches) == 1:
            s_claim, s_name, s_ord = matches[0]
            quals = [wb.string_snak(p.object_named_as, s_name)]
            if s_ord:
                quals.insert(0, wb.string_snak(p.series_ordinal, str(s_ord)))
            new = wb.statement(wb.item_snak(p.author, author_qid), quals,
                               list(s_claim.get("references") or []) + [ref])
            used_string_claims.add(s_claim["id"])
            planned_items.add(author_qid)
            if ctx.policy.convert_author_name_strings:
                actions.append(Action("convert_author_string", qid, p.author, author_qid,
                                      f"replace author string '{s_name}' (#{s_ord}) with item",
                                      claim=new, claim_id=s_claim["id"]))
            else:
                actions.append(Action("proposal", qid, p.author, author_qid,
                                      f"author string '{s_name}' (#{s_ord}) is {author_qid}"))
            continue
        seq = contrib.get("sequence")
        if not seq:
            actions.append(Action("skip", qid, p.author, author_qid, "no author sequence in NVA"))
            continue
        if has_author_list and not (ctx.policy.add_author_at_free_ordinal and str(seq) not in used_ordinals):
            actions.append(Action("proposal", qid, p.author, author_qid,
                                  f"'{name}' (#{seq}) not found in existing author list and position taken"))
            continue
        quals = [wb.string_snak(p.series_ordinal, str(seq))]
        if name:
            quals.append(wb.string_snak(p.object_named_as, name))
        planned_items.add(author_qid)
        used_ordinals.add(str(seq))
        actions.append(Action("add_claim", qid, p.author, author_qid,
                              "author missing on Wikidata" + (" (free position in author list)"
                                                              if has_author_list else ""),
                              claim=wb.statement(wb.item_snak(p.author, author_qid), quals, [ref])))
    return actions


# --------------------------------------------------------------------------- new items
def _pages(inst: dict) -> Tuple[Optional[str], Optional[int]]:
    """(page range string for P304, page count for P1104)."""
    pages = inst.get("pages") or {}
    begin, end = str(pages.get("begin") or "").strip(), str(pages.get("end") or "").strip()
    if begin:
        return (f"{begin}–{end}" if end and end != begin else begin), None
    count = str(pages.get("pages") or "").strip()
    return None, (int(count) if count.isdigit() and 0 < int(count) < 100000 else None)


def plan_new_work(record: dict, ctx: Context, venue_qid: Optional[str] = None) -> Action:
    """Build a new item for an NVA record (caller has already ruled out existing items)."""
    p, cs = ctx.props, ctx.create
    rid = record.get("identifier", "?")

    def skip(reason: str) -> Action:
        return Action("skip", "", p.instance_of, rid, f"not created: {reason}")

    if record.get("status") not in (None, "PUBLISHED"):
        return skip(f"status {record.get('status')}")
    ed = record.get("entityDescription") or {}
    inst = (ed.get("reference") or {}).get("publicationInstance") or {}
    spec = cs.types.get(inst.get("type") or "")
    if not spec:
        return skip(f"type {inst.get('type')} not enabled for creation")
    title = clean_title(ed.get("mainTitle"))
    if not title:
        return skip("no title")
    date_ = nva_date(ed.get("publicationDate"), ctx.today.year + 1)
    if not date_:
        return skip("no valid publication date")
    doi = normalize_doi(record_doi(record))
    handles = record_handles(record)
    if cs.require_identifier and not (doi or handles):
        return skip("no DOI or handle")
    contributors = sorted([c for c in ed.get("contributors") or []
                           if (c.get("role") or {}).get("type") in ctx.author_roles],
                          key=lambda c: c.get("sequence") or 10**6)
    if len(contributors) > cs.max_authors:
        return skip(f"{len(contributors)} authors (max {cs.max_authors})")
    linked = [c for c in contributors
              if ctx.person_map.get(person_id_from_uri((c.get("identity") or {}).get("id")) or "")]
    if cs.require_linked_author and not linked:
        return skip("no author linked to Wikidata")
    if spec.get("venue_required") and not venue_qid:
        return skip("journal not found on Wikidata (by ISSN)")

    ref = work_reference(ctx, record)
    lang = lexvo_to_wikidata(ed.get("language"))
    year = date_[0][1:5]
    label_lang = lang or cs.label_fallback_language
    data = {"labels": {label_lang: {"language": label_lang, "value": title[:250].strip()}},
            "descriptions": {}}
    if spec.get("en"):
        data["descriptions"]["en"] = {"language": "en", "value": f"{spec['en']} published in {year}"}
    if spec.get("nb"):
        data["descriptions"]["nb"] = {"language": "nb", "value": f"{spec['nb']} publisert i {year}"}

    def st(snak, quals=()):
        return wb.statement(snak, quals, [ref])

    claims = [st(wb.item_snak(p.instance_of, spec["class"]))]
    if lang:
        claims.append(st(wb.monolingual_snak(p.title, title, lang)))
        if cs.languages.get(lang):
            claims.append(st(wb.item_snak(p.language_of_work, cs.languages[lang])))
    claims.append(st(wb.time_snak(p.publication_date, *date_)))
    if doi:
        claims.append(st(wb.string_snak(p.doi, doi)))
    if handles:
        claims.append(st(wb.string_snak(p.handle, handles[0])))
    if venue_qid:
        claims.append(st(wb.item_snak(p.published_in, venue_qid)))
    for key, prop in (("volume", p.volume), ("issue", p.issue)):
        v = str(inst.get(key) or "").strip()
        if v and len(v) <= 50:
            claims.append(st(wb.string_snak(prop, v)))
    page_range, page_count = _pages(inst)
    if page_range:
        claims.append(st(wb.string_snak(p.pages, page_range)))
    if page_count:
        claims.append(st(wb.quantity_snak(p.number_of_pages, page_count)))

    seen_items: Set[str] = set()
    for c in contributors:
        ident = c.get("identity") or {}
        name = (ident.get("name") or "").strip()
        seq = c.get("sequence")
        quals = [wb.string_snak(p.series_ordinal, str(seq))] if seq else []
        aq = ctx.person_map.get(person_id_from_uri(ident.get("id")) or "")
        if aq and aq not in seen_items:
            seen_items.add(aq)
            claims.append(st(wb.item_snak(p.author, aq),
                             quals + ([wb.string_snak(p.object_named_as, name)] if name else [])))
        elif name and not aq:
            claims.append(st(wb.string_snak(p.author_name_string, name), quals))
    data["claims"] = claims
    return Action("create_item", "", p.instance_of, rid, f"create {spec.get('en') or spec['class']}",
                  claim=data)
