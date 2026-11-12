"""Signal (the messenger) transport and renderer — the counterpart of `slack.py`.

Everything this repo sends to Signal goes through `post_message` here, which
converts Markdown to Signal's own text styles on the way out and splits a long
message, exactly as `framework/slack.py` does for Slack's mrkdwn. Code that
builds a message writes Markdown; there is one place that renders it, per
platform.

## Why a subprocess, and why our own account

One process owns a Signal account at a time: `signal-cli`'s daemon holds it,
and a second invocation cannot open it concurrently. That is the same shape as
"one Socket Mode consumer per Slack app" in the README, and it decides the
architecture. An agent therefore talks to an account *this deployment owns*,
through the `signal-cli` binary, rather than borrowing a conversational agent's
account and depending on its relay.

Which dependency applies follows the transport. `signal-cli` needs no Python
package -- the dependency is the binary, whose presence on PATH is checked when
that transport is built, and that is all that is checked there. The gateway
transport needs no binary and uses `requests`, which this repo already
depends on, and it checks a good deal more before a run starts: the URL's
scheme, `wait_s` against the gateway's cap and against the HTTP timeout, the
label's length, and the token file's mode and kind. An account that is
not registered, or a group id this account is not a member of, costs a
subprocess and the account lock to verify, so it is left to fail on the first
send rather than on every run that sends nothing.

## Signal has no markup, it has ranges

Slack takes marked-up text. Signal takes **plain text plus a list of styled
ranges**, passed as `--text-style start:length:STYLE`. So `to_signal()` returns
both: the text with every marker removed, and the ranges that reproduce them.

Two consequences shaped the code:

- **Two units, each with one job.** Offsets are UTF-16 code units, which
  signal-cli's own help says in capitals: an emoji is two units, so a message
  with one emoji before a bold word would bold the wrong letters if offsets
  were counted in Python characters. The *limit*, though, is 2000 UTF-8
  **bytes** -- `ServiceConfig.MAX_MESSAGE_SIZE_BYTES`, tested in `ManagerImpl`
  as `Utf8.size(messageText()) > MAX_MESSAGE_SIZE_BYTES`. So `_units` counts
  offsets and `_bytes` counts capacity, and no function mixes them: in `_cut`
  the fit test is bytes while the offset it carries is units. Counting
  capacity in units instead under-counts every non-ASCII character -- the
  bullet this file writes is one unit and three bytes -- and signal-cli does
  not refuse the result, it truncates the text and attaches the tail as a
  file, so the mistake arrives as a mangled message rather than an error.
- **A range cannot be reopened across messages.** So a long message is
  rendered *once*, and the text and its ranges are split together: a bold span
  that straddles a part boundary becomes a bold range at the end of one part
  and another at the start of the next. Splitting the Markdown first would have
  been simpler and cuts a `**` in half when a single line is longer than the
  limit, leaving the markers visible and the span unstyled.

The styles Signal accepts are BOLD, ITALIC, STRIKETHROUGH, MONOSPACE and
SPOILER — verified against signal-cli 0.14.9, which rejects anything else with
`Invalid style: <name>`. There is no link style: Signal linkifies bare URLs
itself, so `[label](url)` renders as `label (url)` and the URL stays visible.
Headings become bold lines and list markers become bullets, the same choices
`slack.py` makes for the same reason.

`__x__` is deliberately not bold, matching `slack.py`: what this repo posts is
full of `__init__`, and `**bold**` is how anyone here writes bold.

A footer is appended after the conversion and never rendered, which is the
contract `slack.py` states: a standing compliance line means the characters it
was written with.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from time import time
from typing import Protocol

import requests

# Signal's own per-message limit, in UTF-8 bytes. A longer message is split
# into several, in order, the way `slack.py` splits on its block limit. Not
# UTF-16 units: signal-cli measures `Utf8.size` against this number, and past
# it splits the text itself and attaches the remainder, which is a success as
# far as the send is concerned and a truncated message as far as a reader is.
SIGNAL_MESSAGE_LIMIT = 2000

BULLET = "•"

# Room kept back for `(part 12 of 34)` plus its newline, so numbering a part
# can never push it over the limit. The same reservation `slack.py` makes. The
# marker is ASCII, so its width is the same number of bytes as characters.
_MARKER_ROOM = 24

# The longest label the gateway may be prefixing. "Leads: " is seven bytes;
# this is room for a name, not for content. See `_gateway_from_config` for
# what an unbounded one does to the splitter.
_MAX_LABEL_BYTES = 64

# How much longer than the gateway's own wait the HTTP timeout has to be. A
# gateway that answers a moment after its deadline must still be heard: the
# answer at that moment is a 202, and losing it to a socket timeout turns a
# message in flight into one the caller is told was refused.
_WAIT_MARGIN = 5

# Every status a gateway 200 can carry, all of which mean the message left:
# a retry that arrives after Signal has acked sees `delivered` or `read`, and
# `deleted` is a message that went out and was then deleted. Anything outside
# this set is a word this version has not been told the meaning of, and
# guessing at one would either lose a message or send it twice.
_GATEWAY_SENT = frozenset({"sent", "delivered", "read", "deleted"})

# Statuses a 200 can carry that mean the message has not left yet. The
# gateway returns 202 while a send is in flight, so these arrive only from a
# build that does not implement the `wait_s` this transport asks for -- its
# published schema allows `queued` on a 200 and an older deployment answered
# that way.
#
# They are not treated as pending, which is the thing to understand here. The
# gateway writes its idempotency entry when it accepts the message, with the
# status it has at that moment, and only the `wait_s` poll loop ever reports a
# later one -- the stored entry keeps saying `queued` forever. So on a build
# without that loop, every resubmit of this key replays the same `queued` and
# pending would never resolve: the run would fail on every schedule for the
# whole TTL, over a message that was very likely delivered on the first
# attempt, and then post a second copy once the key expired. A hard failure
# naming the cause is the honest answer, because the fix is a gateway build
# and no amount of retrying substitutes for it.
_GATEWAY_PENDING = frozenset({"queued"})

# `failed` on a 200 would contradict the status code, but the gateway's
# published enum allows it and the honest reading is the status, not the code:
# a message the gateway says failed did not go out. Kept separate from the
# unknown-status path so the error says what happened rather than that this
# version cannot tell.
_GATEWAY_FAILED = frozenset({"failed"})

# `received` belongs to an inbound message and should never come back from a
# send. If it does, the message exists and has not been sent, which is the
# same position as `queued` -- and the same frozen-entry problem, so it is
# read the same way rather than as something this version has not been told.
_GATEWAY_INBOUND = frozenset({"received"})

# Every status the gateway's published enum declares, for the error that says
# what this version understands.
_known = sorted(_GATEWAY_SENT | _GATEWAY_PENDING | _GATEWAY_FAILED | _GATEWAY_INBOUND)

BOLD = "BOLD"
ITALIC = "ITALIC"
STRIKETHROUGH = "STRIKETHROUGH"
MONOSPACE = "MONOSPACE"
SPOILER = "SPOILER"

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")

_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|~])")
_CODE_SPAN_RE = re.compile(r"`+([^`]*)`+")
_LINK_RE = re.compile(
    r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*(?:<([^<>\s]*)>|([^()\s]+))(?:\s+\"[^\"]*\")?\s*\)"
)
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^<>\s]+)>")
_BOLD_ITALIC_RE = re.compile(r"(?<![\w*])\*\*\*(\S(?:.*?\S)?)\*\*\*(?![\w*])", re.S)
_BOLD_RE = re.compile(r"(?<![\w*])\*\*(\S(?:.*?\S)?)\*\*(?![\w*])", re.S)
_STRIKE_RE = re.compile(r"~~(\S(?:.*?\S)?)~~", re.S)
_SPOILER_RE = re.compile(r"\|\|(\S(?:.*?\S)?)\|\|", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])")
# One underscore each side, never two. See the module docstring.
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!_)(\S(?:[^_\n]*\S)?)_(?!_)(?![\w])")

# Anything that looks like a phone number, for the scrubber. signal-cli reports
# a failed send to a group by naming the member it could not reach.
_NUMBER_RE = re.compile(r"\+\d{7,15}")

# Tried in order at each position; the earliest match in the line wins, and a
# tie goes to the pattern listed first. A backslash escape comes first so that
# `\*literal\*` is neither emphasised nor left with its backslashes: the escape
# consumes both markers before any emphasis rule can pair them. Code spans and
# links come next so that `*` inside a URL or a code span is left alone.
_INLINE_RULES: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (_ESCAPE_RE, None),
    (_CODE_SPAN_RE, MONOSPACE),
    (_LINK_RE, None),
    (_AUTOLINK_RE, None),
    (_BOLD_ITALIC_RE, "BOLD+ITALIC"),
    (_BOLD_RE, BOLD),
    (_STRIKE_RE, STRIKETHROUGH),
    (_SPOILER_RE, SPOILER),
    (_ITALIC_STAR_RE, ITALIC),
    (_ITALIC_UNDER_RE, ITALIC),
)

# A match whose content is re-scanned for nested markup. A code span is not in
# this set: inside one, markers are literal text.
_RECURSIVE = frozenset({"BOLD+ITALIC", BOLD, STRIKETHROUGH, SPOILER, ITALIC})


class SignalSendError(RuntimeError):
    """A Signal message did not go out. Nothing downstream may mark it sent."""


class SignalCliError(SignalSendError):
    """signal-cli refused or failed. The message carries its stderr, scrubbed."""


class SignalGatewayError(SignalSendError):
    """The gateway refused the send, or could not be reached."""


class SignalSendPending(SignalSendError):
    """The gateway accepted the message and Signal had not taken it yet.

    A sibling of `SignalGatewayError` rather than a kind of it, because the
    obvious consumer -- `except SignalGatewayError:  # refused, give up` --
    would otherwise swallow the one case that is not a refusal. The message
    may still arrive. A run-and-exit caller cannot wait to find out, so it
    must neither mark the signal delivered nor report success: the next run
    resubmits it under the same idempotency key, which waits on the message
    already in flight instead of sending a second one.

    That promise is bounded by the gateway's `idempotency_ttl_s`, 24 hours by
    default. A caller whose runs are further apart than the TTL gets a fresh
    key for the same content, and the resubmit then posts a second copy.
    """


def _units(text: str) -> int:
    """Length in UTF-16 code units, which is what Signal's offsets count.

    The one place this repo counts offsets: style ranges and mention
    positions. Capacity is `_bytes`, because the limit is measured in UTF-8
    bytes instead -- the two are different questions and mixing them is the
    bug this split prevents. Python's `len` counts code points, so an emoji
    (two units) ahead of a styled span would shift every range after it by one
    and style the wrong characters.
    """
    retval = len(text.encode("utf-16-le")) // 2
    return retval


def _bytes(text: str) -> int:
    """Length in UTF-8 bytes, which is what Signal's limit counts.

    The one place this repo measures capacity for Signal. Separate from
    `_units` on purpose: the limit and the offsets are different units, and
    the only way to keep them straight is to keep them in different
    functions. Every non-ASCII character makes them disagree -- the bullet is
    one unit and three bytes, an em-dash likewise, an emoji two units and
    four bytes.
    """
    retval = len(text.encode("utf-8"))
    return retval


# The longest footer that still leaves room for the message it rides on. A
# footer rides on every part, costing its length plus the newline before it,
# and a split message also carries a part marker. See `check_footer`.
MAX_FOOTER_BYTES = SIGNAL_MESSAGE_LIMIT - _MARKER_ROOM - 2


def check_footer(
    footer: str, label: str = "signal footer", limit: int = SIGNAL_MESSAGE_LIMIT
) -> None:
    """Refuse a footer that leaves no room for the message it rides on.

    Without this the arithmetic in `post_message` runs out of room and then
    goes negative, and a splitter given a non-positive limit emits one
    character per part: a single scored signal becomes thousands of Signal
    messages, each one of them under the limit, so nothing downstream objects
    and the run reports success. `slack.py` refuses the same shape for the same
    reason, and is checked where the config is read so it fails before the run
    has paid for a fetch or an LLM call.

    `limit` is Signal's own unless a caller is posting under a smaller one, in
    which case the room a footer may take shrinks with it: a check against the
    constant would pass a footer that leaves the caller's limit nothing.
    """
    room = limit - _MARKER_ROOM - 2
    if _bytes(footer) > room:
        raise ValueError(
            f"{label} is {_bytes(footer)} UTF-8 bytes, which leaves no room for content "
            f"under {'Signal' if limit == SIGNAL_MESSAGE_LIMIT else 'this caller'}'s "
            f"{limit}-byte limit"
        )


class _Builder:
    """Accumulates plain text and the style ranges over it."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.ranges: list[tuple[int, int, str]] = []
        self._len = 0

    @property
    def text(self) -> str:
        retval = "".join(self.parts)
        return retval

    def write(self, text: str) -> None:
        if not text:
            return
        self.parts.append(text)
        self._len += _units(text)

    def mark(self) -> int:
        retval = self._len
        return retval

    def style(self, start: int, style: str) -> None:
        """Style everything written since `start`. Empty spans are dropped."""
        length = self._len - start
        if length <= 0:
            return
        for name in style.split("+"):
            self.ranges.append((start, length, name))


