"""Entrypoint contract: `agent run` — one invocation, one pass, exits 0/nonzero.

Not implemented yet beyond the shape (wiring a real agent's source/scorer/
destination together is issue #8, reddit-leads).
"""
from __future__ import annotations

from framework.interfaces import Destination, Scorer, Source
from framework.state import SeenStore


def run(source: Source, scorer: Scorer, destination: Destination, seen: SeenStore) -> int:
    signals = source.fetch()
    for signal in signals:
        if seen.has(signal.id):
            continue
        scored = scorer.score(signal)
        destination.send(scored)
        seen.mark(signal.id)
    seen.save()
    return 0
