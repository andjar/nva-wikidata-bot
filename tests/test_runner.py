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


def test_suggest_mappings_only_fills_exact_matches(cfg, monkeypatch):
    orgs = {"185": {"labels": {"nb": "Universitetet i Oslo", "en": "University of Oslo"}, "acronym": "UIO"},
            "1990": {"labels": {"en": "Some Institute"}}}
    FakeNva.institution = lambda self, num: orgs.get(num)
    hits = {"University of Oslo": [{"id": "Q486156", "label": "University of Oslo", "description": "university"}],
            "Universitetet i Oslo": [{"id": "Q486156", "label": "Universitetet i Oslo", "description": "universitet"}],
            "Some Institute": [{"id": "Q9", "label": "Some Other Institute", "description": ""}],
            "Professor": [{"id": "Q121594", "label": "professor", "description": "academic rank"}]}
    FakeReader.search_entities = lambda self, text, lang="en", limit=3: hits.get(text, [])
    cfg.policy.add_former_employers = True
    r = runner_mod.Runner(cfg, "suggest-mappings", live=False)
    assert r.execute(r.run_suggest_mappings) == "completed"
    report = Path(cfg.path("report_dir"))
    org_map = yaml.safe_load((report / "org-items-suggestions.yaml").read_text())
    assert org_map == {"185": "Q486156", "1990": None}          # no exact match -> null, never a guess
    positions = yaml.safe_load((report / "positions-suggestions.yaml").read_text())
    assert positions["professor"] == "Q121594"
    assert FakeWriter.edits == []

    # The reviewed file can be used directly as org_items.
    raw = yaml.safe_load(Path(cfg.base_dir / "config.yaml").read_text())
    raw["org_items"] = str(report / "org-items-suggestions.yaml")
    (cfg.base_dir / "config.yaml").write_text(yaml.safe_dump(raw))
    cfg2 = Config.load(str(cfg.base_dir / "config.yaml"))
    assert cfg2["org_items"] == {"185": "Q486156", "1990": None}


def test_bad_mapping_file_is_rejected(cfg, tmp_path):
    bad = tmp_path / "orgs.yaml"
    bad.write_text('"185": University of Oslo\n')
    raw = yaml.safe_load((cfg.base_dir / "config.yaml").read_text())
    raw["org_items"] = str(bad)
    (cfg.base_dir / "config.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(Exception, match="not an item id"):
        Config.load(str(cfg.base_dir / "config.yaml"))


# ------------------------------------------------------------------ lag and NVA errors
from nvabot.nva import NvaError, NvaNotPublic
from nvabot.writer import ServerLagged


def test_server_lag_pauses_and_retries_without_failure(cfg, monkeypatch):
    cfg.raw["limits"]["lag_pause_seconds"] = 0
    real = FakeWriter.edit_entity
    calls = {"n": 0}

    def lagged_once(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ServerLagged("maxlag")
        return real(self, *a, **k)

    monkeypatch.setattr(FakeWriter, "edit_entity", lagged_once)
    r = runner_mod.Runner(cfg, "persons", live=True)
    assert r.execute(r.run_persons) == "completed"
    assert len(FakeWriter.edits) == 1 and r.counters["lag_pauses"] == 1
    assert r.state.db.execute("SELECT COUNT(*) FROM actions WHERE status='failed'").fetchone()[0] == 0


def test_persistent_lag_stops_cleanly(cfg, monkeypatch):
    cfg.raw["limits"].update(lag_pause_seconds=0, max_lag_pauses=2)
    monkeypatch.setattr(FakeWriter, "edit_entity", lambda self, *a, **k: (_ for _ in ()).throw(ServerLagged("x")))
    r = runner_mod.Runner(cfg, "persons", live=True)
    status = r.execute(r.run_persons)
    assert status.startswith("lagged")
    assert r.state.db.execute("SELECT COUNT(*) FROM actions WHERE status='failed'").fetchone()[0] == 0
    assert r.state.get_cursor("persons") != "15120"         # lagged item not marked done; --resume retries it


def test_hidden_nva_profile_is_skipped_not_fatal(cfg, monkeypatch):
    def person(self, pid):
        if pid == "11111":
            raise NvaNotPublic("401")
        return copy.deepcopy(PERSON) if pid == "15120" else None
    monkeypatch.setattr(FakeNva, "person", person)
    r = runner_mod.Runner(cfg, "persons", live=False)
    assert r.execute(r.run_persons) == "completed"
    assert r.counters["nva_profile_not_public"] == 1
    assert "planned,Q1,add_claim,P496" in r.report_path.read_text()   # the other person still processed
    assert r.state.db.execute("SELECT COUNT(*) FROM review WHERE value='11111'").fetchone()[0] == 0


def test_many_nva_errors_in_a_row_stop_the_run(cfg, monkeypatch):
    cfg.raw["limits"]["max_consecutive_nva_errors"] = 2
    monkeypatch.setattr(FakeNva, "person", lambda self, pid: (_ for _ in ()).throw(NvaError("NVA 502")))
    r = runner_mod.Runner(cfg, "persons", live=False)
    assert r.execute(r.run_persons).startswith("circuit breaker")
