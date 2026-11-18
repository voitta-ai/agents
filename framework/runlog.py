"""Timestamp every line a run writes to its log (issue #139).

A run's log lines carried no date or time of any kind. The summary line, the
per-signal `scored` and `unscored` lines and whatever diagnostics a source
printed were all untimed, which meant the log could not answer the first
question anyone asks of a broken agent: when did this start?

Seven consecutive failed runs on the leads workspace produced seven
byte-identical lines. Nothing in the file distinguished a day-long outage from
three bad runs, and dating it took `stat` on `state/seen.json`, the log file
mtimes and the cadence out of the launchd plist -- three sources outside the
log, none of which survives log rotation or being read on another machine.

Worse, the plist is a tempting substitute and it lies. Reading
`StartCalendarInterval` and labelling lines with the times they *ought* to have
produces confident, fabricated timestamps; it was done during that
investigation and reported a 16:25 run at 11:21.

## Why the prefix is applied to the stream, not at each print

There are 37 `file=sys.stderr` call sites across the framework and a source
plugin is free to add more. Prefixing at the call site means every one of them
has to opt in, a new plugin silently opts out, and the timestamps drift apart
as each site formats its own. Wrapping the two streams once, for the duration
of the run, timestamps every line the run emits, including lines from plugin
code this module has never heard of.

A traceback is the exception to that and has to be handled by the caller. The
interpreter prints it only after the exception has left the block, by which
time the real streams are back, so the caller prints it from inside instead --
see `_cmd_run` in `framework/cli.py`. It is worth the four lines: a failed run's
traceback is the part that says why, and it would otherwise be the only untimed
thing in the file.

The wrapper buffers until it sees a newline because `print()` writes the text
and the terminator separately: a naive prefix-per-write would emit a timestamp
of its own in front of every bare `"\\n"`.

## The prefix goes in front, and the rest of the line is untouched

`grep ' scored id='` (note the leading space, which is what excludes the
`unscored id=` lines) and
`sed 's/.*score=\\([0-9]*\\).*/\\1/'` are how the score distribution is read,
and neither cares what precedes the match. Nothing anchors on `^agent=`, so a
leading field is the one change that costs no existing reader. The timestamp is
local time with its UTC offset -- an unmarked local time is the next version of
this bug, the first time the question is whether something happened before or
after a restart.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, TextIO


def _stamp() -> str:
    """The current local time as ISO-8601 with its UTC offset, to the second."""
    retval = datetime.now().astimezone().isoformat(timespec="seconds")
    return retval


class _Timestamped:
    """Wraps a text stream, prefixing each complete line with the local time.

    Everything other than `write` is delegated to the wrapped stream, so a
    caller that asks for `fileno()`, `encoding` or `isatty()` -- including the
    interpreter, printing a traceback -- sees the real stream's answer.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._pending = ""

    def write(self, text: str) -> int:
        buffered = self._pending + text
        lines = buffered.split("\n")
        # The last element is whatever follows the final newline: empty when
        # the text ended on one, a partial line otherwise. Hold it back so its
        # timestamp is the moment the line is finished rather than started.
        self._pending = lines.pop()
        for line in lines:
            # A blank line stays blank. Prefixing it would date nothing and
            # leave trailing whitespace on the only lines that exist to
            # separate things.
            prefix = f"{_stamp()} " if line else ""
            self._stream.write(f"{prefix}{line}\n")
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def close_pending(self) -> None:
        """Emit a held-back partial line, so nothing is swallowed on exit."""
        if self._pending:
            self._stream.write(f"{_stamp()} {self._pending}\n")
            self._pending = ""
        self._stream.flush()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


@contextmanager
def timestamped() -> Iterator[None]:
    """Timestamp every line written to stdout and stderr inside this block.

    Both streams are wrapped because the two halves of a run's log are split
    across them: launchd sends stdout to `run.log` and stderr to
    `run.error.log`, and the summary line goes to the first while the
    per-signal lines go to the second.

    The streams are restored on the way out whatever happened, and any partial
    line is flushed first -- a run that dies mid-line should still show how far
    it got.
    """
    out, err = _Timestamped(sys.stdout), _Timestamped(sys.stderr)
    sys.stdout, sys.stderr = out, err
    try:
        yield
    finally:
        out.close_pending()
        err.close_pending()
        sys.stdout, sys.stderr = out._stream, err._stream
