"""Run-time precautions: kill switches, edit budget and a circuit breaker."""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Optional

log = logging.getLogger("nvabot.safety")


class StopRequested(RuntimeError):
    """Someone asked the bot to stop (stop page, talk page message or STOP file)."""


class BudgetExhausted(RuntimeError):
    """The maximum number of edits for this run was reached."""


class CircuitOpen(RuntimeError):
    """Too many failures - something is wrong, stop before doing damage."""


class LagStop(RuntimeError):
    """Wikidata stayed lagged too long - stop cleanly and try again later."""


class Safety:
    def __init__(self, reader, writer, *, live: bool, max_edits: int, stop_page: Optional[str],
                 stop_on_talk: bool, check_every: int, max_consecutive_failures: int,
                 max_failure_ratio: float, stop_file: str, unflagged_max_edits: int,
                 maxlag: float = 5, lag_poll_seconds: int = 60, max_lag_wait_minutes: float = 60):
        self.reader, self.writer = reader, writer
        self.live = live
        self.max_edits = max_edits
        self.stop_page = stop_page
        self.stop_on_talk = stop_on_talk
        self.check_every = max(1, check_every)
        self.max_consecutive = max_consecutive_failures
        self.max_ratio = max_failure_ratio
        self.stop_file = stop_file
        self.unflagged_max_edits = unflagged_max_edits
        self.edits = 0
        self.attempts = 0
        self.consecutive_failures = 0
        self.window = deque(maxlen=50)
        self.maxlag = maxlag
        self.lag_poll_seconds = lag_poll_seconds
        self.max_lag_wait = max_lag_wait_minutes * 60
        self.lag_waits = 0

    def preflight(self) -> None:
        """Checks before the first edit of a live run."""
        if not self.live:
            return
        info = self.writer.userinfo()
        groups = set(info.get("groups") or [])
        if "bot" not in groups and self.max_edits > self.unflagged_max_edits:
            log.warning("account has no bot flag: limiting this run to %d edits (trial edits)",
                        self.unflagged_max_edits)
            self.max_edits = self.unflagged_max_edits
        self.check()
        self.wait_for_lag()

    def check(self) -> None:
        if os.path.exists(self.stop_file):
            raise StopRequested(f"local stop file {self.stop_file} exists")
        if self.stop_page:
            text = self.reader.page_text(self.stop_page)
            if text is None or text.strip().lower() not in ("run", "go", "on"):
                raise StopRequested(f"stop page [[{self.stop_page}]] does not say 'run'")
        if self.stop_on_talk and self.writer is not None and self.writer.has_new_messages():
            raise StopRequested("new message on the bot's talk page")

    def before_edit(self) -> None:
        if self.edits >= self.max_edits:
            raise BudgetExhausted(f"edit budget of {self.max_edits} reached")
        if self.attempts and self.attempts % self.check_every == 0:
            self.check()
        self.attempts += 1

    def wait_for_lag(self) -> None:
        """Wait until Wikidata's lag is at or below our maxlag, polling; stop the run after max_lag_wait.

        Called before the first edit and whenever Wikidata refuses a write because of lag. Waiting is
        not a failure. The STOP file is honoured while waiting; stop page and talk page every 5 minutes.
        """
        started = time.monotonic()
        last_check = started
        reported = False
        while True:
            lag, kind = self.reader.current_lag()
            if lag is None or lag <= self.maxlag:
                if reported:
                    log.info("Wikidata lag is %s s - continuing", "unknown" if lag is None else f"{lag:.0f}")
                return
            waited = time.monotonic() - started
            if waited >= self.max_lag_wait:
                raise LagStop(f"Wikidata lag still {lag:.0f} s (limit {self.maxlag} s) after "
                              f"{waited / 60:.0f} min; try again later")
            log.warning("Wikidata lag is %.0f s%s, above the %d s limit - waiting (%.0f of max %.0f min)",
                        lag, f" ({kind})" if kind else "", self.maxlag, waited / 60, self.max_lag_wait / 60)
            reported = True
            self.lag_waits += 1
            deadline = time.monotonic() + self.lag_poll_seconds
            while time.monotonic() < deadline:
                if os.path.exists(self.stop_file):
                    raise StopRequested(f"local stop file {self.stop_file} exists")
                time.sleep(min(10, max(0, deadline - time.monotonic())))
            if time.monotonic() - last_check >= 300:
                self.check()
                last_check = time.monotonic()

    lag_pause = wait_for_lag   # used after a write was refused because of lag

    def success(self) -> None:
        self.edits += 1
        self.consecutive_failures = 0
        self.window.append(True)

    def failure(self) -> None:
        self.consecutive_failures += 1
        self.window.append(False)
        if self.consecutive_failures >= self.max_consecutive:
            raise CircuitOpen(f"{self.consecutive_failures} consecutive failures")
        if len(self.window) >= 20:
            ratio = self.window.count(False) / len(self.window)
            if ratio > self.max_ratio:
                raise CircuitOpen(f"failure ratio {ratio:.0%} over last {len(self.window)} edits")
