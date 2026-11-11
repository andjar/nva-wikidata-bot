"""Run-time precautions: kill switches, edit budget and a circuit breaker."""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Optional

log = logging.getLogger("nvabot.safety")


class StopRequested(RuntimeError):
    """Someone asked the bot to stop (stop page, talk page message or STOP file)."""


class BudgetExhausted(RuntimeError):
    """The maximum number of edits for this run was reached."""


class CircuitOpen(RuntimeError):
    """Too many failures - something is wrong, stop before doing damage."""


class Safety:
    def __init__(self, reader, writer, *, live: bool, max_edits: int, stop_page: Optional[str],
                 stop_on_talk: bool, check_every: int, max_consecutive_failures: int,
                 max_failure_ratio: float, stop_file: str, unflagged_max_edits: int):
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
