"""End-to-end run with fake NVA / Wikidata clients: dry run, live run, re-run idempotency, kill switch."""
import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from nvabot import runner as runner_mod
from nvabot import writer as writer_mod
from nvabot.config import Config
from nvabot.state import State

from .fixtures import NVA_ITEM, PERSON, RECORD, claim, entity, person_entity, work_entity
from nvabot import wbjson as wb

RECORD2 = copy.deepcopy(RECORD)
RECORD2.update(identifier="0190-new", handle="https://hdl.handle.net/11250/999", additionalIdentifiers=[])
RECORD2["entityDescription"]["mainTitle"] = "Another paper about snow avalanches in Norway"
RECORD2["entityDescription"]["reference"]["doi"] = "https://doi.org/10.1000/new1"

ROOT = Path(__file__).resolve().parents[1]


class FakeNva:
    def __init__(self, *a, **k):
        self.requests_made = 0

    def person(self, pid):
        return copy.deepcopy(PERSON) if pid == "15120" else None

    def publications_by_person(self, pid, modified_since=None):
        return [copy.deepcopy(RECORD), copy.deepcopy(RECORD2)] if pid == "15120" else []

    def search_resources(self, **params):
        FakeNva.last_params = params
        return [copy.deepcopy(RECORD), copy.deepcopy(RECORD2)]

    base = "https://api.nva.unit.no"


class FakeSparql:
    def __init__(self, *a, **k):
        pass

    def ask(self, query, endpoint="main"):
        return True


class FakeReader:
    entities = {}
    title_hits = []

    def search_titles(self, title, limit=10):
        return list(self.title_hits)

    def __init__(self, *a, **k):
        self.cache = {}

    def applicable_stated_in(self, *a):
        return NVA_ITEM

    def nva_person_map(self, prop):
        return {"15120": "Q1", "11111": "Q11"}, []

    def get_entities(self, qids):
        return {q: copy.deepcopy(self.entities.get(q)) for q in qids}

    def page_text(self, title):
        return FakeWriter.stop_text

    def lookup_values(self, prop, values, normalizer, endpoints=("main",)):
        out = {}
        for v in values:
            n = normalizer(v)
            owners = set(self.cache.get((prop, n), set()))
            if prop == "P356" and n == "10.1371/JOURNAL.PONE.0047855":
                owners.add("Q100")
            if prop == "P236" and n == "1932-6203":
                owners.add("Q564954")
            out[n] = owners
        return out

    def remember(self, prop, value, qid):
        self.cache.setdefault((prop, value), set()).add(qid)


class FakeWriter:
    created = []
    stop_text = "run"
    edits = []
    pages = {}

    def __init__(self, *a, **k):
        pass

    def userinfo(self):
        return {"groups": ["user"], "messages": False}

    def has_new_messages(self):
        return False

    def edit_entity(self, qid, claims, baserevid, summary):
        FakeWriter.edits.append((qid, claims, summary))
        # apply the edit to the fake entity so a second run sees it
        e = FakeReader.entities[qid]
        for c in claims:
            if c.get("remove") == "":
                continue
            if "id" in c:
                lst = e["claims"][c["mainsnak"]["property"]]
                lst[[x["id"] for x in lst].index(c["id"])] = c
            else:
                c = dict(c, id=f"{qid}$new{len(FakeWriter.edits)}{c['mainsnak']['property']}")
                e["claims"].setdefault(c["mainsnak"]["property"], []).append(c)
        e["lastrevid"] += 1
        return e["lastrevid"]

    def create_entity(self, data, summary):
        qid = f"Q{500 + len(FakeWriter.created)}"
        FakeWriter.created.append((qid, data, summary))
        e = {"id": qid, "lastrevid": 1, "labels": data["labels"], "claims": {}}
        for i, c in enumerate(data["claims"]):
            e["claims"].setdefault(c["mainsnak"]["property"], []).append(dict(c, id=f"{qid}$c{i}"))
        FakeReader.entities[qid] = e
        return qid, 1

    def save_page(self, title, text, summary):
        FakeWriter.pages[title] = text


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["paths"] = {k: str(tmp_path / v) for k, v in raw["paths"].items()}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(runner_mod, "NvaClient", FakeNva)
    monkeypatch.setattr(runner_mod, "SparqlClient", FakeSparql)
    monkeypatch.setattr(runner_mod, "WikidataReader", FakeReader)
    monkeypatch.setattr(writer_mod, "PywikibotWriter", FakeWriter)
    FakeReader.entities = {"Q1": person_entity(), "Q100": work_entity()}
    FakeWriter.edits, FakeWriter.pages, FakeWriter.stop_text, FakeWriter.created = [], {}, "run", []
    FakeReader.title_hits = []
    return Config.load(str(p))


