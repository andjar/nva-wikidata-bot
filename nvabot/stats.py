"""Public statistics and the review queue, as JSON and as wikitext pages."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import State, now

KIND_LABEL = {"add_claim": "statements added", "add_reference": "references added",
              "convert_author_string": "author strings → items", "update_claim": "statements extended",
              "create_item": "items created"}


def collect(state: State, review_days: int = 30) -> dict:
    db = state.db
    edits = [dict(r) for r in db.execute(
        "SELECT kind, prop, COUNT(*) n, COUNT(DISTINCT qid) items FROM actions "
        "WHERE status='done' GROUP BY kind, prop ORDER BY kind, prop")]
    totals = db.execute("SELECT COUNT(DISTINCT qid || ':' || revid) edits, COUNT(DISTINCT qid) items, COUNT(*) changes "
                        "FROM actions WHERE status='done'").fetchone()
    monthly = [dict(r) for r in db.execute(
        "SELECT substr(ts,1,7) month, COUNT(DISTINCT qid || ':' || revid) edits, COUNT(*) changes FROM actions "
        "WHERE status='done' GROUP BY month ORDER BY month DESC LIMIT 24")]
    failures = db.execute("SELECT COUNT(*) n FROM actions WHERE status='failed'").fetchone()["n"]
    runs = []
    for r in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 15"):
        d = dict(r)
        d["counters"] = json.loads(d["counters"] or "{}")
        runs.append(d)
    since = (datetime.now(timezone.utc) - timedelta(days=review_days)).isoformat()
    review = [dict(r) for r in db.execute(
        "SELECT kind, COUNT(*) n FROM review WHERE last_seen >= ? GROUP BY kind", (since,))]
    return {"generated": now(), "totals": dict(totals), "failed_actions": failures,
            "by_kind_and_property": edits, "monthly": monthly, "recent_runs": runs,
            "open_review_items": review}


def write_json(stats: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")


def _prop(p: str) -> str:
    return f"{{{{P|{p}}}}}" if p and p.startswith("P") else (p or "—")


def stats_wikitext(stats: dict, conflicts_page: str, source_url: str) -> str:
    t = stats["totals"]
    lines = [
        "__NOTOC__",
        "This page is updated automatically by the NVA import bot after each run. "
        f"Source code: {source_url}. Items needing human review: [[{conflicts_page}]].",
        "",
        f"''Last updated: {stats['generated']} (UTC)''",
        "",
        "== Totals ==",
        '{| class="wikitable"',
        f"| Edits || {t.get('edits') or 0}",
        "|-",
        f"| Items edited || {t.get('items') or 0}",
        "|-",
        f"| Statement/reference changes || {t.get('changes') or 0}",
        "|-",
        f"| Failed changes || {stats['failed_actions']}",
        "|}",
        "",
        "== Changes by type ==",
        '{| class="wikitable sortable"',
        "! Change !! Property !! Count !! Items",
    ]
    for row in stats["by_kind_and_property"]:
        lines += ["|-", f"| {KIND_LABEL.get(row['kind'], row['kind'])} || {_prop(row['prop'])} "
                        f"|| {row['n']} || {row['items']}"]
    lines += ["|}", "", "== Per month ==", '{| class="wikitable"', "! Month !! Edits !! Changes"]
    for row in stats["monthly"]:
        lines += ["|-", f"| {row['month']} || {row['edits']} || {row['changes']}"]
    lines += ["|}", "", "== Recent runs ==", '{| class="wikitable"',
              "! Run !! Mode !! Live !! Started !! Finished !! Status !! Edits !! Conflicts"]
    for r in stats["recent_runs"]:
        c = r["counters"]
        lines += ["|-", f"| {r['id']} || {r['mode']} || {'yes' if r['live'] else 'dry run'} || {r['started']} "
                        f"|| {r['finished'] or ''} || {r['status']} || {c.get('edits', 0)} || {c.get('conflict', 0)}"]
    lines += ["|}", "", "== Open review items (last 30 days) ==", '{| class="wikitable"', "! Kind !! Count"]
    for r in stats["open_review_items"]:
        lines += ["|-", f"| {r['kind']} || {r['n']}"]
    lines.append("|}")
    return "\n".join(lines) + "\n"


def review_wikitext(state: State, max_rows: int, review_days: int = 30) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=review_days)).isoformat()
    rows = list(state.db.execute(
        "SELECT * FROM review WHERE last_seen >= ? ORDER BY kind, last_seen DESC LIMIT ?", (since, max_rows)))
    lines = [
        "Cases the NVA import bot did '''not''' edit because NVA and Wikidata disagree, a value is already "
        "used on another item (possible duplicates), or a human decision is needed. Fix the data on "
        "Wikidata or in NVA; resolved rows disappear after 30 days without being seen again. "
        "Please do not edit this page, it is overwritten.",
        "",
        f"''Last updated: {now()} (UTC). Showing at most {max_rows} rows.''",
        "",
        '{| class="wikitable sortable"',
        "! Item !! Kind !! Property !! Value !! Detail !! Source !! Last seen",
    ]
    for r in rows:
        item = f"{{{{Q|{r['qid']}}}}}" if str(r["qid"]).startswith("Q") else r["qid"]
        value = str(r["value"]).replace("|", "{{!}}")
        detail = str(r["detail"]).replace("|", "{{!}}")
        lines += ["|-", f"| {item} || {r['kind']} || {_prop(r['prop'])} || <nowiki>{value}</nowiki> "
                        f"|| {detail} || {r['subject']} || {r['last_seen'][:10]}"]
    lines.append("|}")
    return "\n".join(lines) + "\n"
