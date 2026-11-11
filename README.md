# NVA → Wikidata bot

[![tests](https://github.com/andjar/nva-wikidata-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/andjar/nva-wikidata-bot/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Source code of [User:NVAImportBot](https://www.wikidata.org/wiki/User:NVAImportBot), operated by
[User:Ajarmund](https://www.wikidata.org/wiki/User:Ajarmund). To stop the bot, leave a message on
[its talk page](https://www.wikidata.org/wiki/User_talk:NVAImportBot).

Adds **missing, referenced** data from [Nasjonalt vitenarkiv (NVA)](https://nva.sikt.no) to Wikidata, and can
optionally create items for publications that are not on Wikidata yet. It never overwrites a value. The only
removal is the standard conversion of an author name string (P2093) into an author item (P50) for the same
person.

| Command | Finds items by | Can add |
|---|---|---|
| `persons` | NVA person ID (P2287) | ORCID (P496), employer (P108) with position as qualifier (P2868) |
| `link-orcid` | ORCID, no NVA person ID yet | NVA person ID (P2287) |
| `works` | publications of linked persons in NVA, matched to items by DOI or handle | Handle (P1184), DOI (P356), title (P1476), publication date (P577), authors (P50) |
| `works --create` | same, for NVA records with **no** item | a new item (see below) |
| `handles` | items that already have an NVA handle (P1184) | same as `works` |
| `doi-handles` | NVA records with DOI + handle (filter by `--institution`, `--since`, `--year-since`), matched by DOI | Handle (P1184) only |
| `suggest-positions` | NVA role labels | writes `reports/positions-suggestions.yaml` with candidate items (no edits) |
| `stats` | — | rebuilds `stats.json`; `--publish` updates the on-wiki stats and review pages |
| `check` | — | read-only self-test of config, NVA, WDQS, NVA item, stop page |

Every added statement carries a reference: *stated in* NVA (P248), the NVA person ID or the NVA landing
page (P854), and *retrieved* (P813).

## How duplicates and bad edits are avoided

* **Fresh state.** Each item is read live from the Wikidata API just before planning; WDQS is used only to find
  candidates and for uniqueness checks.
* **Normalised comparison.** `https://doi.org/10.1/abc` = `10.1/ABC`; handle URLs = bare handles; ORCID URLs = bare iDs.
* **Present in any rank = present.** A value someone deprecated is never re-added.
* **Existing value, no NVA reference →** the NVA reference is added to the existing statement, not a second statement.
* **Different value already there →** no edit; the case goes to the review page.
* **Identifier already on another item →** no edit; reported as a possible duplicate item.
* **Two independent signals for works.** A DOI/handle match must also agree on title (similarity ≥ 0.8) and year (±1),
  otherwise nothing is written for that item.
* **Author links (paper → person, P50)** are made only through the NVA person ID → QID link, never twice:
  a matching author name string is converted into P50 (keeping its series ordinal and the name as *object named as*);
  otherwise P50 is added at the NVA position if that position is free in the author list; anything else goes to review.
* **One atomic edit per item** (`wbeditentity` with `baserevid`), so edit conflicts are detected, re-read and re-planned once.
* Only NVA records with status `PUBLISHED` and person profiles marked `verified` are used (configurable).
* The NVA handle and ORCID searches are fuzzy, so every hit is re-verified for an exact match.

## Employer and position

Current NVA affiliations become *employer* (P108) for the institution, with the NVA role as *subject has role*
(P2868) qualifier. NVA has no dates, so none are invented; the reference's *retrieved* date records when it was
true. Former affiliations are skipped unless `add_former_employers` is on (then *end time* = unknown value).
Existing employer statements are extended with a missing position and an NVA reference, never duplicated; a
statement with an end time while NVA says "current" is sent to review.
Institutions are mapped in `org_items` (and/or a property in `org_id`), positions in `positions`; run
`suggest-positions` to see which NVA role labels occur and check the proposed items before adding them.

## Creating items (`works --create`)

Off unless `create.enabled: true` **and** `--create`. A record is only created when all of this holds:

* status PUBLISHED, type listed in `create.types` (default: articles, review articles, monographs, PhD theses,
  research reports), title, valid date, DOI or handle;
* at least one author is linked to Wikidata (so the new item always links back to its authors with P50);
* for articles, the journal is found on Wikidata by ISSN (P236), giving *published in* (P1433);
* no item has its DOI or any of its handles (both WDQS graphs), the bot's own `created` table has no item for it
  (WDQS lags; the table does not), and a title search finds no item with the same title and year;
* at most `create.max_per_run` new items per run.

New items get label, English/Bokmål descriptions, instance of, title, language, date, DOI, handle, journal, volume,
issue, pages, P50 for linked authors and P2093 for the rest (with series ordinals), all referenced to NVA.
At the start of each `--create` run the configured classes are checked to be subclasses of *work* and the
languages to be languages; the run stops otherwise.

## Handles from DOIs

`doi-handles` walks NVA (narrow it with `--institution 194.0.0.0`, `--since`, `--year-since`; `--incremental`
continues from the last completed run), finds Wikidata items by DOI and adds the NVA handle with a reference.
Once items carry handles, `handles` keeps them in sync with NVA. `works` also adds handles when it matches by DOI.

## Precautions while running

* **Dry run by default.** Without `--live` nothing is edited; a CSV of planned changes is written to `reports/`.
* **Kill switches**, checked before the first edit and every 10 edits: the on-wiki stop page must contain just `run`;
  any new message on the bot's talk page stops it; a local `STOP` file stops it.
* **Edit budget** per run (`--max-edits`, `limits.max_edits_per_run`), and an automatic cap of 250 edits while the
  account has no bot flag.
* **Circuit breaker:** stops after 5 consecutive failures or > 20 % failures over the last 50 edits.
* **Politeness:** `maxlag=5`, Pywikibot's write throttle, a descriptive User-Agent, rate limits and retries with
  back-off for NVA and WDQS (honouring `Retry-After`), `assert=user` so it never edits logged out.

## Logging and public stats

* `logs/nvabot.log` – rotating application log.
* `logs/audit-run<N>-*.jsonl` – one line per decision (added, referenced, skipped, conflict, failed) with revision ids.
* `reports/*-run<N>-*.csv` – human-readable list of what was (or would be) changed and why.
* `reports/not-on-wikidata-run<N>-*.csv` – NVA publications of linked people with no Wikidata item (candidates for
  item creation by other tools, not by this bot).
* `data/state.sqlite` – runs, every action, review queue, resume cursors.
* After each live run the bot updates two public pages in its user space:
  `User:<bot>/NVA/Stats` (totals, by property, per month, recent runs) and `User:<bot>/NVA/Review` (conflicts and
  proposals for humans). Every edit summary links the run info page and an
  [EditGroups](https://www.wikidata.org/wiki/Wikidata:Edit_groups) batch, so a whole run can be inspected or undone.

## Setup

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml          # edit bot name, pages, user agent
cp user-config.example.py user-config.py    # Pywikibot account settings
python -m pytest -q                         # offline tests
python -m nvabot check                      # read-only self-test
```

Create a [BotPassword](https://www.wikidata.org/wiki/Special:BotPasswords) for the bot account (grants: *edit existing
pages*, *edit your user page*… keep it minimal) and store it with Pywikibot (`python -m pywikibot.scripts.login`).
Never commit `user-config.py`, `user-password.py` or `config.yaml` with secrets.

## Getting approval (Wikidata bot policy)

1. Register the separate account `NVAImportBot` and create its pages from the ready-made wikitext in
   [`docs/wiki/`](docs/wiki/): `User:NVAImportBot` (operator, purpose, how to stop it), `User:NVAImportBot/NVA`
   (task description, linked from every edit summary) and `User:NVAImportBot/Stop` (just `run`).
2. Do dry runs and read the CSV reports (`persons --limit 200`, `works --person <id>`).
3. Mention the plan on the talk page of the NVA person ID property and to relevant WikiProjects
   (item creation especially: say which types, how many, and how duplicates are prevented).
4. File a request at [Wikidata:Requests for permissions/Bot](https://www.wikidata.org/wiki/Wikidata:Requests_for_permissions/Bot)
   (drafts: [`docs/wiki/RfP_NVAImportBot.wikitext`](docs/wiki/RfP_NVAImportBot.wikitext) for enrichment,
   [`RfP_NVAImportBot_2.wikitext`](docs/wiki/RfP_NVAImportBot_2.wikitext) for item creation),
   one task per request (e.g. 1: ORCID, employer and position; 2: works enrichment incl. handles from DOIs and
   author-string conversion; 3: item creation).
5. Make the test edits the reviewers ask for, e.g. `python -m nvabot persons --live --max-edits 50`, and link the
   EditGroups batch in the request.
6. After the flag is granted, raise `limits.max_edits_per_run` and schedule.

Before going live, verify these assumptions in the config:

* P2287 is the property currently labelled "NVA person ID", and `check` resolves the NVA item (else set `wikidata.nva_item`).
* NVA's metadata licence allows CC0 re-use on Wikidata.
* The handle prefixes you want in `wikidata.handle_prefixes` (only `11250` by default).
* Every QID you put in `org_items`, `positions` and `create.types` (open each one).
* The `link_by_orcid.candidate_query` is narrow enough for your purpose.

## Scheduling (Toolforge)

`deploy/jobs.yaml` defines weekly jobs for `toolforge jobs load deploy/jobs.yaml`. `works --incremental` only fetches
NVA records modified since the last full run; `persons --resume` continues a sweep split over several runs.

## Layout

```
nvabot/normalize.py  identifier, name, title and date normalisation/validation
nvabot/nva.py        polite read-only NVA client
nvabot/wikidata.py   WDQS + Action API reads (main and scholarly graphs)
nvabot/planner.py    pure decision logic - every rule above lives here and is unit-tested
nvabot/writer.py     Pywikibot writes (one atomic edit per item)
nvabot/safety.py     stop page, talk page, STOP file, budget, circuit breaker
nvabot/state.py      SQLite state;   nvabot/stats.py   stats + review pages
nvabot/runner.py     orchestration;  nvabot/__main__.py CLI
tests/               offline tests (no network, no Pywikibot needed)
docs/wiki/           wikitext for the bot's user pages and the bot requests
deploy/              Toolforge jobs and run wrapper
```

## Licence

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).
