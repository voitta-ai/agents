"""Posting to a Slack incoming webhook: Markdown to mrkdwn, splitting, identity.

Every destination here writes Markdown — the models do, the sources do (Reddit
bodies are Markdown), and the code around them did too. Slack does not render
Markdown. It renders *mrkdwn*, which shares almost none of the syntax that
matters: no headings, a different link form, one asterisk for bold rather than
two. A real release-notes digest arrived reading `## skillz` and
`[v1.106.0](<url>)`, literally (issue #73).

So there is exactly one way out of this process to a webhook, `post_message`,
and it converts. Nothing above it decides whether to convert, which is why the
code that builds a header writes `**Title**` (Markdown) and not `*Title*`
(mrkdwn): the text handed to `post_message` is Markdown all the way down, and
what Slack receives is mrkdwn all the way down.

What `to_mrkdwn` does, per issue #73:

    # H1 .. ###### H6      ->  *text* on its own line (Slack has no headings)
    [text](url)            ->  <url|text>          (a <url> is unwrapped first)
    ![alt](url)            ->  <url|alt>
    bare url               ->  left alone (Slack links it)
    **bold**, __bold__     ->  *bold*
    *italic*, _italic_     ->  _italic_
    `code`, ``` blocks     ->  unchanged, and their content is never converted
    - item, * item, 1. it  ->  bullet character, indent preserved
    > quote                ->  unchanged
    tables                 ->  left as text

Emphasis is resolved in one pass through placeholders rather than two
sequential substitutions, because `**bold**` -> `*bold*` followed by an italic
rule would immediately turn that `*bold*` back into `_bold_`.

**Splitting: several messages, not several blocks.** A Slack `text` may hold
40000 characters but a `section` block caps at 3000, and a digest can exceed
either. This posts several messages in order instead of building a `blocks`
payload, for two reasons: the payload stays `{"text": ...}`, so a message that
needed neither conversion nor splitting is byte-identical to what this repo
posted before, and a reader scrolling a channel sees the parts in order with no
block-level truncation to notice. Splits land on a paragraph boundary where one
exists, then a line boundary, and only inside a line when a single line is
itself over the limit. A split that would land inside a fenced code block
closes the fence and reopens it in the next message, so no part renders as
broken code.

**Identity** (issue #77): `username`, `icon_emoji` and `icon_url` ride on the
payload. Several agents post through one webhook, and without these every
digest arrives under the same bot name, so two that land seconds apart read as
one message. Unset means the payload does not carry the key at all and Slack
uses the webhook's own identity, exactly as before.

The `footer` (issue #64) is appended after conversion, verbatim, to *every*
message a split produces: it exists for a standing line that has to be on every
message, and a compliance line that lands only on the last of three parts is
not that.
"""
from __future__ import annotations

import re

import requests

# A Slack section block holds 3000 characters; a message that would exceed one
# is posted as several messages (see the module docstring).
SLACK_BLOCK_LIMIT = 3000

_BOLD_OPEN = "\x00b\x00"
_BOLD_CLOSE = "\x00B\x00"
_ITALIC_OPEN = "\x00i\x00"
_ITALIC_CLOSE = "\x00I\x00"

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BOLD_STAR_RE = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.S)
_BOLD_UNDER_RE = re.compile(r"__(\S(?:.*?\S)?)__", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(\S(?:[^_\n]*\S)?)_(?![\w_])")

BULLET = "•"


def _inline(text: str) -> str:
    """Links and emphasis in one line of prose; code spans are left alone."""
    parts = _CODE_SPAN_RE.split(text)
    converted = []
    for part in parts:
        if part.startswith("`"):
            converted.append(part)
            continue
        part = _IMAGE_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>" if m.group(1) else f"<{m.group(2)}>", part)
        part = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", part)
        part = _BOLD_STAR_RE.sub(rf"{_BOLD_OPEN}\1{_BOLD_CLOSE}", part)
        part = _BOLD_UNDER_RE.sub(rf"{_BOLD_OPEN}\1{_BOLD_CLOSE}", part)
        part = _ITALIC_STAR_RE.sub(rf"{_ITALIC_OPEN}\1{_ITALIC_CLOSE}", part)
        part = _ITALIC_UNDER_RE.sub(rf"{_ITALIC_OPEN}\1{_ITALIC_CLOSE}", part)
        converted.append(part)
    retval = "".join(converted)
    retval = retval.replace(_BOLD_OPEN, "*").replace(_BOLD_CLOSE, "*")
    retval = retval.replace(_ITALIC_OPEN, "_").replace(_ITALIC_CLOSE, "_")
    return retval


def to_mrkdwn(text: str) -> str:
    """Render Markdown as Slack mrkdwn. See the module docstring for the table."""
    lines = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading and heading.group(2):
            lines.append(_inline(f"**{heading.group(2)}**"))
            continue
        item = _LIST_RE.match(line)
        if item:
            lines.append(f"{item.group(1)}{BULLET}  {_inline(item.group(2))}")
            continue
        lines.append(_inline(line))
    retval = "\n".join(lines)
    return retval


def _hard_split(line: str, limit: int) -> list[str]:
    retval = [line[i : i + limit] for i in range(0, len(line), limit)]
    return retval


def _pack(pieces: list[str], joiner: str, limit: int) -> list[str]:
    """Greedily fill messages with `pieces`, joined by `joiner`."""
    retval: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}{joiner}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            retval.append(current)
        if len(piece) <= limit:
            current = piece
            continue
        chunks = _hard_split(piece, limit)
        retval.extend(chunks[:-1])
        current = chunks[-1]
    if current:
        retval.append(current)
    return retval


