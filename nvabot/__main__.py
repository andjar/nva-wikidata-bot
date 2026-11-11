"""Command line: python -m nvabot [--config FILE] <command> [options]

Every command is a DRY RUN unless --live is given. A dry run reads NVA and
Wikidata and writes a CSV report of what it would do; it never edits.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import Config, ConfigError


def setup_logging(log_dir, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger("nvabot")
    root.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(log_dir / "nvabot.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


def qid(value: str) -> str:
    if not re.fullmatch(r"Q\d+", value):
        raise argparse.ArgumentTypeError(f"{value} is not a QID")
    return value


def date_arg(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nvabot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    def editing(p):
        p.add_argument("--live", action="store_true", help="really edit Wikidata (default: dry run)")
        p.add_argument("--max-edits", type=int, help="lower the edit budget for this run")
        p.add_argument("--limit", type=int, help="process at most N items/records")

    sub.add_parser("check", help="validate config and test read access")
    p = sub.add_parser("persons", help="enrich items that have an NVA person ID (ORCID, employer)")
    editing(p)
    p.add_argument("--item", type=qid, action="append", default=[], help="only this item (repeatable)")
    p.add_argument("--resume", action="store_true", help="continue after the last processed NVA id")
    p = sub.add_parser("link-orcid", help="add NVA person ID to items whose ORCID matches an NVA profile")
    editing(p)
    p = sub.add_parser("works", help="enrich publications of linked persons (handle, DOI, title, date, authors)")
    editing(p)
    p.add_argument("--person", action="append", default=[], help="only this NVA person id (repeatable)")
    p.add_argument("--since", type=date_arg, help="only NVA records modified since this date")
    p.add_argument("--incremental", action="store_true", help="only records modified since the last full run")
    p.add_argument("--create", action="store_true",
                   help="create items for NVA records not on Wikidata (needs create.enabled in config)")
    p = sub.add_parser("handles", help="enrich items that already carry an NVA handle")
    editing(p)
    p = sub.add_parser("doi-handles", help="add NVA handles to items found by DOI (handle only)")
    editing(p)
    p.add_argument("--institution", help="NVA/Cristin top-level organisation, e.g. 194.0.0.0")
    p.add_argument("--since", type=date_arg, help="only NVA records modified since this date")
    p.add_argument("--year-since", type=int, help="only publications from this year on")
    p.add_argument("--incremental", action="store_true", help="continue from the last completed run")
    p = sub.add_parser("suggest-mappings", aliases=["suggest-positions", "suggest-orgs"],
                       help="propose institution and position items from NVA profiles (no edits)")
    p.add_argument("--limit", type=int, default=500, help="number of NVA profiles to sample (0 = all)")
    p = sub.add_parser("stats", help="(re)build stats.json; --publish also updates the wiki pages")
    p.add_argument("--publish", action="store_true")

    args = ap.parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.path("log_dir"), args.verbose)
    log = logging.getLogger("nvabot")

    if args.command == "check":
        return run_check(cfg, log)
    if args.command == "stats":
        from .runner import publish_stats
        from .state import State
        writer = None
        if args.publish:
            from .writer import PywikibotWriter
            writer = PywikibotWriter(cfg["bot"]["site"], cfg["bot"].get("assert_user", True),
                                     cfg["limits"]["write_max_retries"])
        publish_stats(cfg, State(cfg.path("state_db")), writer)
        log.info("stats written to %s", cfg.path("report_dir") / "stats.json")
        return 0

    if getattr(args, "create", False) and not cfg["create"]["enabled"]:
        print("--create needs create.enabled: true in the config", file=sys.stderr)
        return 2
    from .runner import Runner
    runner = Runner(cfg, args.command, live=getattr(args, "live", False), max_edits=getattr(args, "max_edits", None))
    log.info("run %s: %s (%s)", runner.run_id, args.command, "LIVE" if runner.live else "dry run")
    if args.command == "persons":
        status = runner.execute(runner.run_persons, args.item, args.limit, args.resume)
    elif args.command == "link-orcid":
        status = runner.execute(runner.run_link_orcid, args.limit)
    elif args.command == "works":
        status = runner.execute(runner.run_works, args.person, args.since, args.incremental, args.limit,
                                args.create)
    elif args.command == "doi-handles":
        status = runner.execute(runner.run_doi_handles, args.institution, args.since, args.year_since,
                                args.incremental, args.limit)
    elif args.command in ("suggest-mappings", "suggest-positions", "suggest-orgs"):
        status = runner.execute(runner.run_suggest_mappings, args.limit)
    else:
        status = runner.execute(runner.run_handles, args.limit)
    # budget reached and server lag are normal stops for a scheduled job, not failures
    return 0 if status == "completed" or status.startswith(("budget", "lagged")) else 1


def run_check(cfg, log) -> int:
    from .nva import NvaClient
    from .wikidata import API_URLS, SparqlClient, WikidataReader
    ua = cfg["bot"]["user_agent"]
    ok = True
    try:
        nva = NvaClient(cfg["nva"]["api_base"], ua)
        page = nva.search_page(size=1)
        log.info("NVA search OK (%s records)", page.get("totalHits"))
    except Exception as e:  # noqa: BLE001
        ok = False
        log.error("NVA: %s", e)
    try:
        sparql = SparqlClient(cfg["wikidata"]["sparql_endpoints"], ua)
        reader = WikidataReader(sparql, API_URLS[cfg["bot"]["site"]], ua)
        p = cfg.props
        nva_item = cfg["wikidata"]["nva_item"] or reader.applicable_stated_in(p.nva_person_id,
                                                                             p.applicable_stated_in)
        log.info("NVA item for references: %s", nva_item or "NOT FOUND - set wikidata.nva_item")
        ok &= bool(nva_item)
        mapping, problems = reader.nva_person_map(p.nva_person_id)
        log.info("Wikidata items with NVA person ID: %d (%d problems)", len(mapping), len(problems))
        stop = reader.page_text(cfg["bot"]["stop_page"])
        log.info("stop page [[%s]]: %r", cfg["bot"]["stop_page"], (stop or "MISSING").strip()[:40])
        ok &= bool(stop and stop.strip().lower() in ("run", "go", "on"))
    except Exception as e:  # noqa: BLE001
        ok = False
        log.error("Wikidata: %s", e)
    log.info("check %s", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
