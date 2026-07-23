"""Plugin interfaces for the agents framework.

One real implementation each for v1 (Reddit source, regex scorer, Slack
destination) — but defined as interfaces from the start since a new source
may need different shapes (see planning discussion, not guessed abstractly
in advance beyond this).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Signal:
    """One raw item pulled from a Source, before scoring."""

    id: str  # stable id used for dedupe (e.g. reddit post id)
    text: str
    url: str
    source: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoredSignal:
    signal: Signal
    score: int
    reasons: list[str] = field(default_factory=list)


class Source(Protocol):
    """Produces raw Signals for one run. No side effects beyond fetching."""

    def fetch(self) -> list[Signal]:
        ...


class Scorer(Protocol):
    """Scores a Signal. Regex/keyword for v1; LLM-based waterfall is a
    tracked future replacement (see issue: LLM-based scorer)."""

    def score(self, signal: Signal) -> ScoredSignal:
        ...


class Destination(Protocol):
    """Delivers a ScoredSignal somewhere. Slack for v1; email later."""

    def send(self, scored: ScoredSignal) -> None:
        ...