def _close_fences(parts: list[str]) -> list[str]:
    """Reopen a fenced block that a split left open, so no part renders broken."""
    retval = []
    for i, part in enumerate(parts):
        opened = sum(1 for line in part.split("\n") if _FENCE_RE.match(line)) % 2 == 1
        if opened and i + 1 < len(parts):
            part += "\n```"
            parts[i + 1] = "```\n" + parts[i + 1]
        retval.append(part)
    return retval


def split_for_slack(text: str, limit: int = SLACK_BLOCK_LIMIT) -> list[str]:
    """Split `text` into messages of at most `limit` characters, in order.

    Paragraph boundaries first, then line boundaries, then inside a line only
    when one line is itself longer than the limit. Never truncates.
    """
    if len(text) <= limit:
        retval = [text]
        return retval
    # A part that ends inside a fenced block grows by the closing fence, and
    # the part after it by the reopening one, so a text with fences is packed
    # to a smaller limit and still fits once `_close_fences` has run.
    fenced = any(_FENCE_RE.match(line) for line in text.split("\n"))
    room = limit - 8 if fenced else limit
    parts: list[str] = []
    for block in _pack(text.split("\n\n"), "\n\n", room):
        if len(block) <= room:
            parts.append(block)
            continue
        parts.extend(_pack(block.split("\n"), "\n", room))
    retval = _close_fences(parts)
    return retval


class SlackIdentity:
    """Per-agent `username` / `icon_emoji` / `icon_url` for webhook posts (issue #77)."""

    def __init__(self, username: str = "", icon_emoji: str = "", icon_url: str = ""):
        self.username = username
        self.icon_emoji = icon_emoji
        self.icon_url = icon_url

    def payload(self) -> dict:
        retval = {}
        if self.username:
            retval["username"] = self.username
        if self.icon_emoji:
            retval["icon_emoji"] = self.icon_emoji
        if self.icon_url:
            retval["icon_url"] = self.icon_url
        return retval


def identity_from_config(cfg: dict, label: str, default: SlackIdentity | None = None) -> SlackIdentity:
    """Read the identity keys from a destination or route block.

    A key absent here falls back to `default` (the destination-level identity
    for a route). `icon_emoji` must be written with its colons, since that is
    what Slack accepts and `rocket` instead of `:rocket:` posts no icon at all
    and says nothing about why.
    """
    base = default or SlackIdentity()
    values = {
        "username": cfg.get("username", base.username),
        "icon_emoji": cfg.get("icon_emoji", base.icon_emoji),
        "icon_url": cfg.get("icon_url", base.icon_url),
    }
    for key, value in values.items():
        if not isinstance(value, str):
            raise ValueError(f"{label} '{key}' must be a string, got {value!r}")
    emoji = values["icon_emoji"]
    if emoji and not (emoji.startswith(":") and emoji.endswith(":") and len(emoji) > 2):
        raise ValueError(
            f"{label} 'icon_emoji' must be written with colons, like ':rocket:', got {emoji!r}"
        )
    if values["icon_emoji"] and values["icon_url"]:
        raise ValueError(
            f"{label} sets both 'icon_emoji' and 'icon_url'; Slack honours one, so set one"
        )
    retval = SlackIdentity(**values)
    return retval


def post_message(
    webhook_url: str,
    text: str,
    timeout: int = 30,
    footer: str = "",
    identity: SlackIdentity | None = None,
) -> None:
    """Convert `text` to mrkdwn and post it, split into several messages if long.

    The single way out of this process to an incoming webhook. `footer` is
    appended verbatim, after conversion, to every message posted.
    """
    body = to_mrkdwn(text)
    limit = SLACK_BLOCK_LIMIT - (len(footer) + 1 if footer else 0)
    extra = identity.payload() if identity else {}
    for part in split_for_slack(body, limit):
        payload = {"text": f"{part}\n{footer}" if footer else part}
        payload.update(extra)
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
