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
    <url> (autolink)       ->  <url>
    bare url               ->  left alone (Slack links it)
    ***bold italic***      ->  *_bold italic_*
    **bold**               ->  *bold*
    __bold__, ___x___      ->  left alone, on purpose (see below)
    *italic*, _italic_     ->  _italic_
    \\*escaped\\*            ->  *escaped*, the backslash consumed, no emphasis
    `code`, ``` blocks     ->  unchanged, and their content is never converted
    - item, * item, 1. it  ->  bullet character, indent preserved
    > quote                ->  unchanged, and the marker is never escaped
    tables                 ->  left as text
    & < >                  ->  &amp; &lt; &gt; in literal text

**`__bold__` is deliberately not converted**, though Markdown says it is. What
these agents actually emit is release notes and scorer reasons about code, where
`__init__`-style names are ordinary and nobody writes bold as `__bold__`. Turning
a dunder name into `*init*` in a message about that name is the more likely
mistake, so the underscore bold rule is gone on purpose: do not restore it as a
bug fix. `**bold**` still converts, and `_italic_` is unaffected.

**Emphasis only fires on word boundaries**, both for `*` and for `_`, which is
what keeps `https://example.com/a__b__c` and `snake_case_name` intact. A URL
that lost a pair of underscores to a bold rule is a different URL, and an
approved `slack-post` step templating `{url}` would then post a link the
operator never approved. Emphasis is resolved through placeholders in one pass
rather than by sequential substitution, because `**b**` -> `*b*` followed by an
italic rule would immediately turn that `*b*` back into `_b_`.

**`__x__` is deliberately not bold, though Markdown says it is.** Do not "fix"
this back. What goes through here is release notes and scorer reasons about
code, where `__init__`, `__main__` and `__all__` are ordinary words and a
double underscore almost never means emphasis; nobody writing for this channel
reaches for `__x__` when `**x**` is right there. So `**bold**` converts,
`__bold__` and `___bold___` pass through as written, and a name that arrives
without backticks survives instead of turning into `*init*`.

**Escaping** is Slack's documented rule for `text`: `&`, `<` and `>` become
entities so that content which happens to look like `<something>` is not read
as a channel, user or link reference. It is applied to literal text only —
never to a `<url|label>` this module built, never to a blockquote's `>` marker,
and never inside a code span or a fenced block, whose contents this module
leaves exactly as written. A `<url|label>` that arrives *already formatted*
is held aside the same way, because upstream content is full of them: a
scorer's reasons, a digest's prose and the slack-workspace source's own message
text all carry links a human already wrote in Slack's syntax, and escaping one
posts its markup instead of the link. The recogniser insists on a real scheme
in the URL half, so prose like `if a<b|c>d` is escaped as the text it is. A link's URL keeps its `&` escaped as well, and a
`|`, `<` or `>` inside a URL is percent-encoded, since mrkdwn's link syntax
splits on the first `|` and ends at the first `>`: without that, a URL holding
one silently produces a link pointing somewhere else.

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
broken code. Cutting inside a line cuts between **grapheme clusters**, never
between the code points of one: a ZWJ emoji split down the middle leaves a
bare figure at the end of one message and a stray joiner at the start of the
next, and a part that rejoins correctly but renders as garbage is still wrong.

**Parts are numbered** (`_(part 2 of 3)_` on its own line) whenever there is
more than one. The parts of one message are posted in order, but any other
agent sharing the webhook can post between them, and without the marker a
reader cannot tell a continuation from an unrelated message that landed in the
middle.

