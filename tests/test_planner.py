import copy
from datetime import date

from nvabot import wbjson as wb
from nvabot.planner import (Context, CreateSettings, Policy, Props, plan_link, plan_new_work,
                            plan_person, plan_work)
from nvabot.writer import build_claims_payload

from .fixtures import NVA_ITEM, PERSON, claim, entity, person_entity, record, work_entity


def ctx(org_map=None, **policy):
    return Context(props=Props(), policy=Policy(**policy), nva_item=NVA_ITEM, today=date(2026, 11, 4),
                   person_map={"11111": "Q11", "15120": "Q1"}, org_map=org_map or {},
                   positions={"professor": "Q121594", "research fellow": "Q1"})


def kinds(actions):
    return [(a.kind, a.prop) for a in actions]


def nva_ref():
    return [wb.reference([wb.item_snak("P248", NVA_ITEM)])]


# ------------------------------------------------------------------ persons
def test_adds_missing_orcid_with_reference():
    acts = plan_person(person_entity(), PERSON, ctx())
    assert kinds(acts) == [("add_claim", "P496")]
    ref = acts[0].claim["references"][0]
    assert wb.reference_values(ref, "P248") == [NVA_ITEM]
    assert wb.reference_values(ref, "P2287") == ["15120"]
    assert wb.reference_values(ref, "P813")[0]["time"] == "+2026-11-04T00:00:00Z"


def test_existing_orcid_gets_reference_not_duplicate():
    e = person_entity([claim(wb.string_snak("P496", "0000-0002-1825-0097"))])
    acts = plan_person(e, PERSON, ctx())
    assert kinds(acts) == [("add_reference", "P496")]
    payload = build_claims_payload(e, acts)
    assert len(payload) == 1 and payload[0]["id"] == acts[0].claim_id
    assert len(payload[0]["references"]) == 1


def test_existing_orcid_with_nva_reference_is_skipped():
    e = person_entity([claim(wb.string_snak("P496", "0000-0002-1825-0097"), references=nva_ref())])
    assert kinds(plan_person(e, PERSON, ctx())) == [("skip", "P496")]


def test_deprecated_value_is_never_readded():
    e = person_entity([claim(wb.string_snak("P496", "0000-0002-1825-0097"), rank="deprecated")])
    acts = plan_person(e, PERSON, ctx())
    assert kinds(acts) == [("skip", "P496")] and "deprecated" in acts[0].reason


def test_different_orcid_is_conflict():
    e = person_entity([claim(wb.string_snak("P496", "0000-0002-1694-233X"))])
    assert kinds(plan_person(e, PERSON, ctx())) == [("conflict", "P496")]


def test_orcid_used_on_other_item_is_conflict():
    c = ctx()
    c.value_owner = lambda prop, value: {"Q77"}
    acts = plan_person(person_entity(), PERSON, c)
    assert kinds(acts) == [("conflict", "P496")] and "Q77" in acts[0].reason


def test_guards():
    not_human = entity("Q1", [claim(wb.string_snak("P2287", "15120"))])
    assert kinds(plan_person(not_human, PERSON, ctx())) == [("skip", "P31")]
    unverified = dict(PERSON, verified=False)
    assert kinds(plan_person(person_entity(), unverified, ctx())) == [("skip", "P2287")]
    other = entity("Q1", [claim(wb.item_snak("P31", "Q5")), claim(wb.string_snak("P2287", "1"))])
    assert kinds(plan_person(other, PERSON, ctx())) == [("conflict", "P2287")]


ORG = {"185": "Q34433", "1990": "Q2000"}


def employer_actions(e, **policy):
    return [a for a in plan_person(e, PERSON, ctx(org_map=ORG, **policy)) if a.prop == "P108"]


def test_employer_with_position_added():
    acts = employer_actions(person_entity())
    added = [a for a in acts if a.kind == "add_claim"]
    assert [a.value for a in added] == ["Q34433"]                       # former employer not added
    assert wb.snak_value(added[0].claim["qualifiers"]["P2868"][0]) == "Q121594"
    assert ("skip", "P108") in kinds(acts)


