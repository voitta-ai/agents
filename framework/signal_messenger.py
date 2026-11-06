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

There is no Python dependency to install, so there is no optional extra: the
dependency is the `signal-cli` binary, whose presence on PATH is checked when
the transport is built. That is all that is checked there. An account that is
not registered, or a group id this account is not a member of, costs a
subprocess and the account lock to verify, so it is left to fail on the first
send rather than on every run that sends nothing.

## Signal has no markup, it has ranges

Slack takes marked-up text. Signal takes **plain text plus a list of styled
ranges**, passed as `--text-style start:length:STYLE`. So `to_signal()` returns
both: the text with every marker removed, and the ranges that reproduce them.

Two consequences shaped the code:

- **Offsets are UTF-16 code units, not characters**, which signal-cli's own
  help says in capitals. An emoji is two units, so a message with one emoji
  before a bold word would bold the wrong letters if offsets were counted in
  Python characters. `_units` is the only place that counts, including the
  split: measuring the split in code points and the limit in units is a
  disagreement that grows to 2x on emoji, and it refuses messages that fit.
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

import json
import os
import re
import shutil
import subprocess

# Signal's own per-message limit, in UTF-16 code units. A longer message is
# split into several, in order, the way `slack.py` splits on its block limit.
SIGNAL_MESSAGE_LIMIT = 2000

BULLET = "•"

# Room kept back for `(part 12 of 34)` plus its newline, so numbering a part
# can never push it over the limit. The same reservation `slack.py` makes.
_MARKER_ROOM = 24

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


class SignalCliError(RuntimeError):
    """signal-cli refused or failed. The message carries its stderr, scrubbed."""


def _units(text: str) -> int:
    """Length in UTF-16 code units, which is what Signal counts.

    The one place this repo counts for Signal, for both the style offsets and
    the message limit. Python's `len` counts code points, so an emoji (two
    units) ahead of a styled span would shift every range after it by one and
    style the wrong characters.
    """
    retval = len(text.encode("utf-16-le")) // 2
    return retval


# The longest footer that still leaves room for the message it rides on. A
# footer rides on every part, costing its length plus the newline before it,
# and a split message also carries a part marker. See `check_footer`.
MAX_FOOTER_UNITS = SIGNAL_MESSAGE_LIMIT - _MARKER_ROOM - 2


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
    if _units(footer) > room:
        raise ValueError(
            f"{label} is {_units(footer)} UTF-16 units, which leaves no room for content "
            f"under {'Signal' if limit == SIGNAL_MESSAGE_LIMIT else 'this caller'}'s "
            f"{limit}-unit limit"
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
    messages of invalid text.
    """
    retval: list[tuple[str, int]] = []
    current = ""
    start = offset
    for char in text:
        if current and _units(current) + _units(char) > limit:
            retval.append((current, start))
            start += _units(current)
            current = ""
        current += char
    if current:
        retval.append((current, start))
    return retval


def _pack(pieces: list[tuple[str, int]], sep: str, limit: int, finer) -> list[tuple[str, int]]:
    """Greedily join `pieces` into parts of at most `limit` units.

    A piece too long on its own is handed to `finer`, which splits it on the
    next boundary down.
    """
    parts: list[tuple[str, int]] = []
    current: tuple[str, int] | None = None
    for piece, offset in pieces:
        if current is not None:
            candidate = f"{current[0]}{sep}{piece}"
            if _units(candidate) <= limit:
                current = (candidate, current[1])
                continue
            parts.append(current)
            current = None
        if _units(piece) <= limit:
            current = (piece, offset)
            continue
        parts.extend(finer(piece, offset, limit))
    if current is not None:
        parts.append(current)
    retval = parts
    return retval


def _split_units(text: str, limit: int) -> list[tuple[str, int]]:
    """`text` in parts of at most `limit` UTF-16 units, each with its offset.

    Paragraph boundaries first, then line boundaries, then inside a line only
    when one line is itself longer than the limit. Never truncates.
    """
    if limit < 1:
        raise SignalCliError(
            f"signal message split asked for parts of {limit} units; "
            "a footer or a marker has eaten the whole message"
        )
    if _units(text) <= limit:
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


def _as_text(value: object) -> str:
    """Whatever a subprocess attribute holds, as a string."""
    if value is None:
        retval = ""
    elif isinstance(value, bytes):
        retval = value.decode("utf-8", "replace")
    else:
        retval = str(value)
    return retval


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

    def send(self, text: str, ranges: list[tuple[int, int, str]]) -> int:
        """Send one already-rendered message. Returns its Signal timestamp.

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


def transport_from_config(cfg: dict, label: str, timeout: int = 120) -> SignalTransport:
    """Build the transport a destination block asks for.

    Both values are named by env var, never written in config: the account is a
    phone number and the group id identifies a private conversation, and this
    repo's examples are public.
    """
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
    transport: SignalTransport,
    text: str,
    footer: str = "",
    limit: int = SIGNAL_MESSAGE_LIMIT,
) -> list[int]:
    """Post Markdown to Signal, splitting it if it is too long.

    Returns the timestamp of every message sent, in order; a zero is a message
    that went out under an id signal-cli did not report. The footer rides on
    each part, as it does on Slack, because a standing line that appears on
    only the last part of three is a line a reader can miss -- and it is
    appended after the conversion, so it arrives as the characters it was
    written with.
    """
    check_footer(footer, limit=limit)
    rendered, ranges = to_signal(text)
    tail = f"\n{footer}" if footer else ""
    room = limit - _units(tail)
    pieces = _split_units(rendered, room)
    if len(pieces) > 1:
        # Numbering costs units, so the split is redone with room for the
        # marker rather than letting it push a part over the limit.
        pieces = _split_units(rendered, room - _MARKER_ROOM)
    total = len(pieces)
    timestamps: list[int] = []
    for number, (piece, offset) in enumerate(pieces, 1):
        body = piece
        styles = _clip(ranges, offset, _units(piece))
        if total > 1:
            marker = f"\n(part {number} of {total})"
            styles.append((_units(body) + 1, _units(marker) - 1, ITALIC))
            body += marker
        body += tail
        if _units(body) > limit:
            # Every reservation above -- the footer, the marker, the join
            # between paragraphs -- is arithmetic that can be wrong. This
            # measures the assembled part instead of predicting it, so a slip
            # is a loud failure naming the size rather than a message Signal
            # rejects or truncates without saying so.
            raise SignalCliError(
                f"signal message part {number} of {total} assembled to {_units(body)} "
                f"units, over the {limit}-unit limit; refusing to send it"
            )
        try:
            timestamps.append(transport.send(body, styles))
        except SignalCliError as exc:
            if timestamps:
                raise SignalCliError(
                    f"part {number} of {total} failed after {len(timestamps)} of them "
                    f"reached the group, which now holds an incomplete message: {exc}"
                ) from exc
            raise
    retval = timestamps
    return retval