def _link_text(match: re.Match[str]) -> str:
    """`[label](url)` as Signal shows it: the label, then the URL in brackets.

    Signal linkifies a bare URL and has no link style, so hiding the URL behind
    a label would lose it entirely. A label equal to the URL, or absent, leaves
    the URL alone rather than printing it twice.
    """
    label = (match.group(1) or "").strip()
    url = (match.group(2) or match.group(3) or "").strip()
    if not url:
        retval = label
        return retval
    if not label or label == url:
        retval = url
        return retval
    retval = f"{label} ({url})"
    return retval


def _inline(text: str, out: _Builder) -> None:
    """Convert one line's inline markup, writing text and ranges into `out`."""
    position = 0
    while position < len(text):
        best: tuple[int, re.Match[str], str | None] | None = None
        for pattern, style in _INLINE_RULES:
            found = pattern.search(text, position)
            if found is None:
                continue
            if best is None or found.start() < best[0]:
                best = (found.start(), found, style)
        if best is None:
            out.write(text[position:])
            return
        start, match, style = best
        out.write(text[position:start])
        mark = out.mark()
        if style is None:
            out.write(_link_text(match) if match.re is _LINK_RE else match.group(1))
        elif style in _RECURSIVE:
            _inline(match.group(1), out)
            out.style(mark, style)
        else:
            out.write(match.group(1))
            out.style(mark, style)
        position = match.end()


