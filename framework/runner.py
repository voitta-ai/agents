"""Entrypoint contract: `agent run` — one invocation, one pass, exits 0/nonzero.

The loop is per-signal, which suits lead-finding where each signal is
independently interesting. Agents whose output is a single summary — a daily
digest, a calendar rundown, a weekly blog draft — need a point where something
sees the whole run. That is `Destination.flush()`, called once after the loop
(issue #26).

`flush()` is optional. A streaming destination does not define it and is
unaffected.

Dedupe is marked only after delivery has actually happened, which for a batching
destination means after `flush()`, not after `send()`. Marking earlier would
lose signals silently: `send()` merely buffers, so a `flush()` that throws would
leave those signals marked-seen and never delivered, with nothing to retry.
Deferring means a failed run re-delivers rather than drops — at-least-once,
which is the right bias for a notifier.
"""
from __future__ import annotations

from framework.interfaces import Destination, Scorer, Source
from framework.state import SeenStore


def run(source: Source, scorer: Scorer, destination: Destination, seen: SeenStore) -> int:
    signals = source.fetch()

    pending: list[str] = []
    for signal in signals:
        if seen.has(signal.id):
            continue
        scored = scorer.score(signal)
        destination.send(scored)
        pending.append(signal.id)

    flush = getattr(destination, "flush", None)
    if callable(flush):
        flush()

    # Only now is delivery known to have happened, for streaming and batching
    # destinations alike. Note seen.save() was always end-of-run, so nothing is
    # lost by deferring the marks to here.
    for signal_id in pending:
        seen.mark(signal_id)
    seen.save()
    return 0
