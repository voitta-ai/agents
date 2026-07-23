"""Dedupe/state persistence between run-and-exit invocations.

The one thing the framework truly owns between runs: what's already been
seen/sent, so a double-fire from cron isn't harmful.

v1: keep behavior equivalent to hq/leads/signals/signal_processor.py's
existing dedupe logic (treated as reference, not redesigned yet — see
tracked issue).
"""
from __future__ import annotations

import json
from pathlib import Path


class SeenStore:
    """Flat-file set of already-processed signal ids, one file per agent."""

    def __init__(self, path: Path):
        self.path = path
        self._seen: set[str] = set()
        if self.path.exists():
            self._seen = set(json.loads(self.path.read_text()))

    def has(self, signal_id: str) -> bool:
        return signal_id in self._seen

    def mark(self, signal_id: str) -> None:
        self._seen.add(signal_id)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self._seen)))
