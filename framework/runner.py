"""Entrypoint contract: `agent run` — one invocation, one pass, exits 0/nonzero.

The loop is per-signal, which suits lead-finding where each signal is
independently interesting. Agents whose output is a single summary — a daily
digest, a calendar rundown, a weekly blog draft — need a point where something
sees the whole run. That is `Destination.flush()`, called once after the loop
(issue #26).

`flush()` is optional. A streaming destination does not define it and is
unaffected.

The mirror image on the source side is `Source.commit()`, called once after
`seen.save()` succeeds (issue #46). It exists for a source whose fetch window is
its own state — the github-releases source keeps `last_run` so "what shipped
since we last looked" is an explicit question rather than an over-fetch that
SeenStore trims. Such a source must not advance that window inside `fetch()`:
a run that fetched, then failed to deliver, would have moved the window past
releases nobody saw. `commit()` runs only after delivery and the dedupe marks
are durable, so a failure anywhere earlier leaves the window where it was and
the next run re-fetches it. Optional and discovered with getattr, exactly like
`flush()`; existing sources are unaffected.

`commit()` runs only on an `ok` run, never on a `partial` one. An unscored
signal is deliberately left unmarked so the next run retries it, and for a
windowed source that retry exists only if the next run's window still contains
the signal. Advancing the window past it would turn "retried next run" into
"never fetched again": the silent skip `commit()` exists to prevent. So a
partial run leaves the window where it was, the next run re-fetches it, and
SeenStore drops everything that did get through.

Dedupe is marked only after delivery has actually happened, which for a batching
destination means after `flush()`, not after `send()`. Marking earlier would
lose signals silently: `send()` merely buffers, so a `flush()` that throws would
leave those signals marked-seen and never delivered, with nothing to retry.
Deferring means a failed run re-delivers rather than drops — at-least-once,
which is the right bias for a notifier.

A cron/launchd `exit 0` with an empty log cannot tell "healthy, nothing new"
from "source silently returned []" from "everything already deduped" — three
situations that call for very different responses. So `run()` prints exactly
one summary line on every invocation, including a zero-signal one and including
a *failing* one (issue #31).

"Unconditional" is the whole point, so the summary is emitted from a `finally`.
Every step here can raise — `fetch()`, `score()`, `send()`, `flush()` and
`seen.save()` — and a run that dies inside `fetch()` is exactly the case that
used to produce a nonzero exit with no diagnostic at all. The line reports how
far the run got before it died, and `status=` says which it was. The exception
still propagates; the summary does not suppress it.

On the counters, and on what is deliberately *not* here. `sent` counts signals
the destination accepted from `send()`. It is not a delivery confirmation: for a
batching destination `send()` only buffers and the real network call happens in
`flush()`, so naming it `delivered` would overclaim in the one feature meant to
stop the log lying. The runner also does not try to learn how many signals a
destination declined by its own gating (say a per-route min_score). That would
mean new optional protocol surface on `Destination`, which is wider than issue
#31 asked for, and absent that hook the number is simply unknowable — so
reporting `declined=0` by default would be a guess presented as a fact.

Note what this does *not* buy: `sent` and `new` are equal by construction on any
successful run, because a signal is counted and appended in the same iteration.
They diverge only on a failure path. So the line cannot tell you a destination
gated something internally, and nothing here pretends otherwise — a destination
that drops signals by its own rules has to say so in its own logs. Naming the
counter `sent` rather than `delivered` is the whole of the honesty available at
this layer.

`unscored` counts signals whose scorer raised `ScoreError`: a failure that
belongs to that one signal (an LLM reply that could not be read even after a
retry), not to the scorer as a whole. Aborting the run for it would let one
signal block delivery of every other signal for as long as it stayed in the
feed, since each run would replay it first and die at the same place. So the
runner skips it — not sent, not marked seen, so the next run retries it — and
prints one stderr line naming its id and the error. Everything else is
delivered, flushed and deduped as usual, then the run reports `status=partial`
and exits 1, so a scheduler still sees a nonzero exit and the operator still
has something to look at. `new` does not include unscored signals: on a run
that completes, `fetched` is already-seen plus `new` plus `unscored`. Any other
exception from `score()` still aborts the run with `status=failed`; a whole
model chain being down would fail every signal alike, and there is nothing to
salvage by carrying on.

The statuses, then: `ok` (exit 0), `partial` (exit 1, some signals unscored,
everything else delivered, the source's window not advanced), `commit_failed`
(the exception from `commit()` propagates; everything was delivered and marked
seen, only the source's window did not advance, so the next run re-fetches it
and SeenStore drops it all), and `failed` (the exception propagates, nothing
from this run is marked seen).
"""
from __future__ import annotations

import sys

from framework.interfaces import Destination, ScoreError, Scorer, Source
from framework.state import SeenStore


def run(
    source: Source,
    scorer: Scorer,
    destination: Destination,
    seen: SeenStore,
    agent_name: str = "unknown",
) -> int:
    fetched = 0
    new = 0
    sent = 0
    unscored = 0
    status = "failed"

    try:
        signals = source.fetch()
        fetched = len(signals)

        pending: list[str] = []
        for signal in signals:
            if seen.has(signal.id):
                continue
            try:
                scored = scorer.score(signal)
            except ScoreError as exc:
                # This signal only: not sent, not marked, so the next run
                # retries it. Anything else a scorer raises aborts the run.
                unscored += 1
                detail = str(exc).replace("\n", " ")
                print(
                    f"agent={agent_name} unscored id={signal.id} error={detail}",
                    file=sys.stderr,
                )
                continue
            destination.send(scored)
            sent += 1
            pending.append(signal.id)
        new = len(pending)

        flush = getattr(destination, "flush", None)
        if callable(flush):
            flush()

        # Only now is delivery known to have happened, for streaming and
        # batching destinations alike. Note seen.save() was always end-of-run,
        # so nothing is lost by deferring the marks to here.
        for signal_id in pending:
            seen.mark(signal_id)
        seen.save()

        if unscored:
            status = "partial"
        else:
            # A source that tracks its own fetch window advances it only here,
            # once delivery and the dedupe marks are both durable, and never on
            # a partial run: an unscored signal must still be inside the next
            # run's window to be retried (issue #46).
            commit = getattr(source, "commit", None)
            if callable(commit):
                # Delivered and marked already: if commit() raises, the line
                # must not say `failed`.
                status = "commit_failed"
                commit()
            status = "ok"
    finally:
        print(
            f"agent={agent_name} status={status} fetched={fetched} "
            f"new={new} sent={sent} unscored={unscored} seen={len(seen)}"
        )

    retval = 1 if unscored else 0
    return retval
