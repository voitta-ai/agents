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
dependency is the `signal-cli` binary, checked for when the transport is built
so a missing one fails at startup rather than on signal seventeen.

## Signal has no markup, it has ranges

Slack takes marked-up text. Signal takes **plain text plus a list of styled
ranges**, passed as `--text-style start:length:STYLE`. So `to_signal()` returns
both: the text with every marker removed, and the ranges that reproduce them.

Two consequences worth knowing:

- **Offsets are UTF-16 code units, not characters**, which signal-cli's own
  help says in capitals. An emoji is two units, so a message with one emoji
  before a bold word would bold the wrong letters if offsets were counted in
  Python characters. `_units` is the only place that counts.
- **A range cannot be reopened across messages.** A long message is split
  first, while it is still Markdown, and each part is converted on its own.

The styles Signal accepts are BOLD, ITALIC, STRIKETHROUGH, MONOSPACE and
SPOILER — verified against signal-cli 0.14.9, which rejects anything else with
`Invalid style: <name>`. There is no link style: Signal linkifies bare URLs
itself, so `[label](url)` renders as `label (url)` and the URL stays visible.
Headings become bold lines and list markers become bullets, the same choices
`slack.py` makes for the same reason.

`__x__` is deliberately not bold, matching `slack.py`: what this repo posts is
full of `__init__`, and `**bold**` is how anyone here writes bold.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from framework.slack import split_for_slack as _split_paragraphs

# Signal's own per-message limit. A longer message is split into several, in
# order, the way `slack.py` splits on its block limit.
SIGNAL_MESSAGE_LIMIT = 2000

BULLET = "•"

# Room kept back for `_(part 12 of 34)_` plus its newline, so numbering a part
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

# Tried in order at each position; the earliest match in the line wins, and a
# tie goes to the pattern listed first. Code spans and links come before the
# emphasis rules so that `*` inside a URL or a code span is left alone.
_INLINE_RULES: tuple[tuple[re.Pattern[str], str | None], ...] = (
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

    The one place this repo counts offsets for Signal. Python's `len` counts
    code points, so an emoji (two units) ahead of a styled span would shift
    every range after it by one and style the wrong characters.
    """
    retval = len(text.encode("utf-16-le")) // 2
    return retval


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


def _numbered(parts: list[str]) -> list[str]:
    total = len(parts)
    retval = [f"{part}\n_(part {i} of {total})_" for i, part in enumerate(parts, 1)]
    return retval


def _unescape(text: str) -> str:
    retval = _ESCAPE_RE.sub(r"\1", text)
    return retval


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
            out.write(_unescape(text[position:]))
            return
        start, match, style = best
        out.write(_unescape(text[position:start]))
        mark = out.mark()
        if style is None:
            out.write(_unescape(_link_text(match) if match.re is _LINK_RE else match.group(1)))
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
        """Keep the account number out of anything we raise or log."""
        retval = text.replace(self.account, "<account>") if self.account else text
        return retval

    def send(self, text: str, ranges: list[tuple[int, int, str]]) -> int:
        """Send one already-rendered message. Returns its Signal timestamp.

        That timestamp *is* the message id in Signal — the counterpart of a
        Slack `ts`, and the only handle an edit could later be made with.

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
            raise SignalCliError(
                f"signal-cli did not answer within {self.timeout}s"
            ) from exc
        if done.returncode != 0:
            tail = self._scrub((done.stderr or done.stdout or "").strip())[-800:]
            raise SignalCliError(f"signal-cli exited {done.returncode}: {tail}")
        try:
            body = json.loads(done.stdout or "{}")
            retval = int(body["timestamp"])
        except (ValueError, KeyError, TypeError) as exc:
            # A zero exit with no timestamp means it was probably delivered and
            # we cannot say which message it was, so this does not fail the
            # send -- the same call this repo makes for a tweet that returns 2xx
            # without an id.
            raise SignalCliError(self._scrub(f"signal-cli: no timestamp in reply: {exc}")) from exc
        return retval


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

    Returns the timestamp of every message sent, in order. The footer rides on
    each part, as it does on Slack, because a standing line that appears on only
    the last part of three is a line a reader can miss.
    """
    room = limit - (len(footer) + 1 if footer else 0)
    parts = _split_paragraphs(text, limit=room)
    if len(parts) > 1:
        # Numbering costs characters, so the split is redone with room for the
        # marker rather than letting it push the last part over the limit.
        parts = _numbered(_split_paragraphs(text, limit=room - _MARKER_ROOM))
    timestamps = []
    for part in parts:
        rendered, ranges = to_signal(f"{part}\n{footer}" if footer else part)
        if _units(rendered) > limit:
            # The split works on Markdown and the markers are then removed, so
            # a part can only come out shorter than it went in. Arriving here
            # means the arithmetic is wrong, and Signal would reject or truncate
            # rather than say so.
            raise SignalCliError(
                f"signal message part is {_units(rendered)} units, over the {limit} limit"
            )
        timestamps.append(transport.send(rendered, ranges))
    retval = timestamps
    return retval
