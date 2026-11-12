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

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
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


class SignalSendPending(SignalGatewayError):
    """The gateway accepted the message and Signal had not taken it yet.

    Separate from `SignalGatewayError` because it is not a failure: the
    message may still arrive. A run-and-exit caller cannot wait to find out,
    so it must neither mark the signal delivered nor report success -- the
    next run resubmits it under the same idempotency key, which waits on the
    same message instead of sending a second one.
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
        raise SignalCliError(
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

    def send(self, text: str, ranges: list[tuple[int, int, str]]) -> int:
        """Send one rendered message; return its Signal timestamp, or 0."""
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
    ):
        self.base_url = base_url.rstrip("/")
        self.conversation = conversation
        self.label = label
        self.wait_s = wait_s
        self.timeout = timeout
        self._token = token
        # A label is prepended by the gateway to every message it sends, so
        # its bytes are not available to the body. `_bytes` and not `len`,
        # for the reason the whole module says.
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

    def _key(self, text: str) -> str:
        """An idempotency key for this exact message to this conversation.

        Derived from the content, so the retry a `SignalSendPending` asks for
        carries the same key and waits on the message already in flight
        instead of sending a second copy. The digest is of the text and the
        styles together: two parts of one message can hold the same words
        under different emphasis.
        """
        material = json.dumps(
            [self.conversation, self.label, text], ensure_ascii=False, sort_keys=True
        )
        retval = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return retval

    def send(self, text: str, ranges: list[tuple[int, int, str]]) -> int:
        """Send one rendered message, waiting for Signal to take it.

        Returns the Signal timestamp on a 200. Raises on anything else, so a
        caller that marks a signal delivered only when this returns can never
        mark one that did not arrive:

        - 200 means Signal took it, and the timestamp is its id.
        - 502 means the send was refused for good; retrying it is pointless
          until whatever was wrong is fixed.
        - 202 means it was still in flight when the gateway's own deadline
          passed. Not a failure and not a success, so it raises
          `SignalSendPending` and the next run resubmits the same key.
        """
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
            "idempotency_key": self._key(text),
            "wait_s": self.wait_s,
        }
        try:
            response = requests.post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # Not `from exc`: a requests exception renders the request, and
            # the Authorization header is on it.
            raise SignalGatewayError(
                f"signal gateway at {self.base_url} did not answer: "
                f"{self._scrub(str(exc))[-400:]}"
            ) from None
        tail = self._scrub((response.text or "").strip())[-400:]
        if response.status_code == 202:
            raise SignalSendPending(
                f"signal gateway queued the message but Signal had not taken it within "
                f"{self.wait_s}s; it is not delivered and not failed, so it stays unmarked "
                f"and the next run resubmits it: {tail}"
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
        if status != "sent":
            # A 200 is the gateway saying Signal took it. If it says anything
            # other than `sent`, this code does not know what it means, and
            # guessing would either lose a message or duplicate one.
            raise SignalGatewayError(
                f"signal gateway answered 200 with status {status!r}, which this "
                f"version does not know how to read: {tail}"
            )
        try:
            retval = int(body["timestamp"])
            return retval
        except (KeyError, TypeError, ValueError):
            # Delivered, id unknown: the same call the subprocess transport
            # makes when signal-cli accepts a message without naming it.
            # Raising here would discard a delivered message's mark and post
            # it again next run.
            return 0


def _read_token_file(path: str, label: str) -> str:
    """The token in a 0600 file, refusing one anybody else can read.

    The mode is checked rather than assumed: a token readable by another
    account is a token to treat as disclosed, and the moment to notice is
    before the run starts rather than after it has posted.
    """
    try:
        info = os.stat(path)
    except OSError as exc:
        raise RuntimeError(f"{label}: cannot read 'token_file' {path!r}: {exc.strerror}") from None
    leaked = stat.S_IMODE(info.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
    if leaked:
        raise RuntimeError(
            f"{label}: 'token_file' {path!r} is mode {stat.S_IMODE(info.st_mode):04o}; it holds "
            "a send credential and must be 0600, readable only by the account that runs the "
            "agent. Fix it with chmod 600 and, since it was readable, have the token reissued"
        )
    with open(path, encoding="utf-8") as handle:
        retval = handle.read().strip()
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
    message_label = gateway.get("label", "")
    if not isinstance(message_label, str):
        raise RuntimeError(f"{label}: gateway 'label' must be a string")
    wait_s = int(gateway.get("wait_s", 10))
    if not 1 <= wait_s <= 30:
        # The gateway caps its own wait at 30. A larger number here would be
        # silently clamped there, which is a config file that does not say
        # what happens.
        raise RuntimeError(
            f"{label}: gateway 'wait_s' is {wait_s}; it must be between 1 and 30, "
            "which is the gateway's own cap"
        )
    token = _read_token_file(token_file, label)
    retval = GatewayTransport(
        base_url=url,
        conversation=conversation,
        token=token,
        label=message_label,
        wait_s=wait_s,
        timeout=timeout,
    )
    if retval.limit < 1:
        raise RuntimeError(
            f"{label}: gateway 'label' {message_label!r} is as long as the whole message limit"
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
) -> list[int]:
    """Post Markdown to Signal, splitting it if it is too long.

    Returns the timestamp of every message sent, in order; a zero is a message
    that went out under an id signal-cli did not report. The footer rides on
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
    timestamps: list[int] = []
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
            raise SignalCliError(
                f"signal message part {number} of {total} assembled to {_bytes(body)} "
                f"bytes, over the {limit}-byte limit; refusing to send it"
            )
        try:
            timestamps.append(transport.send(body, styles))
        except SignalSendError as exc:
            if timestamps:
                # The same class, not a fixed one: a part left pending by the
                # gateway and a part refused outright are different answers,
                # and a caller deciding whether to retry needs the one it
                # got. Flattening both to `SignalCliError` here would tell a
                # retrying caller that a message still in flight had failed,
                # and "failed" is the word that stops it resubmitting.
                raise type(exc)(
                    f"part {number} of {total} did not go out after {len(timestamps)} of "
                    f"them reached the group, which now holds an incomplete message. "
                    f"Resubmitting is safe -- each part carries its own idempotency key, "
                    f"so the parts already sent are not sent twice: {exc}"
                ) from exc
            raise
    retval = timestamps
    return retval