def to_signal(text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Markdown in, plain text plus `(start, length, STYLE)` ranges out.

    Block rules, matching the choices `slack.py` makes: a fenced block becomes
    monospace with its fences removed, a heading becomes a bold line, and a list
    marker becomes a bullet. Everything else is inline markup.
    """
    out = _Builder()
    lines = text.split("\n")
    index = 0
    first = True
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line):
            index += 1
            block: list[str] = []
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                block.append(lines[index])
                index += 1
            index += 1  # the closing fence, or the end of the text
            if not first:
                out.write("\n")
            mark = out.mark()
            out.write("\n".join(block))
            out.style(mark, MONOSPACE)
            first = False
            continue
        if not first:
            out.write("\n")
        first = False
        heading = _HEADING_RE.match(line)
        if heading:
            mark = out.mark()
            _inline(heading.group(2), out)
            out.style(mark, BOLD)
            index += 1
            continue
        listed = _LIST_RE.match(line)
        if listed:
            out.write(f"{listed.group(1)}{BULLET} ")
            _inline(listed.group(2), out)
            index += 1
            continue
        _inline(line, out)
        index += 1
    retval = (out.text, out.ranges)
    return retval


def _segments(text: str, sep: str, base: int = 0) -> list[tuple[str, int]]:
    """Each piece of `text` split on `sep`, with its UTF-16 offset in the whole.

    The offsets are the point: a part's styles are the ranges that fall inside
    it, so a split has to say where each piece came from.
    """
    retval = []
    offset = base
    for piece in text.split(sep):
        retval.append((piece, offset))
        offset += _units(piece) + _units(sep)
    return retval


def _cut(text: str, offset: int, limit: int) -> list[tuple[str, int]]:
    """Cut inside a line, on a code-point boundary, when nothing coarser fits.

    A code point is never split, so a surrogate pair cannot be halved into two
    messages of invalid text, and no part can end with half a UTF-8 sequence.

    Both units appear here, which is why they are named: the test is whether
    another character still fits under the byte limit, and the number carried
    forward is the piece's offset in UTF-16 units for its style ranges.
    """
    retval: list[tuple[str, int]] = []
    current = ""
    start = offset
    for char in text:
        if current and _bytes(current) + _bytes(char) > limit:
            retval.append((current, start))
            start += _units(current)
            current = ""
        current += char
    if current:
        retval.append((current, start))
    return retval


def _pack(pieces: list[tuple[str, int]], sep: str, limit: int, finer) -> list[tuple[str, int]]:
    """Greedily join `pieces` into parts of at most `limit` bytes.

    A piece too long on its own is handed to `finer`, which splits it on the
    next boundary down.
    """
    parts: list[tuple[str, int]] = []
    current: tuple[str, int] | None = None
    for piece, offset in pieces:
        if current is not None:
            candidate = f"{current[0]}{sep}{piece}"
            if _bytes(candidate) <= limit:
                current = (candidate, current[1])
                continue
            parts.append(current)
            current = None
        if _bytes(piece) <= limit:
            current = (piece, offset)
            continue
        parts.extend(finer(piece, offset, limit))
    if current is not None:
        parts.append(current)
    retval = parts
    return retval


def _split_bytes(text: str, limit: int) -> list[tuple[str, int]]:
    """`text` in parts of at most `limit` UTF-8 bytes, each with its offset.

    Paragraph boundaries first, then line boundaries, then inside a line only
    when one line is itself longer than the limit. Never truncates. The offset
    that comes back with each part is in UTF-16 units, because it addresses
    style ranges rather than capacity.
    """
    if limit < 1:
        raise SignalSendError(
            f"signal message split asked for parts of {limit} bytes; "
            "a footer or a marker has eaten the whole message"
        )
    if _bytes(text) <= limit:
        retval = [(text, 0)]
        return retval

    def lines(piece: str, offset: int, room: int) -> list[tuple[str, int]]:
        inner = _pack(_segments(piece, "\n", offset), "\n", room, _cut)
        return inner

    retval = _pack(_segments(text, "\n\n"), "\n\n", limit, lines)
    return retval


def _clip(
    ranges: list[tuple[int, int, str]], start: int, length: int
) -> list[tuple[int, int, str]]:
    """The styles covering `[start, start + length)`, rebased on that part.

    A range that straddles the boundary is kept on both sides, clipped to each:
    Signal has no way to reopen a style in the next message, so the two halves
    of a bold span are two bold ranges.
    """
    retval = []
    for range_start, range_length, style in ranges:
        low = max(range_start, start)
        high = min(range_start + range_length, start + length)
        if high > low:
            retval.append((low - start, high - low, style))
    return retval


def _short_digest(text: str) -> str:
    """A few bytes of hash, to tell two configurations apart inside a key."""
    retval = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return retval


def _as_text(value: object) -> str:
    """Whatever a subprocess attribute holds, as a string."""
    if value is None:
        retval = ""
    elif isinstance(value, bytes):
        retval = value.decode("utf-8", "replace")
    else:
        retval = str(value)
    return retval


class Transport(Protocol):
    """What `post_message` needs from whatever carries the message.

    `limit` belongs to the transport rather than to the splitter because only
    the transport knows what the text will be wrapped in: the gateway may put
    a label in front of every message, and those bytes come out of the same
    2000.
    """

    limit: int
    # Whether a resubmitted part is deduplicated by the far side. False for
    # signal-cli, which has nothing to deduplicate against, so a retry after
    # a partial send posts the earlier parts a second time. `post_message`
    # tells a caller which of those two situations it is in rather than
    # asserting the safe one for both.
    idempotent: bool

    def send(
        self, text: str, ranges: list[tuple[int, int, str]], seed: str = ""
    ) -> int | str:
        """Send one rendered message; return the handle it is addressed by.

        `seed` identifies *which* message this is, independently of how it
        reads. A transport that deduplicates resubmits needs that: see
        `GatewayTransport._key`. A transport that does not may ignore it.

        Which handle depends on the transport, and neither is interchangeable
        with the other: signal-cli hands back Signal's own timestamp, an int,
        which is what an edit through signal-cli would take. The gateway hands
        back its own `message_id`, a string, which is what its edit, delete
        and reaction endpoints take -- it keeps the Signal timestamp itself. A
        zero means the message went out and the transport did not say which.
        """
        ...


class SignalTransport:
    """Where a Signal message goes: one group, through the `signal-cli` binary.

    Group only, deliberately. The approval conversation this exists for is a
    dedicated group, and a one-to-one recipient is a second target shape with
    its own trusted-sender question; it can be added when something needs it.
    """

    def __init__(
        self,
        account: str,
        group_id: str,
        binary: str = "signal-cli",
        timeout: int = 120,
    ):
        self.account = account
        self.group_id = group_id
        self.binary = binary
        self.timeout = timeout
        self.limit = SIGNAL_MESSAGE_LIMIT
        self.idempotent = False

    def _scrub(self, text: str) -> str:
        """Keep the account, the group and any phone number out of what we raise.

        Both the account and the group id are named by environment variable
        rather than written in config because each identifies a private
        conversation -- so neither belongs in a failure either. signal-cli
        echoes the group id back on a bad-group error, and names the member it
        could not reach when a send to a group fails, and what this raises
        reaches logs, CI output and issue paste-ins.
        """
        retval = text
        for secret, placeholder in ((self.account, "<account>"), (self.group_id, "<group>")):
            if secret:
                retval = retval.replace(secret, placeholder)
        retval = _NUMBER_RE.sub("<number>", retval)
        return retval

    def send(
        self, text: str, ranges: list[tuple[int, int, str]], seed: str = ""
    ) -> int:
        """Send one already-rendered message. Returns its Signal timestamp.

        `seed` is accepted and ignored: signal-cli has nothing to deduplicate
        a resubmit against, which is what `idempotent = False` says.

        That timestamp *is* the message id in Signal -- the counterpart of a
        Slack `ts`, and the only handle an edit could later be made with. A
        zero means the message went out and signal-cli did not say which one it
        was; see below.

        The text goes in on stdin rather than in `argv`: a digest is longer than
        a comfortable argument, and a message full of quotes and `$(...)` must
        reach signal-cli as itself.
        """
        argv = [
            self.binary,
            "--output=json",
            "-a",
            self.account,
            "send",
            "-g",
            self.group_id,
            "--message-from-stdin",
        ]
        if ranges:
            argv.append("--text-style")
            argv.extend(f"{start}:{length}:{style}" for start, length, style in ranges)
        try:
            done = subprocess.run(
                argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Not `from exc`: TimeoutExpired renders the whole argv, which
            # holds `-a <number>` and `-g <group>`, and this exception reaches
            # an uncaught traceback. Whatever signal-cli managed to say is
            # worth keeping, scrubbed, because at the default timeout on a
            # multi-part message it is the only clue which part hung.
            said = self._scrub(_as_text(exc.stderr) or _as_text(exc.stdout)).strip()[-800:]
            raise SignalCliError(
                f"{self.binary} did not answer within {self.timeout}s"
                + (f"; it had said: {said}" if said else "")
            ) from None
        if done.returncode != 0:
            tail = self._scrub((done.stderr or done.stdout or "").strip())[-800:]
            raise SignalCliError(f"signal-cli exited {done.returncode}: {tail}")
        # Read line by line: signal-cli's `--output=json` is line-oriented on
        # other commands, so a second line one day must not discard a timestamp
        # that was on the first.
        for line in (done.stdout or "").splitlines():
            if not line.strip():
                continue
            try:
                body = json.loads(line)
            except ValueError:
                continue
            if isinstance(body, dict) and "timestamp" in body:
                try:
                    retval = int(body["timestamp"])
                    return retval
                except (TypeError, ValueError):
                    break
        # A zero exit means signal-cli accepted the message and handed it to
        # Signal. If its output does not say which message, that is a reply
        # shape this code does not know -- not a failed send. Raising here
        # would abort the run *after* delivery, leave the signal unmarked, and
        # post it again on the next run: a permanent duplicate for a parsing
        # question. So this returns 0, meaning delivered, id unknown, the same
        # call this repo makes for a tweet that returns 2xx without an id.
        return 0


class GatewayTransport:
    """Where a Signal message goes: one conversation, through voitta-gateway.

    The gateway owns the Signal account. Only one process can, so this and
    `SignalTransport` are alternatives rather than layers: once the gateway's
    daemon holds the account, the subprocess path cannot open it.

    Three things are deliberately not here. The group id never appears -- the
    gateway maps an alias such as `leads` to it, so a private conversation's
    id does not travel to its consumers or into their logs. The token is read
    from a 0600 file rather than the environment, because a run spawns LLM
    CLIs as children and a variable in the environment is visible to all of
    them. And nothing polls for delivery: a run-and-exit caller cannot wait,
    so it asks the gateway to wait instead.
    """

    def __init__(
        self,
        base_url: str,
        conversation: str,
        token: str,
        label: str = "",
        wait_s: int = 10,
        timeout: int = 120,
        fingerprint: str = "",
        repeats: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.conversation = conversation
        self.label = label
        self.wait_s = wait_s
        self.timeout = timeout
        self._token = token
        # Which destination this is, as far as anything stable can say. See
        # `_key`: the gateway's idempotency store is namespaced by consumer,
        # so two destinations behind one token need to differ here.
        self.fingerprint = fingerprint
        # Whether this destination deliberately sends the same signal more
        # than once. See `_key`: it decides whether the gateway's replay of
        # an earlier message is the retry this design wants or a delivery
        # quietly dropped.
        self.repeats = repeats
        self.idempotent = not repeats
        # One value per process, so every part of every message in this run
        # shares it and a retry within the run would still match.
        self._run = f"{int(time()):x}-{os.getpid():x}"
        # A label is prepended by the gateway to every message it sends, so
        # its bytes are not available to the body. `_bytes` and not `len`,
        # for the reason the whole module says.
        #
        # This value only *mirrors* the gateway's: the prefix comes from its
        # own `consumers.<name>.label`, no endpoint reports that back, and the
        # gateway re-checks the length after prefixing. So a label configured
        # there and missing here means every full-size part is refused with
        # 422 `too_long` -- which `send` names as a likely cause, because the
        # arithmetic looks right from this side and the mismatch is the only
        # thing that explains it.
        self.limit = SIGNAL_MESSAGE_LIMIT - (_bytes(f"{label}: ") if label else 0)

    def _scrub(self, text: str) -> str:
        """Keep the token out of whatever this raises.

        Less to hide than the subprocess transport -- there is no account
        number or group id here, by design -- but a gateway that echoed a bad
        Authorization header back would otherwise put the token in a
        traceback, a log and any issue it is pasted into.
        """
        retval = text
        if self._token:
            retval = retval.replace(self._token, "<token>")
        retval = _NUMBER_RE.sub("<number>", retval)
        return retval

    def _warn_replayed(self, status: str, seed: str) -> None:
        """Say that the gateway replayed a message rather than sending one.

        The gateway found this idempotency key in its store and answered with
        the earlier message's outcome; nothing new went out. For the retry
        this design asks for, that is exactly right. For a caller sending the
        same signal twice inside `idempotency_ttl_s` it is a silent no-op
        reported as success, and the flag is the only evidence either way --
        so it is said out loud, from one place, before any of `send`'s
        returns.
        """
        keyed = (
            f"the id {seed!r} this part was sent under"
            if seed
            else "derived from this part's text and styles"
        )
        print(
            f"signal gateway replayed an earlier message for this part "
            f"(duplicate=true, status {status or 'unreported'!r}); nothing new was sent. "
            f"The key is {keyed}, and the gateway remembers it for its idempotency TTL -- "
            f"so a deliberate repeat, as opposed to a retry, keeps being replayed until "
            f"that TTL expires"
            + (
                ", and editing the message will not change that, because the key does "
                "not depend on the text"
                if seed
                else ""
            ),
            file=sys.stderr,
        )

    def _key(
        self,
        text: str,
        ranges: list[tuple[int, int, str]],
        seed: str = "",
        salt: str = "",
    ) -> str:
        """An idempotency key for this message to this conversation.

        The retry a `SignalSendPending` asks for has to carry the same key as
        the send it is retrying, or the gateway has nothing to match and the
        parts that did arrive are posted again.

        So `seed` is preferred over the content when a caller supplies one,
        because the content is not stable across runs in this framework: a
        retry re-scores, `llm_scorer` appends the rung the vendor waterfall
        landed on to its reasons, and the destination renders those reasons
        into the text. A different rung, or any drift in a model's wording,
        changes the text and therefore a content-derived key -- and the part
        that already reached the group is sent a second time. A signal's `id`
        does not move for either reason.

        The material also holds this transport's `fingerprint`, a digest of
        the workspace it was built in, and the seed `post_message` builds
        carries a digest of the footer. Both are there because the
        gateway namespaces its idempotency store by consumer, not by
        destination: two destinations sharing one gateway token and one
        conversation are one consumer to it, so the same signal would
        otherwise produce the same key from both, and the second one's
        message -- its own footer, its own reasons -- would come back as a
        replay of the first, reported as delivered and never sent. The footer
        alone does not settle it, since an empty or shared footer is exactly
        the case that collides; the workspace does, and it is stable against
        the config edits an operator makes while chasing a failure. Two
        destinations with the same footer in one workspace and one
        conversation still collide -- and are the same message to the same
        people, the one case where a replay is the right answer. Beyond that,
        give each agent its own gateway consumer and token, which is what the
        namespacing is for.

        The seed `post_message` builds is
        `<id>@<footer digest>#<number>-of-<total>`, optionally with a run
        nonce (see `repeats`), and the total is in it on purpose. A key that
        named only the part number would match across runs that split
        differently, and the worse direction of
        that is silent: a retry whose text shortened into one part would carry
        the key of the old part 1, the gateway would replay that part, this
        side would read a 200 and mark the signal delivered, and everything
        that had been in part 2 would never be sent. Including the total means
        a different split is a different message, which is what it is.

        What remains, and is the lesser of the two: a retry that splits the
        same way but reads differently matches the parts that already arrived,
        so the group keeps the first run's wording for those and takes the new
        wording for the rest. Nothing is lost -- the same signal is described
        either way -- and it is strictly better than a duplicate or a hole.

        `salt` moves a key off an entry the gateway can no longer honour --
        see the `message_gone` branch in `send`. It is not a retry knob: the
        whole value of the key is that a retry reuses it.

        Without a seed it falls back to the content, which is right for a
        caller that has no stable id to offer. The digest covers the styles as
        well as the text, since two parts of one message can hold the same
        words under different emphasis.
        """
        if seed and self.repeats:
            # A source that re-lists the same items every run means to send
            # them again, and the gateway cannot tell that from a retry: both
            # arrive as the same key inside its 24-hour window, and it
            # answers the second one by replaying the first. Reported as
            # delivered, never sent. So a destination that repeats itself
            # gets a key that is unique per run, and gives up the
            # cross-run retry guarantee in exchange -- which is the trade
            # `idempotent` then reports, so a partial send says that
            # resubmitting repeats the parts that arrived.
            seed = f"{seed}!{self._run}"
        material = json.dumps(
            [self.conversation, self.label, self.fingerprint, salt, seed] if seed
            else [self.conversation, self.label, self.fingerprint, salt, text, ranges],
            ensure_ascii=False,
            sort_keys=True,
        )
        retval = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return retval

    def send(
        self, text: str, ranges: list[tuple[int, int, str]], seed: str = ""
    ) -> int | str:
        """Send one rendered message, waiting for Signal to take it.

        `seed` identifies the message independently of how it reads, and is
        what the idempotency key is built from when a caller gives one. See
        `_key`.

        Returns the gateway's `message_id` on a 200. Raises on anything else,
        so a caller that marks a signal delivered only when this returns can
        never mark one that did not arrive:

        - 200 means the message left, under any of `_GATEWAY_SENT`, or --
          from a build without `wait_s` -- that it has not left yet, under
          `_GATEWAY_PENDING`. A 200 carries no Signal timestamp: the gateway
          keeps that and addresses the message by `message_id`, which is what
          its edit, delete and reaction endpoints take. A `duplicate` flag
          means the key was found in the gateway's idempotency store and the
          earlier outcome replayed, so nothing new was sent.
        - 502 means the send was refused for good; retrying it is pointless
          until whatever was wrong is fixed.
        - 202 means it was still in flight when the gateway's own deadline
          passed, which includes a retry of a message still in flight. Not a
          failure and not a success, so it raises `SignalSendPending` and the
          next run resubmits the same key.
        """
        # The ranges go in as offsets into the text as submitted, not
        # shifted for a label. The gateway rebases them itself when it
        # prefixes one -- `_labelled` adds `units(prefix)` to every style and
        # mention start -- so adjusting here would move them twice. Only the
        # label's *bytes* are this side's business, in `limit`.
        payload = {
            "to": {"conversation": self.conversation},
            "parts": [
                {
                    "type": "text",
                    "text": text,
                    "styles": [
                        {"start": start, "length": length, "style": style}
                        for start, length, style in ranges
                    ],
                }
            ],
            "idempotency_key": self._key(text, ranges, seed),
            "wait_s": self.wait_s,
        }
        response = self._post(payload)
        if response.status_code == 409 and "message_gone" in (response.text or ""):
            # The gateway still holds this idempotency key but has lost the
            # message it names -- its data was reset, or a retention it did
            # not have yet removed the row. The key is derived, so every run
            # would compute it again and get this same 409 until it expires:
            # a message that is not delivered and, from this side, cannot be.
            # So the key is moved once, deliberately, and the send retried.
            # The cost is a duplicate if the original did reach the group
            # before the reset; the alternative is a silent gap for the rest
            # of the TTL, and a duplicate someone can see is the better of
            # the two. Once, not in a loop: a second `message_gone` under a
            # fresh key is a gateway problem, not a key problem.
            print(
                "signal gateway lost the message behind this idempotency key "
                "(message_gone); sending once more under a fresh key, which may "
                "duplicate it if the original went out before the gateway's data was "
                "reset",
                file=sys.stderr,
            )
            payload["idempotency_key"] = self._key(text, ranges, seed, salt="message_gone")
            response = self._post(payload)
        said = self._scrub((response.text or "").strip())
        tail = said[-400:]
        return self._read(response, said, tail, seed)

    def _post(self, payload: dict) -> requests.Response:
        """One POST to the gateway, with every failure shape classified."""
        try:
            response = requests.post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self.timeout,
            )
            return response
        except requests.ConnectTimeout as exc:
            # No connection was established, so nothing was sent.
            # Not `from exc`: a requests exception renders the request, and
            # the Authorization header is on it.
            raise SignalGatewayError(
                f"signal gateway at {self.base_url} did not accept a connection within "
                f"{self.timeout}s; nothing was sent: {self._scrub(str(exc))[-300:]}"
            ) from None
        except (requests.exceptions.SSLError, requests.exceptions.ProxyError) as exc:
            # Both are `ConnectionError` subclasses and would otherwise be
            # caught below as pending. They are not ambiguous: a handshake
            # that failed or a proxy that refused the tunnel means the request
            # never went, and reporting a misconfigured certificate as a
            # message possibly in flight would hide it run after run.
            raise SignalGatewayError(
                f"signal gateway at {self.base_url} could not be reached securely, so "
                f"nothing was sent: {self._scrub(str(exc))[-300:]}"
            ) from None
        except (
            requests.ReadTimeout,
            requests.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ):
            # Everything else that fails around a connection leaves the
            # message in doubt: a read deadline passing, a gateway restarting
            # or a proxy resetting while it holds the response for `wait_s`, a
            # truncated body, a refused connection. The window the gateway
            # spends holding a reply is exactly where a restart lands, and
            # `requests` raises one class for a reset at that moment and for a
            # port that was never open -- telling them apart means reading
            # urllib3's cause chain, which is not worth depending on for a
            # message's wording. Pending is the answer that is true either
            # way: it marks nothing and resubmits under the same key, which
            # the gateway matches if the message did get through, and which is
            # also what a caller should do when the gateway is simply down.
            raise SignalSendPending(
                f"the exchange with the signal gateway at {self.base_url} did not complete "
                f"within {self.timeout}s. If the request reached it the message may be "
                f"queued, so it stays unmarked and the next run resubmits it under the "
                f"same key"
            ) from None
        except requests.RequestException as exc:
            raise SignalGatewayError(
                f"signal gateway at {self.base_url} did not answer: "
                f"{self._scrub(str(exc))[-400:]}"
            ) from None
        # Matched on the whole body and truncated only for display: a 422
        # that echoes the part it refused pushes the marker out of a 400-byte
        # tail, and the one failure this transport can explain would fall
        # through to the generic relay.
    def _read(
        self, response: requests.Response, said: str, tail: str, seed: str
    ) -> int | str:
        """What the gateway's answer means. See `send` for the contract."""
        if response.status_code == 202:
            raise SignalSendPending(
                f"signal gateway queued the message but Signal had not taken it within "
                f"{self.wait_s}s; it is not delivered and not failed, so it stays unmarked "
                f"and the next run resubmits it: {tail}"
            )
        if response.status_code == 422 and "too_long" in said:
            # The arithmetic on this side reserved room for `self.label`. If
            # the gateway still finds the part too long, the label it is
            # actually configured with is longer than the one named here --
            # it re-checks after prefixing, and nothing reports its value
            # back, so this is the one failure worth explaining rather than
            # relaying.
            raise SignalGatewayError(
                f"signal gateway refused a part as too long although this side split at "
                f"{self.limit} bytes, reserving room for the label {self.label!r}. The "
                f"gateway prefixes its own consumers.<name>.label and re-checks the length "
                f"afterwards, so that label is probably longer than this one, or set when "
                f"this config has none: {tail}"
            )
        if response.status_code == 502 and "send_failed" in said:
            raise SignalGatewayError(
                f"signal gateway answered 502 send_failed, refusing the message for "
                f"good: {tail}"
            )
        if response.status_code >= 500:
            # Not a refusal. The gateway's own "refused for good" is a 502
            # carrying `send_failed`, handled above; any other 5xx is a
            # gateway that crashed or a proxy that gave up, and it lands in
            # the window the gateway spends holding the response for
            # `wait_s` -- the same ambiguity the connection branch above
            # treats as pending, and for the same reason: the message may
            # already be enqueued.
            raise SignalSendPending(
                f"signal gateway answered {response.status_code}, which is not its own "
                f"refusal. The message may already be queued, so it stays unmarked and "
                f"the next run resubmits it under the same key: {tail}"
            )
        if response.status_code != 200:
            raise SignalGatewayError(
                f"signal gateway answered {response.status_code}: {tail}"
            )
        try:
            body = response.json()
        except ValueError:
            raise SignalGatewayError(
                f"signal gateway answered 200 with a body that is not JSON: {tail}"
            ) from None
        if not isinstance(body, dict):
            raise SignalGatewayError(f"signal gateway answered 200 with {type(body).__name__}")
        status = str(body.get("status") or "")
        if body.get("duplicate"):
            self._warn_replayed(status, seed)
        if not status:
            # Absent, null or empty: all three are the gateway not saying,
            # which is not the same as saying something this version does not
            # know. The same rule applies as to a missing `message_id`: a 200
            # is the gateway saying the message left, and raising over a field
            # it did not fill would discard a delivered message's mark and
            # post it again next run -- or, once the idempotency store
            # replays this same statusless 200, fail identically every run
            # until the TTL expires and then duplicate. The handle is still
            # returned if there is one; only the status was missing.
            retval = str(body.get("message_id") or "") or 0
            return retval
        if status in _GATEWAY_FAILED:
            raise SignalGatewayError(
                f"signal gateway answered 200 but reports the message {status!r}; it did "
                f"not go out: {tail}"
            )
        if status in _GATEWAY_PENDING or status in _GATEWAY_INBOUND:
            raise SignalGatewayError(
                f"signal gateway answered 200 with status {status!r}, which means the "
                f"message has not left and it did not honour the {self.wait_s}s wait this "
                f"send asked for -- an older build. "
                f"The message may well have gone out, but this cannot be established from "
                f"here: the gateway's idempotency entry keeps the status it was written "
                f"with, so resubmitting replays this same answer until the entry expires "
                f"and then sends a second copy. Check the conversation by hand and deploy "
                f"a gateway that implements wait_s: {tail}"
            )
        if status not in _GATEWAY_SENT:
            raise SignalGatewayError(
                f"signal gateway answered 200 with status {status!r}, which this "
                f"version does not know how to read; it knows {', '.join(_known)}: {tail}"
            )
        handle = str(body.get("message_id") or "")
        if not handle:
            # Delivered, id unknown: the same call the subprocess transport
            # makes when signal-cli accepts a message without naming it.
            # Raising here would discard a delivered message's mark and post
            # it again next run.
            return 0
        retval = handle
        return retval


def _read_token_file(path: str, label: str) -> str:
    """The token in a 0600 file, refusing one anybody else can read.

    The mode is checked rather than assumed: a token readable by another
    account is a token to treat as disclosed, and the moment to notice is
    before the run starts rather than after it has posted.
    """
    try:
        # O_NOFOLLOW, and the mode read from the descriptor rather than the
        # path: `os.stat` follows a symlink, so a world-writable link to a
        # 0600 file would pass a check on the target. Opening first and
        # calling fstat also closes the gap between checking a path and
        # reading it. The open is inside the guard because a file that stats
        # and will not open -- 0400 owned by another account, root-owned, a
        # directory at that path -- is exactly the early, labelled failure
        # this function exists to produce.
        # O_NONBLOCK so a FIFO at this path fails instead of hanging the run
        # with no timeout: the regular-file check below cannot run until the
        # open returns, and on a FIFO it never would.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeError(
            f"{label}: cannot read 'token_file' {path!r}: {exc.strerror}"
            + (". A symbolic link is not accepted here" if exc.errno == errno.ELOOP else "")
        ) from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # Checked on the descriptor before anything wraps it: a directory
            # opens happily with O_RDONLY and it is `fdopen` that raises, as a
            # bare IsADirectoryError naming neither the label nor the setting.
            raise RuntimeError(f"{label}: 'token_file' {path!r} is not a regular file")
        leaked = stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        if leaked:
            raise RuntimeError(
                f"{label}: 'token_file' {path!r} is mode {stat.S_IMODE(info.st_mode):04o}; it "
                "holds a send credential and must be 0600, readable only by the account that "
                "runs the agent. Fix it with chmod 600 and, since it was readable, have the "
                "token reissued"
            )
        # Handed over before the call, not inside the `with`: if `fdopen`
        # raises, it has already closed the descriptor, and the `finally`
        # below would close it a second time -- an EBADF masking the real
        # failure, or a close of a number since reused by another thread.
        owned, fd = fd, -1
        with os.fdopen(owned, "rb") as handle:
            retval = handle.read().decode("utf-8").strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not retval:
        raise RuntimeError(f"{label}: 'token_file' {path!r} is empty")
    return retval