@pytest.fixture
def create_cfg(cfg):
    cfg.raw["create"]["enabled"] = True
    return cfg


def test_dry_run_never_edits(cfg):
    r = runner_mod.Runner(cfg, "persons", live=False)
    assert r.execute(r.run_persons) == "completed"
    assert FakeWriter.edits == []
    report = r.report_path.read_text()
    assert "planned,Q1,add_claim,P496,0000-0002-1825-0097" in report


def test_live_run_then_rerun_is_idempotent(cfg):
    r = runner_mod.Runner(cfg, "persons", live=True)
    assert r.execute(r.run_persons) == "completed"
    assert len(FakeWriter.edits) == 1 and "NVA import" in FakeWriter.edits[0][2]
    r = runner_mod.Runner(cfg, "works", live=True)
    assert r.execute(r.run_works) == "completed"
    assert len(FakeWriter.edits) == 2                      # one atomic edit for the work item
    qid, claims, summary = FakeWriter.edits[1]
    assert qid == "Q100" and "editgroups" in summary
    # Second runs must find nothing to do.
    for mode, fn in (("persons", "run_persons"), ("works", "run_works")):
        r = runner_mod.Runner(cfg, mode, live=True)
        assert r.execute(getattr(r, fn)) == "completed"
    assert len(FakeWriter.edits) == 2
    stats = json.loads((Path(cfg.path("report_dir")) / "stats.json").read_text())
    assert stats["totals"]["edits"] == 2
    assert cfg["stats"]["stats_page"] in FakeWriter.pages


def test_stop_page_blocks_live_run(cfg):
    FakeWriter.stop_text = "stop"
    r = runner_mod.Runner(cfg, "persons", live=True)
    assert r.execute(r.run_persons).startswith("stopped")
    assert FakeWriter.edits == []


def test_budget_limits_edits(cfg):
    r = runner_mod.Runner(cfg, "works", live=True, max_edits=1)
    r.safety.max_edits = 0
    r.safety.preflight = lambda: None
    assert r.execute(r.run_works).startswith("budget")
    assert FakeWriter.edits == []


def test_create_dry_run_plans_but_never_writes(create_cfg):
    r = runner_mod.Runner(create_cfg, "works", live=False)
    assert r.execute(r.run_works, create=True) == "completed"
    assert FakeWriter.created == [] and FakeWriter.edits == []
    assert "planned,NEW,create_item" in r.report_path.read_text()


def test_create_live_links_authors_and_is_not_repeated(create_cfg):
    r = runner_mod.Runner(create_cfg, "works", live=True)
    assert r.execute(r.run_works, create=True) == "completed"
    assert len(FakeWriter.created) == 1
    qid, data, summary = FakeWriter.created[0]
    authors = [wb.snak_value(c["mainsnak"]) for c in data["claims"] if c["mainsnak"]["property"] == "P50"]
    assert authors == ["Q11", "Q1"] and "0190-new" in summary
    assert r.state.created_qid("0190-new") == qid
    # Next run: WDQS still knows nothing about the new item, but the state table does.
    r = runner_mod.Runner(create_cfg, "works", live=True)
    assert r.execute(r.run_works, create=True) == "completed"
    assert len(FakeWriter.created) == 1
    assert all(e[0] != qid for e in FakeWriter.edits)     # created item is already complete


def test_create_skipped_when_title_search_finds_item(create_cfg):
    FakeReader.entities["Q700"] = entity("Q700", [claim(wb.monolingual_snak(
        "P1476", "Another paper about snow avalanches in Norway", "en"))])
    FakeReader.title_hits = ["Q700"]
    r = runner_mod.Runner(create_cfg, "works", live=True)
    assert r.execute(r.run_works, create=True) == "completed"
    assert FakeWriter.created == []
    review = r.state.db.execute("SELECT qid, kind FROM review WHERE qid='Q700'").fetchall()
    assert [tuple(x) for x in review] == [("Q700", "proposal")]


def test_doi_handles_adds_only_the_handle(cfg):
    r = runner_mod.Runner(cfg, "doi-handles", live=True)
    assert r.execute(r.run_doi_handles, "194.0.0.0") == "completed"
    assert FakeNva.last_params["top_level_organization"].endswith("/cristin/organization/194.0.0.0")
    assert len(FakeWriter.edits) == 1
    qid, claims, _ = FakeWriter.edits[0]
    assert qid == "Q100" and {c["mainsnak"]["property"] for c in claims} == {"P1184"}
