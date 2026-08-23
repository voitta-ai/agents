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

A cron/launchd `exit 0` with an empty log cannot tell "healthy, nothing new"
from "source silently returned []" from "everything already deduped" — three
situations that call for very different responses. So `run()` prints exactly
one summary line on every invocation, including a zero-signal one; a quiet run
still has to say it was quiet (issue #31).
"""
from __future__ import annotations

from typing import cast

from framework.interfaces import Destination, Scorer, Source
from framework.state import SeenStore


def run(source: Source, scorer: Scorer, destination: Destination, seen: SeenStore, agent_name: str) -> int:
    signals = source.fetch()
    fetched = len(signals)

    pending: list[str] = []
    for signal in signals:
        if seen.has(signal.id):
            continue
        scored = scorer.score(signal)
        destination.send(scored)
        pending.append(signal.id)
    new = len(pending)

    flush = getattr(destination, "flush", None)
    if callable(flush):
        flush()

    # Only now is delivery known to have happened, for streaming and batching
    # destinations alike. Note seen.save() was always end-of-run, so nothing is
    # lost by deferring the marks to here.
    for signal_id in pending:
        seen.mark(signal_id)
    seen.save()

    # `send()` returns nothing, so the runner cannot tell delivery from a
    # destination's own gating (e.g. Slack's per-route min_score) apart from
    # dedupe. Teaching the runner about every destination's gating rules would
    # contort the Destination protocol, so instead a destination MAY expose an
    # optional `declined_count()` -- discovered the same way as `flush()` --
    # reporting how many of this run's signals it chose not to send. Absent
    # that hook, declined is unknown and reported as 0 rather than guessed.
    declined_count = getattr(destination, "declined_count", None)
    declined = cast(int, declined_count()) if callable(declined_count) else 0
    delivered = new - declined

    print(
        f"agent={agent_name} fetched={fetched} new={new} delivered={delivered} "
        f"declined={declined} seen={len(seen)}"
    )
    return 0
