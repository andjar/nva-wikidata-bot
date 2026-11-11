"""Write access to Wikidata through Pywikibot (login, tokens, maxlag, throttling).

Each item is changed in ONE atomic wbeditentity call with `baserevid`, so an
edit conflict is detected instead of silently overwriting someone's change.
Pywikibot is imported lazily so dry runs and tests do not need it.
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Dict, List, Optional, Tuple

from .planner import Action

log = logging.getLogger("nvabot.writer")


class EditConflict(RuntimeError):
    pass


class ServerLagged(RuntimeError):
    """Wikidata refused the write because of replication lag (maxlag); nothing was changed."""


def build_claims_payload(entity: dict, actions: List[Action]) -> List[dict]:
    """Turn planned actions into the `claims` list of a wbeditentity call."""
    out: List[dict] = []
    changes: Dict[str, dict] = {}   # existing statement id -> qualifiers/references to add
    for a in actions:
        if a.kind == "add_claim":
            out.append(a.claim)
        elif a.kind == "convert_author_string":
            out.append(a.claim)
            out.append({"id": a.claim_id, "remove": ""})
        elif a.kind in ("add_reference", "update_claim"):
            ch = changes.setdefault(a.claim_id, {"qualifiers": [], "references": []})
            ch["qualifiers"] += a.qualifiers
            if a.reference:
                ch["references"].append(a.reference)
        elif a.kind == "create_item":
            raise ValueError("create_item is not part of an item edit")
    if changes:
        index = {c["id"]: c for cs in (entity.get("claims") or {}).values() for c in cs}
        for cid, ch in changes.items():
            if cid not in index:
                raise KeyError(f"statement {cid} not found on {entity.get('id')}")
            claim = copy.deepcopy(index[cid])
            for q in ch["qualifiers"]:
                claim.setdefault("qualifiers", {}).setdefault(q["property"], []).append(q)
                order = claim.setdefault("qualifiers-order", [])
                if q["property"] not in order:
                    order.append(q["property"])
            if ch["references"]:
                claim.setdefault("references", []).extend(ch["references"])
            out.append(claim)
    return out


class PywikibotWriter:
    def __init__(self, site_code: str, assert_user: bool = True, max_retries: Optional[int] = 6):
        import pywikibot  # noqa: WPS433 (lazy import on purpose)
        from pywikibot.data import api

        self._pwb = pywikibot
        self._api = api
        code = {"wikidata": "wikidata", "test": "test"}[site_code]
        site = pywikibot.Site(code, "wikidata")
        self.repo = site.data_repository() or site
        self.repo.login()
        self.assert_user = assert_user
        self.max_retries = max_retries   # Pywikibot's own retries; longer lag is handled by Safety.lag_pause
        log.info("logged in to %s as %s", self.repo, self.repo.username())

    @property
    def maxlag(self) -> Optional[float]:
        """The maxlag Pywikibot sends with writes (user-config write_maxlag)."""
        cfg = self._pwb.config
        return getattr(cfg, "write_maxlag", None) or getattr(cfg, "maxlag", None)

    def _req(self, **params) -> dict:
        try:
            return self._api.Request(site=self.repo, parameters=params, max_retries=self.max_retries).submit()
        except self._pwb.exceptions.MaxlagTimeoutError as e:
            raise ServerLagged(str(e)) from e

    def _write_params(self, **params) -> dict:
        params["token"] = self.repo.tokens["csrf"]
        params["bot"] = True
        if self.assert_user:
            params["assert"] = "user"
        return params

    def edit_entity(self, qid: str, claims: List[dict], baserevid: int, summary: str) -> int:
        params = self._write_params(action="wbeditentity", id=qid, baserevid=baserevid,
                                    data=json.dumps({"claims": claims}), summary=summary[:490])
        try:
            r = self._req(**params)
        except self._pwb.exceptions.APIError as e:
            if e.code in ("editconflict", "edit-conflict"):
                raise EditConflict(str(e)) from e
            raise
        return int(r["entity"]["lastrevid"])

    def create_entity(self, data: dict, summary: str) -> Tuple[str, int]:
        """Create a new item in one edit; returns (QID, revision id)."""
        params = self._write_params(action="wbeditentity", new="item", data=json.dumps(data),
                                    summary=summary[:490])
        r = self._req(**params)
        return r["entity"]["id"], int(r["entity"]["lastrevid"])

    def save_page(self, title: str, text: str, summary: str) -> None:
        self._req(**self._write_params(action="edit", title=title, text=text, summary=summary))

    def userinfo(self) -> dict:
        r = self._req(action="query", meta="userinfo", uiprop="hasmsg|groups|rights", formatversion=2)
        return r["query"]["userinfo"]

    def has_new_messages(self) -> bool:
        return bool(self.userinfo().get("messages"))
