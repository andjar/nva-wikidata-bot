"""Test fixtures. NVA shapes are taken from the public API and the NVA search README."""
import copy

from nvabot import wbjson as wb

NVA_ITEM = "Q999"

PERSON = {
    "@context": "https://bibsysdev.github.io/src/person-context.json",
    "id": "https://api.nva.unit.no/cristin/person/15120",
    "type": "Person",
    "identifiers": [{"type": "ORCID", "value": "0000-0002-1825-0097"},
                    {"type": "CristinIdentifier", "value": "15120"}],
    "names": [{"type": "LastName", "value": "Bos-Haugen"}, {"type": "FirstName", "value": "Ida Kristin"}],
    "affiliations": [
        {"type": "Affiliation", "organization": "https://api.nva.unit.no/cristin/organization/185.53.44.12",
         "active": True, "role": {"type": "Role", "labels": {"en": "Professor"}}},
        {"type": "Affiliation", "organization": "https://api.nva.unit.no/cristin/organization/1990.3.0.0",
         "active": False, "role": {"type": "Role", "labels": {"en": "Research fellow"}}}],
    "verified": True,
}

RECORD = {
    "type": "Publication",
    "identifier": "018b857b77b7-697ebc73-5195-4ce4-9ba1-1d5a7b540642",
    "status": "PUBLISHED",
    "handle": "https://hdl.handle.net/11250/3093139",
    "additionalIdentifiers": [{"type": "HandleIdentifier", "sourceName": "ntnu@194.0.0.0",
                               "value": "https://hdl.handle.net/11250/2222222"}],
    "entityDescription": {
        "mainTitle": "A Simple Model for the Variability of Release Area Size",
        "language": "http://lexvo.org/id/iso639-3/eng",
        "publicationDate": {"type": "PublicationDate", "day": "24", "month": "08", "year": "2023"},
        "reference": {"type": "Reference", "doi": "https://doi.org/10.1371/journal.pone.0047855",
                      "publicationContext": {"type": "Journal", "name": "PLOS ONE", "onlineIssn": "1932-6203"},
                      "publicationInstance": {"type": "AcademicArticle", "volume": "7", "issue": "11",
                                              "pages": {"type": "Range", "begin": "e47855", "end": ""}}},
        "contributors": [
            {"type": "Contributor", "identity": {"id": "https://api.nva.unit.no/cristin/person/11111",
                                                 "type": "Identity", "name": "Dieter Issler"},
             "role": {"type": "Creator"}, "sequence": 1},
            {"type": "Contributor", "identity": {"id": "https://api.nva.unit.no/cristin/person/15120",
                                                 "type": "Identity", "name": "Ida Kristin Bos-Haugen"},
             "role": {"type": "Creator"}, "sequence": 2},
            {"type": "Contributor", "identity": {"type": "Identity", "name": "Unlinked Person"},
             "role": {"type": "Creator"}, "sequence": 3},
        ],
    },
}

_counter = [0]


def claim(snak, rank="normal", qualifiers=(), references=()):
    _counter[0] += 1
    st = wb.statement(snak, qualifiers, references)
    st["rank"] = rank
    st["id"] = f"Q1${_counter[0]:08d}"
    return st


def entity(qid="Q1", claims=(), labels=None):
    e = {"id": qid, "lastrevid": 123, "labels": labels or {}, "claims": {}}
    for c in claims:
        e["claims"].setdefault(c["mainsnak"]["property"], []).append(c)
    return e


def person_entity(extra=()):
    return entity("Q1", [claim(wb.item_snak("P31", "Q5")), claim(wb.string_snak("P2287", "15120")), *extra])


def work_entity(extra=(), title=True):
    base = [claim(wb.item_snak("P31", "Q13442814")),
            claim(wb.string_snak("P356", "10.1371/JOURNAL.PONE.0047855"))]
    if title:
        base.append(claim(wb.monolingual_snak("P1476", "A simple model for the variability of release area size",
                                              "en")))
    return entity("Q100", [*base, *extra])


def record(**changes):
    r = copy.deepcopy(RECORD)
    r.update(changes)
    return r
