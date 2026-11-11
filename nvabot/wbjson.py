"""Helpers for building and reading Wikibase entity JSON."""
from __future__ import annotations

from datetime import date
from typing import Iterable, List, Optional, Set

GREGORIAN = "http://www.wikidata.org/entity/Q1985727"


# --------------------------------------------------------------------------- building
def _snak(prop: str, value, vtype: str) -> dict:
    return {"snaktype": "value", "property": prop, "datavalue": {"value": value, "type": vtype}}


def string_snak(prop: str, value: str) -> dict:
    return _snak(prop, value, "string")


def item_snak(prop: str, qid: str) -> dict:
    return _snak(prop, {"entity-type": "item", "numeric-id": int(qid[1:]), "id": qid}, "wikibase-entityid")


def time_snak(prop: str, time: str, precision: int) -> dict:
    return _snak(prop, {"time": time, "timezone": 0, "before": 0, "after": 0,
                        "precision": precision, "calendarmodel": GREGORIAN}, "time")


def monolingual_snak(prop: str, text: str, lang: str) -> dict:
    return _snak(prop, {"text": text, "language": lang}, "monolingualtext")


def somevalue_snak(prop: str) -> dict:
    """'unknown value' - e.g. an end time we know exists but not when."""
    return {"snaktype": "somevalue", "property": prop}


def quantity_snak(prop: str, amount: int) -> dict:
    return _snak(prop, {"amount": f"+{int(amount)}", "unit": "1"}, "quantity")


def day_time(d: date) -> str:
    return f"+{d.isoformat()}T00:00:00Z"


def _group(snaks: Iterable[dict]):
    grouped, order = {}, []
    for s in snaks:
        p = s["property"]
        if p not in grouped:
            grouped[p] = []
            order.append(p)
        grouped[p].append(s)
    return grouped, order


def reference(snaks: Iterable[dict]) -> dict:
    grouped, order = _group(snaks)
    return {"snaks": grouped, "snaks-order": order}


def statement(mainsnak: dict, qualifiers: Iterable[dict] = (), references: Iterable[dict] = ()) -> dict:
    st = {"mainsnak": mainsnak, "type": "statement", "rank": "normal"}
    qualifiers = list(qualifiers)
    if qualifiers:
        st["qualifiers"], st["qualifiers-order"] = _group(qualifiers)
    refs = list(references)
    if refs:
        st["references"] = refs
    return st


# --------------------------------------------------------------------------- reading
def claims(entity: dict, prop: str) -> List[dict]:
    return (entity.get("claims") or {}).get(prop, [])


def snak_value(snak: Optional[dict]):
    if not snak or snak.get("snaktype") != "value":
        return None
    dv = snak.get("datavalue") or {}
    v = dv.get("value")
    if dv.get("type") == "wikibase-entityid" and isinstance(v, dict):
        return v.get("id") or f"Q{v.get('numeric-id')}"
    return v


def main_value(claim: dict):
    return snak_value(claim.get("mainsnak"))


def qualifier_values(claim: dict, prop: str) -> list:
    return [snak_value(s) for s in (claim.get("qualifiers") or {}).get(prop, [])]


def reference_values(ref: dict, prop: str) -> list:
    return [snak_value(s) for s in (ref.get("snaks") or {}).get(prop, [])]


def item_values(entity: dict, prop: str, include_deprecated: bool = False) -> Set[str]:
    out = set()
    for c in claims(entity, prop):
        if c.get("rank") == "deprecated" and not include_deprecated:
            continue
        v = main_value(c)
        if isinstance(v, str):
            out.add(v)
    return out


def labels(entity: dict) -> List[str]:
    return [v.get("value", "") for v in (entity.get("labels") or {}).values()]
