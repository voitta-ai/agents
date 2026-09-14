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
    """Produces raw Signals for one run. No side effects beyond fetching.

    A source may also define `commit()`, called once by the runner after
    `seen.save()` has succeeded, and only on a `status=ok` run (never on a
    `partial` one, whose unscored signals must stay inside the next window). It
    is for a source that tracks its own fetch window (the github-releases
    source's `last_run`): the window advances in `commit()`, never in `fetch()`,
    so a run that fails to deliver does not move it. Like `Destination.flush()` it is discovered with getattr and is
    deliberately not part of this Protocol (issue #46).
    """

    def fetch(self) -> list[Signal]:
        ...


class ScoreError(RuntimeError):
    """This one signal could not be scored; others may be.

    A scorer raises it (or a subclass) when the failure belongs to the signal,
    not to the scorer: the model answered, but not with a readable score. The
    runner skips that signal without sending or marking it, so the next run
    retries it, and finishes the run for everything else with `status=partial`.
    Anything else a scorer raises, such as every model in the chain being down,
    still aborts the run, because it would fail every signal the same way.
    """


class Scorer(Protocol):
    """Scores a Signal. Raises `ScoreError` for a failure specific to that
    signal; any other exception aborts the run."""

    def score(self, signal: Signal) -> ScoredSignal:
        ...


class Destination(Protocol):
    """Delivers a ScoredSignal somewhere. Slack for v1; email later.

    A destination may also define `flush()`, called once by the runner after the
    last signal. Streaming destinations deliver in `send()` and need no flush;
    batching and digest destinations buffer in `send()` and emit in `flush()`.
    It is deliberately not part of this Protocol — the runner discovers it with
    getattr, so existing destinations stay valid without change (issue #26).
    """

    def send(self, scored: ScoredSignal) -> None:
        ...


class Action(Protocol):
    """One step of an approved action (issue #21).

    Actions are the fourth plugin kind, alongside Source / Scorer / Destination.
    They never run inside `agent run`: a destination records a *proposal*, and a
    separate `agent approvals` pass executes the proposal's steps only after a
    trusted human has replied `do <id>`. `payload` is the proposal's content
    merged with the results of earlier steps; `context` carries run-level facts
    (workspace path, proposal id). The returned dict is merged into the payload
    seen by later steps, so a step publishes values by name for the ones after it.
    """

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        ...