def test_former_employer_gets_unknown_end_time():
    acts = employer_actions(person_entity(), add_former_employers=True)
    old = next(a for a in acts if a.value == "Q2000")
    assert old.claim["qualifiers"]["P582"][0]["snaktype"] == "somevalue"


def test_existing_employer_gets_position_and_reference():
    e = person_entity([claim(wb.item_snak("P108", "Q34433"))])
    acts = employer_actions(e)
    upd = [a for a in acts if a.kind == "update_claim"]
    assert len(upd) == 1 and upd[0].reference is not None
    payload = build_claims_payload(e, upd)
    assert wb.snak_value(payload[0]["qualifiers"]["P2868"][0]) == "Q121594"
    assert "P2868" in payload[0]["qualifiers-order"] and len(payload[0]["references"]) == 1


def test_existing_employer_complete_is_skipped_and_end_time_respected():
    full = claim(wb.item_snak("P108", "Q34433"), qualifiers=[wb.item_snak("P2868", "Q121594")],
                 references=nva_ref())
    assert not [a for a in employer_actions(person_entity([full])) if a.is_edit]
    ended = claim(wb.item_snak("P108", "Q34433"), qualifiers=[wb.time_snak("P582", "+2020-00-00T00:00:00Z", 9)])
    assert ("proposal", "P108") in kinds(employer_actions(person_entity([ended])))


def test_employer_off_without_mapping():
    assert not [a for a in plan_person(person_entity(), PERSON, ctx()) if a.prop == "P108"]


def test_link_by_orcid():
    e = entity("Q5000", [claim(wb.item_snak("P31", "Q5")), claim(wb.string_snak("P496", "0000-0002-1825-0097"))])
    acts = plan_link(e, PERSON, ctx())
    assert kinds(acts) == [("add_claim", "P2287")] and acts[0].value == "15120"
    e2 = entity("Q5000", [claim(wb.item_snak("P31", "Q5")), claim(wb.string_snak("P496", "0000-0002-1694-233X"))])
    assert kinds(plan_link(e2, PERSON, ctx())) == [("conflict", "P2287")]


# ------------------------------------------------------------------ works
def test_work_adds_missing_handle_date_and_authors():
    acts = plan_work(work_entity(), record(), ctx())
    k = kinds(acts)
    assert ("add_claim", "P1184") in k
    assert ("add_reference", "P356") in k          # DOI already there -> reference only
    assert ("add_claim", "P577") in k
    assert ("add_claim", "P1476") not in k          # title present -> untouched
    authors = [a for a in acts if a.prop == "P50"]
    assert [a.value for a in authors] == ["Q11", "Q1"]  # unlinked contributor ignored
    quals = authors[1].claim["qualifiers"]
    assert wb.snak_value(quals["P1545"][0]) == "2"
    assert wb.snak_value(quals["P1932"][0]) == "Ida Kristin Bos-Haugen"
    handle = next(a for a in acts if a.prop == "P1184")
    assert handle.value == "11250/3093139"


def test_work_never_duplicates_existing_author_or_legacy_handle():
    e = work_entity([claim(wb.item_snak("P50", "Q11"), references=nva_ref()),
                     claim(wb.string_snak("P1184", "11250/2222222"))])
    acts = plan_work(e, record(), ctx())
    assert not any(a.kind == "add_claim" and a.prop in ("P50", "P1184") and a.value in ("Q11", "11250/3093139")
                   for a in acts)
    assert ("skip", "P1184") in kinds(acts)


def test_author_string_proposal_and_conversion():
    s = claim(wb.string_snak("P2093", "I. K. Bos-Haugen"), qualifiers=[wb.string_snak("P1545", "2")])
    e = work_entity([s])
    acts = plan_work(e, record(), ctx(convert_author_name_strings=False))
    assert ("proposal", "P50") in kinds(acts)
    assert not any(a.kind == "add_claim" and a.value == "Q1" for a in acts)  # string present -> no 2nd entry

    acts = plan_work(copy.deepcopy(e), record(), ctx())     # default: convert
    conv = [a for a in acts if a.kind == "convert_author_string"]
    assert len(conv) == 1 and conv[0].value == "Q1" and conv[0].claim_id == s["id"]
    payload = build_claims_payload(e, conv)
    assert {"id": s["id"], "remove": ""} in payload
    new = payload[0]
    assert wb.snak_value(new["qualifiers"]["P1545"][0]) == "2"
    assert wb.snak_value(new["qualifiers"]["P1932"][0]) == "I. K. Bos-Haugen"


