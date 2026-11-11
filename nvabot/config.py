"""Configuration loading and validation (fail early, before touching any wiki)."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict

import yaml

from .planner import Policy, Props

DEFAULTS: Dict[str, Any] = {
    "bot": {"site": "wikidata", "stop_on_talk_message": True, "editgroups": True, "assert_user": True,
            "source_url": ""},
    "limits": {"max_edits_per_run": 50, "unflagged_max_edits": 250, "stop_check_every": 10,
               "max_consecutive_failures": 5, "max_failure_ratio": 0.2,
               "nva_min_interval_seconds": 0.25, "sparql_min_interval_seconds": 1.0, "maxlag": 5},
    "wikidata": {"sparql_endpoints": {"main": "https://query.wikidata.org/sparql",
                                      "scholarly": "https://query-scholarly.wikidata.org/sparql"},
                 "nva_item": None, "human_item": "Q5", "properties": {},
                 "handle_prefixes": ["11250"]},
    "nva": {"api_base": "https://api.nva.unit.no",
            "landing_page": "https://nva.sikt.no/registration/{identifier}",
            "person_page": "https://nva.sikt.no/research-profile/{id}",
            "author_roles": ["Creator"], "page_size": 100, "max_pages": 5000},
    "org_items": {},       # NVA/Cristin institution number -> Wikidata item, e.g. "194": Q...
    "positions": {},       # lower-case NVA role label -> Wikidata item
    "create": {"enabled": False, "max_per_run": 25, "work_class": "Q386724", "types": {}, "languages": {},
               "max_authors": 300, "require_linked_author": True, "require_identifier": True,
               "duplicate_title_similarity": 0.9, "label_fallback_language": "mul"},
    "policy": {},
    "link_by_orcid": {"candidate_query": ""},
    "paths": {"state_db": "data/state.sqlite", "log_dir": "logs", "report_dir": "reports",
              "stop_file": "STOP"},
    "stats": {"publish": False, "stats_page": "", "conflicts_page": "", "max_conflict_rows": 500},
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Config:
    def __init__(self, raw: dict, base_dir: Path):
        self.raw = raw
        self.base_dir = base_dir
        self.props = Props.from_mapping(raw["wikidata"]["properties"])
        self.policy = Policy.from_mapping(raw["policy"])
        self.validate()

    @classmethod
    def load(cls, path: str) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file {p} not found (copy config.example.yaml)")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(_merge(DEFAULTS, data), p.resolve().parent)

    def __getitem__(self, key):
        return self.raw[key]

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.base_dir / p

    def validate(self) -> None:
        bot = self.raw.get("bot", {})
        for key in ("name", "user_agent", "run_info_page", "stop_page"):
            if not bot.get(key):
                raise ConfigError(f"bot.{key} is required")
        ua = bot["user_agent"]
        if "http" not in ua and "@" not in ua:
            raise ConfigError("bot.user_agent must contain contact information (a URL), "
                              "see https://meta.wikimedia.org/wiki/User-Agent_policy")
        if bot["site"] not in ("wikidata", "test"):
            raise ConfigError("bot.site must be 'wikidata' or 'test'")
        for name, value in vars(self.props).items():
            if value is not None and not re.fullmatch(r"P\d+", str(value)):
                raise ConfigError(f"wikidata.properties.{name}: {value!r} is not a property id")
        for key in ("nva_item", "human_item"):
            v = self.raw["wikidata"].get(key)
            if v is not None and not re.fullmatch(r"Q\d+", str(v)):
                raise ConfigError(f"wikidata.{key}: {v!r} is not an item id")
        lim = self.raw["limits"]
        if not isinstance(lim["max_edits_per_run"], int) or lim["max_edits_per_run"] < 1:
            raise ConfigError("limits.max_edits_per_run must be a positive integer")
        qid_re = re.compile(r"Q\d+")
        for section in ("org_items", "positions"):
            for k, v in (self.raw.get(section) or {}).items():
                if v is not None and not qid_re.fullmatch(str(v)):
                    raise ConfigError(f"{section}.{k}: {v!r} is not an item id")
        create = self.raw["create"]
        for t, spec in (create.get("types") or {}).items():
            if not isinstance(spec, dict) or not qid_re.fullmatch(str(spec.get("class", ""))):
                raise ConfigError(f"create.types.{t} needs class: Q...")
        for k, v in (create.get("languages") or {}).items():
            if not qid_re.fullmatch(str(v)):
                raise ConfigError(f"create.languages.{k}: {v!r} is not an item id")
        if not qid_re.fullmatch(str(create.get("work_class", ""))):
            raise ConfigError("create.work_class must be an item id")
        if not 0 < self.policy.title_similarity_threshold <= 1:
            raise ConfigError("policy.title_similarity_threshold must be in (0, 1]")
        if self.raw["stats"]["publish"] and not (self.raw["stats"]["stats_page"]
                                                 and self.raw["stats"]["conflicts_page"]):
            raise ConfigError("stats.publish needs stats.stats_page and stats.conflicts_page")