def _gateway_from_config(cfg: dict, label: str, timeout: int) -> GatewayTransport:
    """Build the gateway transport a destination block asks for."""
    gateway = cfg.get("gateway")
    if not isinstance(gateway, dict):
        raise RuntimeError(
            f"{label}: transport 'gateway' needs a 'gateway' block with 'url', "
            "'conversation' and 'token_file'"
        )
    url = str(gateway.get("url") or "")
    conversation = str(gateway.get("conversation") or "")
    token_file = str(gateway.get("token_file") or "")
    missing = [
        name
        for name, value in (
            ("url", url),
            ("conversation", conversation),
            ("token_file", token_file),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"{label}: gateway block is missing: {', '.join(missing)}")
    if not url.startswith(("http://", "https://")):
        # `requests` raises MissingSchema on the first send otherwise, which
        # is after the run has fetched, scored and paid for a model call. The
        # same fail-early rule this function applies to 'wait_s'.
        raise RuntimeError(
            f"{label}: gateway 'url' is {url!r}, which has no scheme; it needs "
            "http:// or https://"
        )
    repeats = gateway.get("repeats", False)
    if not isinstance(repeats, bool):
        raise RuntimeError(f"{label}: gateway 'repeats' must be true or false")
    message_label = gateway.get("label", "")
    if not isinstance(message_label, str):
        raise RuntimeError(f"{label}: gateway 'label' must be a string")
    try:
        wait_s = int(gateway.get("wait_s", 10))
    except (TypeError, ValueError):
        # The gateway answers 422 `bad_wait` for this, but only after the run
        # has been built and has fetched and scored something. A config value
        # that is not a number is knowable now.
        raise RuntimeError(
            f"{label}: gateway 'wait_s' is {gateway.get('wait_s')!r}, which is not a number"
        ) from None
    if not 1 <= wait_s <= 30:
        # The gateway caps its own wait at 30. A larger number here would be
        # silently clamped there, which is a config file that does not say
        # what happens.
        raise RuntimeError(
            f"{label}: gateway 'wait_s' is {wait_s}; it must be between 1 and 30, "
            "which is the gateway's own cap"
        )
    if timeout < wait_s + _WAIT_MARGIN:
        # The gateway holds the response for up to `wait_s`. A socket timeout
        # at or under that fires first, and a send still in flight then comes
        # back as "the gateway did not answer" -- a refusal -- instead of the
        # pending answer it is. Marking the difference matters: one says do
        # not retry, the other says retry under the same key.
        raise RuntimeError(
            f"{label}: 'timeout' is {timeout}s and gateway 'wait_s' is {wait_s}s. The "
            f"request timeout has to outlast the wait by at least {_WAIT_MARGIN}s, or a "
            f"gateway answering a moment after its own deadline times out the socket "
            f"instead, and a message still in flight is read as a failed send"
        )
    # Which destination this is, as one short digest: see
    # `GatewayTransport._key`. The workspace alone, not the whole block --
    # the block holds tuning that an operator changes in response to a
    # failure, and `wait_s` is the first thing they would raise after a 202.
    # Hashing that in would mean the fix for a pending part changes the key
    # of the parts that already arrived and posts them again, which is the
    # outcome the key exists to prevent. `cli.py` injects `_workspace` into
    # every destination block, and two destinations in one workspace are
    # told apart by the footer in the seed.
    fingerprint = _short_digest(str(cfg.get("_workspace") or ""))
    token = _read_token_file(token_file, label)
    retval = GatewayTransport(
        base_url=url,
        conversation=conversation,
        token=token,
        label=message_label,
        wait_s=wait_s,
        timeout=timeout,
        fingerprint=fingerprint,
        repeats=repeats,
    )
    if _bytes(message_label) > _MAX_LABEL_BYTES:
        # Bounded rather than merely checked for leaving a positive remainder.
        # A limit a little above the marker reservation is not safe: once a
        # message needs more than one part the splitter re-splits at
        # `limit - _MARKER_ROOM`, so a remainder of a few dozen bytes yields
        # parts of one or two characters, each of which passes every per-part
        # check while one message becomes hundreds of them and the run reports
        # success. A label is a prefix on every message, not content, so the
        # honest bound is on the label itself.
        raise RuntimeError(
            f"{label}: gateway 'label' is {_bytes(message_label)} bytes, over the "
            f"{_MAX_LABEL_BYTES} allowed. It is prefixed to every message and comes out of "
            f"the same {SIGNAL_MESSAGE_LIMIT}-byte limit, and a label long enough to crowd "
            f"the message splits one into hundreds of tiny ones that each look fine"
        )
    return retval


def transport_from_config(cfg: dict, label: str, timeout: int = 120) -> Transport:
    """Build the transport a destination block asks for.

    Two kinds. `transport: gateway` sends through voitta-gateway, which owns
    the account; anything else drives the signal-cli binary directly. They are
    exclusive, not layered: only one process can hold a Signal account, so the
    cutover is a config flip rather than a fallback chain, and a fallback would
    be a second process trying to open an account the gateway already has.

    For the subprocess transport both values are named by env var, never
    written in config: the account is a phone number and the group id
    identifies a private conversation, and this repo's examples are public.
    """
    kind = str(cfg.get("transport") or "signal-cli")
    if kind == "gateway":
        retval = _gateway_from_config(cfg, label, timeout)
        return retval
    if kind != "signal-cli":
        raise RuntimeError(
            f"{label}: unknown transport {kind!r}; it is 'signal-cli' or 'gateway'"
        )
    account_env = cfg.get("account_env")
    group_env = cfg.get("group_id_env")
    if not account_env or not group_env:
        raise RuntimeError(
            f"{label} needs 'account_env' and 'group_id_env', each naming an environment "
            "variable: the registered Signal number (E.164, e.g. +15551234567) and the "
            "target group id from `signal-cli listGroups -d`"
        )
    account = os.environ.get(str(account_env), "")
    group_id = os.environ.get(str(group_env), "")
    missing = [
        name for name, value in ((account_env, account), (group_env, group_id)) if not value
    ]
    if missing:
        raise RuntimeError(f"{label}: unset or empty: {', '.join(str(m) for m in missing)}")
    binary = str(cfg.get("binary") or "signal-cli")
    if not shutil.which(binary):
        raise RuntimeError(
            f"{label}: {binary!r} is not on PATH. The Signal destination drives the "
            "signal-cli binary (brew install signal-cli), against an account this "
            "deployment has registered"
        )
    retval = SignalTransport(account=account, group_id=group_id, binary=binary, timeout=timeout)
    return retval


def post_message(
    transport: Transport,
    text: str,
    footer: str = "",
    limit: int | None = None,
    seed: str = "",
) -> list[int | str]:
    """Post Markdown to Signal, splitting it if it is too long.

    Returns the handle of every message sent, in order -- a Signal timestamp
    from the subprocess transport, a gateway `message_id` from the gateway
    one, and a zero for a message that went out under an id the transport did
    not report.

    `seed` is a caller-stable id for the message -- a signal's `id`, not
    anything derived from the rendered text -- and each part is sent under
    `<seed>@<footer digest>#<number>-of-<total>`. A transport that
    deduplicates resubmits uses it to recognise a part it has already taken,
    which the text cannot do here because a retry re-scores and the score's
    reasons are rendered into it. The total is part of it so that a retry
    which splits differently is a different message rather than a partial
    match, and the footer tells two destinations apart; see
    `GatewayTransport._key` for what each of those prevents. The footer rides on
    each part, as it does on Slack, because a standing line that appears on
    only the last part of three is a line a reader can miss -- and it is
    appended after the conversion, so it arrives as the characters it was
    written with.
    """
    limit = transport.limit if limit is None else limit
    check_footer(footer, limit=limit)
    rendered, ranges = to_signal(text)
    tail = f"\n{footer}" if footer else ""
    room = limit - _bytes(tail)
    pieces = _split_bytes(rendered, room)
    if len(pieces) > 1:
        # Numbering costs room, so the split is redone with space for the
        # marker rather than letting it push a part over the limit.
        pieces = _split_bytes(rendered, room - _MARKER_ROOM)
    total = len(pieces)
    timestamps: list[int | str] = []
    for number, (piece, offset) in enumerate(pieces, 1):
        body = piece
        styles = _clip(ranges, offset, _units(piece))
        if total > 1:
            marker = f"\n(part {number} of {total})"
            styles.append((_units(body) + 1, _units(marker) - 1, ITALIC))
            body += marker
        body += tail
        if _bytes(body) > limit:
            # Every reservation above -- the footer, the marker, the join
            # between paragraphs -- is arithmetic that can be wrong. This
            # measures the assembled part instead of predicting it, so a slip
            # is a loud failure naming the size rather than a message Signal
            # rejects or truncates without saying so.
            raise SignalSendError(
                f"signal message part {number} of {total} assembled to {_bytes(body)} "
                f"bytes, over the {limit}-byte limit; refusing to send it"
            )
        try:
            timestamps.append(
                transport.send(
                    body,
                    styles,
                    f"{seed}@{_short_digest(footer)}#{number}-of-{total}" if seed else "",
                )
            )
        except SignalSendError as exc:
            if timestamps:
                # The same class, not a fixed one: a part left pending by the
                # gateway and a part refused outright are different answers,
                # and a caller deciding whether to retry needs the one it
                # got. Flattening both to `SignalCliError` here would tell a
                # retrying caller that a message still in flight had failed,
                # and "failed" is the word that stops it resubmitting.
                resubmit = (
                    "Resubmitting does not repeat the parts that arrived, as long as the "
                    "retry splits into the same number of parts: their idempotency keys "
                    "name that total, so a retry whose text renders shorter or longer is "
                    "a different message and does post them again"
                    if getattr(transport, "idempotent", False)
                    else "Resubmitting repeats the parts that did reach the group, because "
                    "this transport has no idempotency key for the far side to match"
                )
                raise type(exc)(
                    f"part {number} of {total} did not go out after {len(timestamps)} of "
                    f"them reached the group, which now holds an incomplete message. "
                    f"{resubmit}: {exc}"
                ) from exc
            raise
    retval = timestamps
    return retval
