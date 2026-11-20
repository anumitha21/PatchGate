"""In-memory failure ledger + circuit breaker. State is process-local ON PURPOSE: no tool can reset it."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import Status

STOP_MESSAGE = "Stop retrying this approach. Summarize what you tried and ask the human."


@dataclass
class Failure:
    files: list[str]
    status: str
    summary: str
    ts: float = field(default_factory=time.time)


class Ledger:
    def __init__(self, max_consecutive_failures_per_file: int = 4):
        self.limit = max_consecutive_failures_per_file
        self._failures: list[Failure] = []
        self._consecutive: dict[str, int] = {}

    def record_failure(self, files: list[str], status: Status | str, summary: str) -> None:
        self._failures.append(Failure(list(files), str(status), summary))
        for f in set(files):
            self._consecutive[f] = self._consecutive.get(f, 0) + 1

    def reset_files(self, files: list[str]) -> None:
        for f in files:
            self._consecutive.pop(f, None)

    def tripped_files(self, files: list[str]) -> list[str]:
        return sorted(f for f in set(files) if self._consecutive.get(f, 0) >= self.limit)

    def is_tripped(self, files: list[str]) -> bool:
        return bool(self.tripped_files(files))

    def recent_for(self, files: list[str], n: int = 3) -> list[Failure]:
        fs = set(files)
        return [f for f in self._failures if fs & set(f.files)][-n:]