**Two transports** (issue #86). A destination names either `webhook_env`, an
incoming webhook, or `bot_token_env` plus `channel_id`, which posts through
`chat.postMessage`. Exactly one, and a block naming both or neither fails at
build time. Conversion, splitting, the footer, the part markers and blocks are
identical on both; the difference is what Slack does with them:

  - **Identity actually works only on the API path.** `username` and
    `icon_emoji` (issue #77) are ignored by a modern incoming webhook — the
    post returns `ok` and the stored message has `username: None` — while
    `chat.postMessage` honours them, given the bot's `chat:write.customize`
    scope. The keys ride on the payload either way, since a webhook ignoring
    them is not an error, but a deployment that wants two digests to look
    different has to use a bot token.
  - **Only the API path returns a message `ts`.** A webhook answers `ok` and
    nothing else, which is why a proposal announced through one can never have
    its buttons removed once it is resolved. `post_message` returns the `ts` of
    every message it posted (empty on the webhook path), so a caller that keeps
    one can edit that message later through `update_message`.

The `footer` (issue #64) is appended after conversion, verbatim, to *every*
message a split produces: it exists for a standing line that has to be on every
message, and a compliance line that lands only on the last of three parts is
not that.
"""
from __future__ import annotations

import os
import re
import unicodedata

import requests

SLACK_API = "https://slack.com/api"
_WRITE_METHODS = ("chat.postMessage", "chat.update", "chat.postEphemeral")

# A Slack section block holds 3000 characters; a message that would exceed one
# is posted as several messages (see the module docstring).
SLACK_BLOCK_LIMIT = 3000

# Room kept back for `_(part 12 of 34)_` plus its newline, so numbering a part
# can never push it over the limit.
_MARKER_ROOM = 24

BULLET = "•"
_ZWJ = "‍"

# Two marker characters, so the emphasis pass cannot restore a fragment the
# outer pass is still holding.
_HOLD_MARKS = ("\x00", "\x01")

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^(\s*>+\s?)(.*)$")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")
_BACKSLASH_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>|~])")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^<>\s]+)>")
# An already-formed mrkdwn link, `<url|label>`, as upstream content carries it.
# The URL half must be a real scheme, so prose like `if a<b|c>d` is not mistaken
# for one and quietly turned into a link.
_MRKDWN_LINK_RE = re.compile(r"<((?:https?://|mailto:|tel:)[^<>|\s]*)\|([^<>|]*)>")
# A label may hold one level of nested brackets; a URL may hold '>' and is
# taken whole, either bare or wrapped in angle brackets.
_LINK_RE = re.compile(
    r"(!?)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(\s*(?:<([^<>\s]*)>|([^()\s]+))(?:\s+\"[^\"]*\")?\s*\)"
)
_BOLD_ITALIC_RE = re.compile(r"(?<![\w*])\*\*\*(\S(?:.*?\S)?)\*\*\*(?![\w*])", re.S)
_BOLD_STAR_RE = re.compile(r"(?<![\w*])\*\*(\S(?:.*?\S)?)\*\*(?![\w*])", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(\S(?:[^*\n]*\S)?)\*(?![\w*])")
# One underscore each side, never two: see "__x__ is left alone" in the module
# docstring. The inner guards keep `__x__` and `___x___` out entirely rather
# than letting the italic rule take a bite out of the ends.
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!_)(\S(?:[^_\n]*\S)?)_(?!_)(?![\w])")


_ENTITY_RE = re.compile(r"&(?!amp;|lt;|gt;|#\d+;|#x[0-9a-fA-F]+;)")


def _escape(text: str) -> str:
    """Slack's three entities.

    An `&` that already opens an entity is left alone: text written `&amp;`
    means that entity, and escaping it again would put `&amp;amp;` in the
    channel. Ampersands go first so the two angle brackets are not doubled.
    """
    retval = _ENTITY_RE.sub("&amp;", text)
    retval = retval.replace("<", "&lt;").replace(">", "&gt;")
    return retval


def _link_url(url: str) -> str:
    """A URL safe inside `<url|label>`.

    mrkdwn takes everything up to the first `|` as the URL and ends the link at
    the first `>`, so both have to leave the URL as percent-encoding, and `&`
    is escaped like any other ampersand Slack is given.
    """
    retval = url.replace("<", "%3C").replace(">", "%3E").replace("|", "%7C")
    retval = _ENTITY_RE.sub("&amp;", retval)
    return retval


class _Holder:
    """Fragments already in their final form, kept out of later rules."""

    def __init__(self, mark: str = _HOLD_MARKS[0]) -> None:
        self.items: list[str] = []
        self._mark = mark
        self._pattern = re.compile(f"{re.escape(mark)}(\\d+){re.escape(mark)}")

    def hold(self, text: str) -> str:
        self.items.append(text)
        retval = f"{self._mark}{len(self.items) - 1}{self._mark}"
        return retval

    def restore(self, text: str) -> str:
        # A held fragment can itself hold one (a link label with a code span in
        # it), so this runs until nothing is left to expand.
        while self._pattern.search(text):
            text = self._pattern.sub(lambda m: self.items[int(m.group(1))], text)
        retval = text
        return retval


def _emphasis(text: str) -> str:
    """Bold and italic, resolved in one pass so neither rule sees the other's output."""
    holder = _Holder(_HOLD_MARKS[1])

    def bold_italic(match: re.Match) -> str:
        retval = holder.hold(f"*_{match.group(1)}_*")
        return retval

    def bold(match: re.Match) -> str:
        retval = holder.hold(f"*{match.group(1)}*")
        return retval

    def italic(match: re.Match) -> str:
        retval = holder.hold(f"_{match.group(1)}_")
        return retval

    for pattern, repl in (
        (_BOLD_ITALIC_RE, bold_italic),
        (_BOLD_STAR_RE, bold),
        (_ITALIC_STAR_RE, italic),
        (_ITALIC_UNDER_RE, italic),
    ):
        text = pattern.sub(repl, text)
    retval = holder.restore(text)
    return retval


def _inline(text: str, holder: _Holder | None = None) -> str:
    """Links, emphasis and escaping for one line of prose.

    Code spans, backslash-escaped characters and finished links are held aside
    the moment they are recognised, so no later rule touches their contents and
    nothing this function builds is escaped twice.
    """
    own = holder is None
    holder = holder or _Holder()

    pieces = []
    for piece in _CODE_SPAN_RE.split(text):
        if piece.startswith("`"):
            pieces.append(holder.hold(piece))
            continue
        pieces.append(piece)
    text = "".join(pieces)

    # A `<url|label>` that arrived already formatted is held whole, before any
    # escaping can turn its angle brackets into entities. Upstream content is
    # where these come from: a scorer's reasons, a digest's prose, and the
    # slack-workspace source's own message text all carry them.
    text = _MRKDWN_LINK_RE.sub(
        lambda m: holder.hold(f"<{_link_url(m.group(1))}|{_escape(m.group(2))}>"), text
    )
    text = _BACKSLASH_RE.sub(lambda m: holder.hold(_escape(m.group(1))), text)

    def link(match: re.Match) -> str:
        label = match.group(2)
        url = match.group(3) if match.group(3) is not None else match.group(4)
        rendered = _inline(label, holder) if label else ""
        if rendered:
            retval = holder.hold(f"<{_link_url(url)}|{rendered}>")
        else:
            retval = holder.hold(f"<{_link_url(url)}>")
        return retval

    # Links before autolinks: `[title](<url>)` holds its own angle brackets, and
    # an autolink rule that ran first would have turned that URL into a nested
    # link inside the link.
    text = _LINK_RE.sub(link, text)
    text = _AUTOLINK_RE.sub(lambda m: holder.hold(f"<{_link_url(m.group(1))}>"), text)
    text = _emphasis(text)
    text = _escape(text)
    if own:
        text = holder.restore(text)
    retval = text
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
        quote = _QUOTE_RE.match(line)
        if quote:
            lines.append(f"{quote.group(1)}{_inline(quote.group(2))}")
            continue
        lines.append(_inline(line))
    retval = "\n".join(lines)
    return retval


def _joins(previous: str, char: str) -> bool:
    """Does `char` continue the grapheme cluster ending in `previous`?

    An approximation of UAX #29 covering what actually turns up in a digest:
    combining marks, ZWJ sequences (family and profession emoji), variation
    selectors, skin-tone modifiers, keycaps and regional-indicator flag pairs.
    Python's `re` has no `\\X`, and this is not worth a new dependency.
    """
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Mc"):
        retval = True
        return retval
    if char == _ZWJ or previous.endswith(_ZWJ):
        retval = True
        return retval
    if char in ("︎", "️", "⃣"):
        retval = True
        return retval
    if "\U0001f3fb" <= char <= "\U0001f3ff":
        retval = True
        return retval
    regional = "\U0001f1e6" <= char <= "\U0001f1ff"
    retval = regional and len(previous) == 1 and "\U0001f1e6" <= previous <= "\U0001f1ff"
    return retval


def _graphemes(text: str) -> list[str]:
    retval: list[str] = []
    for char in text:
        if retval and _joins(retval[-1], char):
            retval[-1] += char
            continue
        retval.append(char)
    return retval


def _hard_split(line: str, limit: int) -> list[str]:
    """Cut one over-long line, between grapheme clusters."""
    retval: list[str] = []
    current = ""
    for cluster in _graphemes(line):
        if len(current) + len(cluster) > limit and current:
            retval.append(current)
            current = ""
        current += cluster
    if current:
        retval.append(current)
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

    Paragraph boundaries first, then line boundaries, then inside a line (on a
    grapheme boundary) only when one line is itself longer than the limit.
    Never truncates.
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


def _numbered(parts: list[str]) -> list[str]:
    total = len(parts)
    retval = [f"{part}\n_(part {i} of {total})_" for i, part in enumerate(parts, 1)]
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


class SlackApiError(RuntimeError):
    """Slack answered, but with `ok: false`."""


def _needs_json_body(params: dict) -> bool:
    """Whether these params have to go as a JSON body rather than a query string.

    The rule is the shape of the payload, not a list of method names. A param
    that is a list or a dict (`blocks`, `attachments`) cannot survive a query
    string: `requests` flattens a list of dicts into one repeated key per dict
    *key*, so `blocks=[{"type": "section", "text": {...}}]` goes out as
    `blocks=type&blocks=text` and the message arrives with every block gone.
    """
    retval = any(isinstance(value, (list, dict)) for value in params.values())
    return retval


def slack_api_call(method: str, token: str, timeout: int, **params) -> dict:
    """One Web API call, raising `SlackApiError` on `ok: false`.

    A write, or any call carrying a list or dict, goes as a JSON body; the
    rest as a query string. Nothing here logs: the token is in the header and
    the params are the message itself.
    """
    if method in _WRITE_METHODS or _needs_json_body(params):
        resp = requests.post(
            f"{SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=params,
            timeout=timeout,
        )
    else:
        resp = requests.get(
            f"{SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=timeout,
        )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or not body.get("ok"):
        error = body.get("error") if isinstance(body, dict) else "unreadable response"
        raise SlackApiError(f"{method}: {error}")
    retval = body
    return retval


class SlackTransport:
    """Where a message goes: an incoming webhook, or `chat.postMessage`.

    `post()` takes the finished payload and returns the message `ts`, which is
    `""` on the webhook path because Slack does not send one back. `can_edit`
    says which kind this is, so a caller can decide what to promise a human
    (buttons that will be tidied up, or buttons that cannot be).
    """

    def __init__(
        self,
        webhook_url: str = "",
        bot_token: str = "",
        channel_id: str = "",
        timeout: int = 30,
    ):
        self.webhook_url = webhook_url
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.timeout = timeout

    @property
    def can_edit(self) -> bool:
        retval = bool(self.bot_token)
        return retval

    def post(self, payload: dict) -> str:
        if not self.bot_token:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-type": "application/json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            retval = ""
            return retval
        body = slack_api_call(
            "chat.postMessage", self.bot_token, self.timeout, channel=self.channel_id, **payload
        )
        retval = str(body.get("ts") or "")
        return retval

    def update(self, ts: str, payload: dict) -> None:
        """Edit a message this transport posted. API path only."""
        if not self.can_edit:
            raise RuntimeError(
                "slack transport: a message posted through an incoming webhook has no ts "
                "and cannot be edited; configure 'bot_token_env' and 'channel_id' instead"
            )
        slack_api_call(
            "chat.update", self.bot_token, self.timeout, channel=self.channel_id, ts=ts, **payload
        )


def transport_from_config(cfg: dict, label: str, timeout: int = 30) -> SlackTransport:
    """Build the transport a destination or route block asks for.

    Exactly one of `webhook_env` or `bot_token_env` + `channel_id`. Naming both
    is refused rather than resolved by precedence: the two differ in whether
    `username` is honoured and whether a message can be edited later, so a
    config that names both is a question, not a preference.
    """
    webhook_env = cfg.get("webhook_env")
    token_env = cfg.get("bot_token_env")
    if webhook_env and token_env:
        raise ValueError(
            f"{label} names both 'webhook_env' and 'bot_token_env'; set one. A bot token "
            "posts through chat.postMessage, which honours 'username'/'icon_emoji' and "
            "returns a message ts; an incoming webhook does neither"
        )
    if not webhook_env and not token_env:
        raise RuntimeError(
            f"{label} needs either a 'webhook_env' or a 'bot_token_env' with a 'channel_id'"
        )
    if webhook_env:
        webhook_url = os.environ.get(webhook_env)
        if not webhook_url:
            raise RuntimeError(f"{label} needs the webhook URL in ${webhook_env}")
        retval = SlackTransport(webhook_url=webhook_url, timeout=timeout)
        return retval
    bot_token = os.environ.get(token_env)
    if not bot_token:
        raise RuntimeError(f"{label} needs the bot token in ${token_env}")
    channel_id = cfg.get("channel_id")
    if not channel_id or not isinstance(channel_id, str):
        raise RuntimeError(
            f"{label} needs a 'channel_id' next to 'bot_token_env' (a webhook carries its own "
            "channel, chat.postMessage does not)"
        )
    retval = SlackTransport(bot_token=bot_token, channel_id=channel_id, timeout=timeout)
    return retval


def _as_transport(target: SlackTransport | str, timeout: int) -> SlackTransport:
    if isinstance(target, SlackTransport):
        retval = target
        return retval
    retval = SlackTransport(webhook_url=target, timeout=timeout)
    return retval


def post_message(
    target: SlackTransport | str,
    text: str,
    timeout: int = 30,
    footer: str = "",
    identity: SlackIdentity | None = None,
    blocks: list | None = None,
) -> list[str]:
    """Convert `text` to mrkdwn and post it, split into several messages if long.

    The single way out of this process to Slack. `target` is a `SlackTransport`
    or, for a caller that has only ever had one, a webhook URL. `footer` is
    appended verbatim, after conversion, to every message posted, and a message
    that had to be split carries a part marker. Returns the `ts` of each message
    posted, which is empty strings on the webhook path.

    `blocks` posts one message carrying them, with the converted text as the
    notification fallback and no splitting, because blocks carry their own
    length handling.
    """
    transport = _as_transport(target, timeout)
    body = to_mrkdwn(text)
    extra = identity.payload() if identity else {}
    if blocks is not None:
        payload = {"text": f"{body}\n{footer}" if footer else body, "blocks": blocks}
        payload.update(extra)
        retval = [transport.post(payload)]
        return retval
    limit = SLACK_BLOCK_LIMIT - (len(footer) + 1 if footer else 0)
    parts = split_for_slack(body, limit)
    if len(parts) > 1:
        # Numbering costs characters, so the split is redone with room for it
        # rather than letting a marker push the last part over the limit.
        parts = _numbered(split_for_slack(body, limit - _MARKER_ROOM))
    retval = []
    for part in parts:
        payload = {"text": f"{part}\n{footer}" if footer else part}
        payload.update(extra)
        retval.append(transport.post(payload))
    return retval


def update_message(
    transport: SlackTransport,
    ts: str,
    text: str,
    blocks: list | None = None,
    identity: SlackIdentity | None = None,
) -> None:
    """Replace a posted message's text (and blocks) in place.

    `blocks=[]` removes them, which is how a resolved proposal loses its
    buttons. `text` is converted like any other message.
    """
    payload: dict = {"text": to_mrkdwn(text)}
    if blocks is not None:
        payload["blocks"] = blocks
    if identity:
        payload.update(identity.payload())
    transport.update(ts, payload)