def test_metadata_mismatch_blocks_all_edits():
    e = work_entity(title=False, extra=[claim(wb.monolingual_snak("P1476", "Completely unrelated paper", "en"))])
    acts = plan_work(e, record(), ctx())
    assert kinds(acts) == [("conflict", "")]
    e2 = work_entity([claim(wb.time_snak("P577", "+2010-00-00T00:00:00Z", 9))])
    assert kinds(plan_work(e2, record(), ctx())) == [("conflict", "")]


def test_work_title_added_when_missing():
    acts = plan_work(work_entity(title=False), record(), ctx())
    t = next(a for a in acts if a.prop == "P1476")
    assert t.kind == "add_claim" and t.claim["mainsnak"]["datavalue"]["value"]["language"] == "en"


def test_doi_conflict_and_unpublished():
    e = work_entity()
    e["claims"]["P356"][0]["mainsnak"]["datavalue"]["value"] = "10.9999/OTHER"
    assert ("conflict", "P356") in kinds(plan_work(e, record(), ctx()))
    assert kinds(plan_work(work_entity(), record(status="DRAFT"), ctx()))[0][0] == "skip"


def test_policy_switches_off_everything():
    off = dict(add_handle=False, add_doi=False, add_title=False, add_publication_date=False, add_authors=False)
    assert plan_work(work_entity(), record(), ctx(**off)) == []


def test_author_added_at_free_position_only():
    other = claim(wb.string_snak("P2093", "Someone Else"), qualifiers=[wb.string_snak("P1545", "1")])
    acts = plan_work(work_entity([other]), record(), ctx())
    added = [a for a in acts if a.kind == "add_claim" and a.prop == "P50"]
    assert [a.value for a in added] == ["Q1"]                    # #2 free; #1 (Q11) is taken
    assert ("proposal", "P50") in kinds(acts)
    taken = claim(wb.string_snak("P2093", "Not Her"), qualifiers=[wb.string_snak("P1545", "2")])
    acts = plan_work(work_entity([other, taken]), record(), ctx())
    assert not [a for a in acts if a.kind == "add_claim" and a.prop == "P50"]


# ------------------------------------------------------------------ new items
def create_ctx(**kw):
    c = ctx(**kw)
    c.create = CreateSettings(types={"AcademicArticle": {"class": "Q13442814", "en": "scholarly article",
                                                         "nb": "vitenskapelig artikkel", "venue_required": True}},
                              languages={"en": "Q1860"})
    return c


def test_new_item_contents():
    a = plan_new_work(record(), create_ctx(), venue_qid="Q564954")
    assert a.kind == "create_item"
    data = a.claim
    assert data["labels"]["en"]["value"].startswith("A Simple Model")
    assert data["descriptions"]["en"]["value"] == "scholarly article published in 2023"
    by = {}
    for c in data["claims"]:
        by.setdefault(c["mainsnak"]["property"], []).append(wb.snak_value(c["mainsnak"]))
        assert wb.reference_values(c["references"][0], "P248") == [NVA_ITEM]
    assert by["P31"] == ["Q13442814"] and by["P407"] == ["Q1860"] and by["P1433"] == ["Q564954"]
    assert by["P356"] == ["10.1371/JOURNAL.PONE.0047855"] and by["P1184"] == ["11250/3093139"]
    assert by["P50"] == ["Q11", "Q1"]                           # link back to the authors
    assert by["P2093"] == ["Unlinked Person"]
    assert by["P478"] == ["7"] and by["P433"] == ["11"] and by["P304"] == ["e47855"]


def test_new_item_guards():
    assert plan_new_work(record(), create_ctx(), venue_qid=None).kind == "skip"   # journal unknown
    r = record()
    r["entityDescription"]["reference"]["publicationInstance"]["type"] = "MediaInterview"
    assert plan_new_work(r, create_ctx(), "Q1").kind == "skip"
    c = create_ctx()
    c.person_map = {}
    assert "linked" in plan_new_work(record(), c, "Q1").reason
