"""Slack-workspace source — read community workspaces we don't administer (issue #19).

Every Slack bot token this repo holds is scoped to a workspace we administer, so
there is no bot path into a community workspace, and getting an app installed
there needs admin approval we are unlikely to get. See issue #19 for the full
feasibility writeup.

## Two transports

`transport:` selects the implementation; the config shape and the Source
interface are the same for both:

  - `agentmail` (issue #19) — the workspace's own notification/digest
    emails are forwarded to an AgentMail inbox; this plugin is a thin
    composition wrapper over the existing `agentmail` source
    (`framework/plugins/agentmail_source.py`). Zero new auth, unambiguously
    within ToS, identical in shape to an already-deployed agent.
    Coarse granularity: you get what Slack chooses to email, not full channel
    history, and per-message links rather than threads.

  - `session` (issue #45) — reads as the logged-in browser user through the
    `xoxc` web token plus the `d` session cookie, the mechanism the
    voitta-ai/skillz `slack-xoxc-session-client` skill documents. Full
    channel history, a real lookback window, folded threads. Read-only
    access as the logged-in user, per configured workspace, is the
    authorization decision #45 records; posting stays unauthorized and the
    client refuses it in code (see below).

## Session transport: how the credentials are obtained

Nothing is configured and nothing is stored. Per run the plugin:

  1. decrypts the `d` (and `d-s`) cookie for `<subdomain>.slack.com` from the
     local browser's cookie store with `pycookiecheat`. `d` is httpOnly, so
     no in-page or extension path can read it; decrypting the store is the
     only scriptable way. On macOS this touches the login keychain and may
     prompt.
  2. GETs the workspace root with that cookie and scrapes the `xoxc-` token
     out of the bootstrap payload (`"api_token":"xoxc-..."`).
  3. POSTs to `https://<subdomain>.slack.com/api/<method>` with the token as
     form data and the cookies attached. The team host matters: the generic
     `slack.com` host rejects session tokens.

Both secrets live in the client instance and die with the process. They are
never written, never logged and never appear in an error message. A call that
comes back `invalid_auth` re-extracts once (the session may have rotated
under us) and, if that still fails, raises naming the subdomain — the fix is
to log in again in the browser.

`pycookiecheat` is an optional dependency (`pip install '.[slack-session]'`)
and is imported inside `_build_session_transport`, not at module scope. The
#40 import isolation is per module: a module-level import would take the
agentmail transport down on a venv that lacks the package, which is exactly
the incident class #40 exists to prevent.

## Session transport: writes are refused in code

`_SessionClient.call` checks the method against `_READ_METHODS` before it
touches the network. Anything not in that allowlist raises `RuntimeError`
naming the method. There is no config key, flag or subclass hook that widens
the list; extending it means editing this file. That is deliberate: the
allowlist is the whole guarantee the read-only authorization rests on, and
a guarantee that can be switched off in a config file is not one.

## Session transport: what a run does

  - `channels` entries are names or conversation ids. A name is resolved to
    an id once per run through `conversations.list` (public and private,
    archived excluded, paginated); an unknown name raises and lists what the
    workspace does have, so a typo is a startup error and not an empty digest.
    An entry shaped `^[CDG][A-Z0-9]{8,}$` is a conversation id and is used
    directly. That is the only way to read a direct message: a DM has no name
    and `conversations.list` does not carry it. An id gets one
    `conversations.info` call for a `meta.channel` label — the channel's name,
    or for a DM the other member's display name through `users.info`. A label
    that cannot be read falls back to the id rather than failing the run.
  - `from_users` (optional) keeps only messages posted by those ids. A
    message's author is its `user`, or the `bot_profile.user_id` behind a bot
    message; a `B...` id matches a message's `bot_id`, for integrations that
    post with no user at all. This is what makes a bot's DM usable: the
    conversation also holds your own replies, and without the filter they
    would be scored as signals too. It applies to thread replies as well, so a
    kept message folds in only the replies from a wanted author — otherwise
    the text of a filtered signal would carry the very writing the filter
    excludes. A parent whose replies are all filtered out folds nothing.
  - `conversations.history` is read per channel with `oldest = now -
    since_hours`, paginated by cursor. Only top-level messages become
    Signals; thread replies that Slack surfaces in history (broadcasts) are
    skipped there and picked up through their parent.
  - With `include_threads`, every message carrying `reply_count` gets its
    replies fetched through `conversations.replies` and folded into the
    parent's text as indented `name: text` lines, so a scorer or a digest
    sees the whole exchange as one item.
  - One Signal per top-level message. `id` is `<channel_id>:<ts>`, which is
    stable across runs and is what the SeenStore dedupes on. `url` is the
    permalink from `chat.getPermalink`, or a constructed archive link where
    that method is refused (see below). `meta` carries `workspace`,
    `channel` (the name), `user` (display name), `reply_count`,
    `permalink_source` (`"api"` or `"constructed"`) and
    `transport: "session"`.
  - User ids are rendered to display names through `users.info`, cached for
    the run.
  - HTTP 429 (and the JSON-level `ratelimited` error) honour `Retry-After`
    and retry that call. A run that still cannot complete raises; it never
    returns the messages it managed to get as though that were everything,
    because the SeenStore would then mark the window done.
  - A page with `has_more: true` and no `next_cursor` raises. That is the
    shape Slack returns when a free-tier workspace's history visibility cap
    cuts the window short, and it is the common case for the community
    workspaces this transport targets. Following it with time-based
    pagination would not help (the older messages are not visible to this
    user), and returning what came back would present a capped window as a
    complete one.

## Session transport on an Enterprise Grid org

Which methods a user session may call is a property of the *workspace*, not of
this config. An Enterprise Grid org refuses several of them outright with
`enterprise_is_restricted`, and a lead agent hit both of these live (issue
#69). Everything needed to actually read messages still works there:
`auth.test`, `conversations.info` (including a DM, by id),
`conversations.history`, `conversations.replies`, `users.info` and `team.info`.
Two things do not:

  - **`chat.getPermalink` is refused.** A link is a convenience and the message
    is the signal, so a refusal is not worth a failed run: the archive URL is
    built instead, `https://<subdomain>.slack.com/archives/<channel_id>/p<ts
    without its dot>`, which is the link the Slack client itself produces.
    `meta["permalink_source"]` records `"constructed"` rather than `"api"`, and
    the first fallback in a run prints one line on stderr, so nobody has to
    guess whether a link was confirmed by Slack or assembled here.
  - **`conversations.list` is refused, so channel *names* cannot be resolved
    at all.** A config naming channels fails on such a workspace, and no
    amount of retrying changes that; the run raises before anything else with
    a message saying the org restricts listing and that channels must be given
    as ids. A config of ids only never calls `conversations.list`, so it works
    on a Grid org unchanged — which makes ids the portable way to write one.

Config keys (session transport):

  workspace       label for Signal.source / meta, required
  subdomain       the `<subdomain>` in `<subdomain>.slack.com`, required
  browser         pycookiecheat browser name (chrome, chromium, brave,
                  firefox, slack); default chrome
  channels        list of channel names or conversation ids (`C...`, `D...`,
                  `G...`), required and non-empty
  from_users      optional list of user ids; keeps only their messages
  since_hours     lookback window; default 24
  include_threads fold thread replies into the parent; default true
  timeout         per-request HTTP timeout in seconds; default 30

The agentmail transport takes the agentmail source's own keys, including
`include_trash` (default false): that inbox's list endpoint hides messages
labelled `trash`, and a workspace's forwarded digests can end up there without
this repo doing anything. See agentmail_source.py.

## Channel scoping is best-effort on the agentmail transport

AgentMail's message list carries no structured channel field (see
agentmail_source.py's docstring on the shape it hands back) — Slack's digest
emails are just subject/sender/body. So `channels:` there is a coarse text
filter over the combined subject+body, matching the "coarse granularity"
tradeoff issue #19 accepted. The session transport scopes by channel for
real.

Contract: see docs/PLUGIN-CONTRACT.md.
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Callable

import requests

from framework.interfaces import Signal, Source
from framework.plugins.agentmail_source import AgentMailSource
from framework.registry import register_source


class _AgentmailTransport:
    """Adapts an AgentMailSource into a workspace-scoped Source.

    Composition, not subclassing: AgentMailSource already does the request,
    the filtering, and the body hydration (and already never swallows a
    failed fetch — `requests.raise_for_status()` propagates). This wrapper
    only adds the workspace label and the coarse channel filter.
    """

    def __init__(self, agentmail: AgentMailSource, workspace: str, channels: list[str]):
        self._agentmail = agentmail
        self._workspace = workspace
        self._channel_terms = [c.lstrip("#").lower() for c in channels]

    def fetch(self) -> list[Signal]:
        signals = self._agentmail.fetch()
        if self._channel_terms:
            signals = [
                s
                for s in signals
                if any(term in s.text.lower() for term in self._channel_terms)
            ]
        for s in signals:
            s.meta["workspace"] = self._workspace
            s.meta["transport"] = "agentmail"
        retval = signals
        return retval


# The read-method allowlist for the session transport. This is the guarantee
# the read-only authorization (issue #45) rests on: `_SessionClient.call`
# refuses any method not listed here before any HTTP happens, and nothing in
# config can extend it. Widening it is a code change with a review.
_READ_METHODS: frozenset[str] = frozenset(
    {
        "auth.test",
        "conversations.list",
        "conversations.info",
        "conversations.history",
        "conversations.replies",
        "chat.getPermalink",
        "users.info",
        "users.list",
    }
)

# A `channels` entry in this shape is a Slack conversation id and is used as
# one: C public/private channel, D direct message, G legacy private group. A
# DM has no name to resolve, which is the case that made this necessary.
_CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")

# What an Enterprise Grid org answers a user session for the methods it does
# not allow it (issue #69). It is a property of the workspace, not of the call.
_RESTRICTED = "enterprise_is_restricted"

_USER_AGENT = "Mozilla/5.0"
_PAGE_LIMIT = 200
_MAX_RATE_LIMIT_RETRIES = 5


class SlackAPIError(RuntimeError):
    """A Slack method answered `ok: false`; `code` is Slack's error string.

    A `RuntimeError` like everything else this module raises, so callers that
    only want a failed run need no change. `code` exists because two of these
    are a property of the *workspace* rather than a bug: an Enterprise Grid org
    refuses some methods to a user session with `enterprise_is_restricted`, and
    the transport reacts differently depending on which method that was.
    """

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class _SessionClient:
    """Slack web-API client authenticated by a live browser session, read-only.

    `cookie_reader` is `pycookiecheat.get_cookies` bound to a browser by the
    builder; the client itself has no import of pycookiecheat so the
    allowlist and the request path can be exercised without it. Secrets are
    held on the instance only and are never logged, written or surfaced in
    exceptions.
    """

    def __init__(
        self,
        subdomain: str,
        cookie_reader: Callable[[str], dict[str, str]],
        timeout: int = 30,
    ):
        self.subdomain = subdomain
        # The team host, not slack.com: session tokens are rejected on the
        # generic host.
        self._base = f"https://{subdomain}.slack.com"
        self._cookie_reader = cookie_reader
        self._timeout = timeout
        self._cookies: dict[str, str] | None = None
        self._token: str | None = None

    def _extract_cookies(self) -> dict[str, str]:
        raw = self._cookie_reader(f"{self._base}/")
        d = raw.get("d", "")
        if not d.startswith("xoxd-"):
            raise RuntimeError(
                f"slack-workspace source (transport: session): no valid 'd' "
                f"session cookie for {self.subdomain}.slack.com in the browser "
                f"cookie store; log in to that workspace in the browser first"
            )
        # Some workspaces require d-s alongside d; send it when present.
        retval = {"d": d, "d-s": raw.get("d-s", "")}
        return retval

    def _extract_token(self) -> str:
        # No raise_for_status here: Slack answers a scripted GET of the
        # workspace root with 403 while still serving the full authenticated
        # HTML that carries the token, so the token's presence in the body is
        # the real success test, not the HTTP status code.
        resp = requests.get(
            f"{self._base}/",
            cookies=self._cookies,
            headers={"User-Agent": _USER_AGENT},
            timeout=self._timeout,
        )
        match = re.search(r'"api_token":"(xoxc-[^"]+)"', resp.text)
        if match is None:
            raise RuntimeError(
                f"slack-workspace source (transport: session): no xoxc token "
                f"in the {self.subdomain}.slack.com page; the browser session "
                f"is probably expired, log in again"
            )
        retval = match.group(1)
        return retval

    def _ensure_auth(self) -> None:
        if self._cookies is None:
            self._cookies = self._extract_cookies()
        if self._token is None:
            self._token = self._extract_token()

    def _reset_auth(self) -> None:
        self._cookies = None
        self._token = None

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One method call with 429 handling. Returns the parsed JSON body."""
        attempts = 0
        while True:
            attempts += 1
            resp = requests.post(
                f"{self._base}/api/{method}",
                data={"token": self._token, **params},
                cookies=self._cookies,
                headers={"User-Agent": _USER_AGENT},
                timeout=self._timeout,
            )
            body: dict[str, Any] = {}
            if resp.status_code != 429:
                resp.raise_for_status()
                body = resp.json()
            limited = resp.status_code == 429 or body.get("error") == "ratelimited"
            if not limited:
                break
            if attempts >= _MAX_RATE_LIMIT_RETRIES:
                raise RuntimeError(
                    f"slack-workspace source (transport: session): {method} on "
                    f"{self.subdomain} still rate limited after {attempts} attempts; "
                    f"aborting the run rather than returning a partial window"
                )
            retry_after = resp.headers.get("Retry-After", "")
            delay = int(retry_after) if retry_after.isdigit() else 1
            time.sleep(max(delay, 1))
        retval = body
        return retval

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Call a read method. Refuses anything outside `_READ_METHODS`.

        The allowlist check runs before authentication and before any HTTP,
        so a refused method never even extracts credentials.
        """
        if method not in _READ_METHODS:
            raise RuntimeError(
                f"slack-workspace source (transport: session): refusing Slack "
                f"method {method!r}; this transport is read-only and writes are "
                f"unauthorized (issue #45). Allowed: {', '.join(sorted(_READ_METHODS))}"
            )
        self._ensure_auth()
        result = self._post(method, params)
        if result.get("error") == "invalid_auth":
            # The browser session may have rotated since extraction; take
            # exactly one fresh look before giving up.
            self._reset_auth()
            self._ensure_auth()
            result = self._post(method, params)
            if result.get("error") == "invalid_auth":
                raise RuntimeError(
                    f"slack-workspace source (transport: session): "
                    f"{self.subdomain}.slack.com rejected the session even after "
                    f"re-extracting it; log in to that workspace again in the browser"
                )
        if not result.get("ok"):
            code = str(result.get("error") or "unknown error")
            raise SlackAPIError(
                f"slack-workspace source (transport: session): {method} on "
                f"{self.subdomain} failed: {code}",
                code=code,
            )
        retval = result
        return retval

    def archive_url(self, channel_id: str, ts: str) -> str:
        """The archive link for a message, built rather than asked for.

        Same shape the Slack client produces: the message timestamp with its
        dot removed, prefixed `p`. Used when `chat.getPermalink` is refused.
        """
        retval = f"{self._base}/archives/{channel_id}/p{str(ts).replace('.', '')}"
        return retval

    def paginate(self, method: str, key: str, **params: Any) -> list[dict[str, Any]]:
        """Follow `response_metadata.next_cursor` and concatenate `key`.

        Raises when a page says `has_more` but gives no cursor to continue
        with: Slack's history-visibility cap, where a partial result must not
        pass for a complete one.
        """
        items: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page_params = dict(params, limit=_PAGE_LIMIT)
            if cursor:
                page_params["cursor"] = cursor
            page = self.call(method, **page_params)
            items.extend(page.get(key) or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                if page.get("has_more"):
                    channel = params.get("channel")
                    where = f" for channel {channel}" if channel else ""
                    raise RuntimeError(
                        f"slack-workspace session: {method}{where} reported has_more "
                        "with no next_cursor after "
                        f"{len(items)} items: history is capped (Slack's free-tier "
                        "history visibility limit), so the rest of the window cannot "
                        "be read; refusing to return a partial result"
                    )
                break
        retval = items
        return retval


class _SessionTransport:
    """Reads named channels as the logged-in user and emits one Signal per message."""

    def __init__(
        self,
        client: _SessionClient,
        workspace: str,
        channels: list[str],
        since_hours: float,
        include_threads: bool,
        from_users: list[str] | None = None,
    ):
        self._client = client
        self._workspace = workspace
        self._channels = [c.strip() for c in channels]
        self._since_hours = since_hours
        self._include_threads = include_threads
        self._from_users = set(from_users or [])
        self._user_names: dict[str, str] = {}
        self._permalink_warned = False

    def _resolve_channels(self) -> list[tuple[str, str]]:
        """Configured entries as (label, id) pairs, in configured order.

        A name is resolved through `conversations.list` and an unknown one is a
        hard error. An id is used as given: a DM has no name to look up, and
        `conversations.list` would not carry it anyway.
        """
        names = [c.lstrip("#") for c in self._channels if not _CHANNEL_ID.match(c)]
        by_name: dict[str, str] = {}
        if names:
            try:
                listed = self._client.paginate(
                    "conversations.list",
                    "channels",
                    types="public_channel,private_channel",
                    exclude_archived="true",
                )
            except SlackAPIError as exc:
                if exc.code != _RESTRICTED:
                    raise
                raise RuntimeError(
                    f"slack-workspace source (transport: session): this workspace "
                    f"restricts channel listing for user sessions "
                    f"({_RESTRICTED}), so the name(s) {', '.join(names)} cannot be "
                    f"resolved. Give those channels as conversation ids instead: "
                    f"open the channel in Slack, 'Copy link', and take the id from "
                    f"the end of the URL (.../archives/<id>). Reading by id needs "
                    f"no listing and works on this workspace; a DM id starts with D."
                ) from exc
            by_name = {c["name"]: c["id"] for c in listed if c.get("name") and c.get("id")}
            missing = [n for n in names if n not in by_name]
            if missing:
                raise RuntimeError(
                    f"slack-workspace source (transport: session): channel(s) "
                    f"{', '.join(missing)} not found in {self._workspace}; the "
                    f"workspace has: {', '.join(sorted(by_name))}"
                )

        resolved: list[tuple[str, str]] = []
        for entry in self._channels:
            if _CHANNEL_ID.match(entry):
                resolved.append((self._label_for_id(entry), entry))
            else:
                name = entry.lstrip("#")
                resolved.append((name, by_name[name]))
        retval = resolved
        return retval

    def _label_for_id(self, channel_id: str) -> str:
        """A human label for a conversation named by id, for `meta.channel`.

        `conversations.info` names a channel. A DM has no name, so the label is
        the other member's display name, which is what an operator reading the
        digest recognises. Anything unreadable falls back to the id itself:
        a label is not worth failing a run over.
        """
        info = self._client.call("conversations.info", channel=channel_id).get("channel") or {}
        name = info.get("name") or info.get("name_normalized") or ""
        if not name and info.get("is_im"):
            other = info.get("user") or ""
            name = f"dm:{self._display_name(other)}" if other else "dm"
        retval = name or channel_id
        return retval

    def _display_name(self, user_id: str) -> str:
        """Display name for a user id, cached for the run."""
        if user_id not in self._user_names:
            info = self._client.call("users.info", user=user_id).get("user") or {}
            profile = info.get("profile") or {}
            self._user_names[user_id] = (
                profile.get("display_name")
                or profile.get("real_name")
                or info.get("real_name")
                or info.get("name")
                or user_id
            )
        retval = self._user_names[user_id]
        return retval

    @staticmethod
    def _message_user_id(message: dict[str, Any]) -> str:
        """The posting user id, including the bot user behind a bot message."""
        retval = message.get("user") or (message.get("bot_profile") or {}).get("user_id") or ""
        return retval

    def _user_name(self, message: dict[str, Any]) -> str:
        user_id = self._message_user_id(message)
        if not user_id:
            # Bot and integration posts carry no user; fall back to what the
            # message itself says.
            retval = message.get("username") or message.get("bot_id") or "unknown"
            return retval
        retval = self._display_name(user_id)
        return retval

    def _is_wanted_author(self, message: dict[str, Any]) -> bool:
        """`from_users` membership. Empty config means every author.

        Applied to thread replies as well as to top-level messages: a reply is
        as much someone's writing as a post, and a filtered conversation that
        folded the filtered-out author's replies into the text would hand the
        scorer exactly what the filter was meant to keep out.
        """
        if not self._from_users:
            retval = True
        else:
            user_id = self._message_user_id(message)
            # A `B...` id may be configured instead, for an integration whose
            # messages carry no user at all.
            retval = (user_id in self._from_users) or (
                message.get("bot_id") in self._from_users
            )
        return retval

    @staticmethod
    def _is_top_level(message: dict[str, Any]) -> bool:
        thread_ts = message.get("thread_ts")
        retval = thread_ts is None or thread_ts == message.get("ts")
        return retval

    def _replies_text(self, channel_id: str, parent_ts: str) -> list[str]:
        replies = self._client.paginate(
            "conversations.replies", "messages", channel=channel_id, ts=parent_ts
        )
        lines = []
        for reply in replies:
            if reply.get("ts") == parent_ts:
                continue
            if not self._is_wanted_author(reply):
                continue
            lines.append(f"  {self._user_name(reply)}: {reply.get('text', '')}")
        retval = lines
        return retval

    def _permalink(self, channel_id: str, ts: str) -> tuple[str, str]:
        """The message's link, and where it came from.

        `chat.getPermalink` is refused outright on an Enterprise Grid org
        (issue #69), and the link is a convenience while the message is the
        signal, so *that* refusal builds the archive URL instead of failing the
        run. `meta["permalink_source"]` says which happened, and the first
        fallback of a run says so once on stderr: constructed links are correct
        but unverified, and an operator should not have to guess which they
        have.

        Only `enterprise_is_restricted` is treated that way. Any other Slack
        error propagates, because a constructed link looks exactly the same
        whether the org restricts the method or the call was wrong, and a run
        that quietly builds links over a real fault would never be noticed.
        """
        link = ""
        try:
            link = self._client.call(
                "chat.getPermalink", channel=channel_id, message_ts=ts
            ).get("permalink", "")
        except SlackAPIError as exc:
            # Only the org-wide restriction is survivable. channel_not_found,
            # message_not_found, missing_scope or a transient fatal_error say
            # something is wrong with this call, and constructing a link would
            # bury that under a URL indistinguishable from a healthy Grid run.
            if exc.code != _RESTRICTED:
                raise
            if not self._permalink_warned:
                self._permalink_warned = True
                print(
                    f"slack-workspace source (transport: session): chat.getPermalink "
                    f"unavailable ({exc.code}); building archive links instead, see "
                    f"meta.permalink_source",
                    file=sys.stderr,
                )
        if link:
            retval = (link, "api")
        else:
            retval = (self._client.archive_url(channel_id, ts), "constructed")
        return retval

    def _signal(self, channel_name: str, channel_id: str, message: dict[str, Any]) -> Signal:
        ts = message["ts"]
        reply_count = int(message.get("reply_count") or 0)
        lines = [message.get("text", "")]
        if self._include_threads and reply_count > 0:
            lines.extend(self._replies_text(channel_id, ts))
        permalink, permalink_source = self._permalink(channel_id, ts)
        retval = Signal(
            id=f"{channel_id}:{ts}",
            text="\n".join(lines),
            url=permalink,
            source=f"slack-workspace:{self._workspace}",
            meta={
                "workspace": self._workspace,
                "channel": channel_name,
                "user": self._user_name(message),
                "reply_count": reply_count,
                "permalink_source": permalink_source,
                "transport": "session",
            },
        )
        return retval

    def fetch(self) -> list[Signal]:
        self._user_names = {}
        self._permalink_warned = False
        oldest = time.time() - self._since_hours * 3600
        signals: list[Signal] = []
        for channel_name, channel_id in self._resolve_channels():
            history = self._client.paginate(
                "conversations.history",
                "messages",
                channel=channel_id,
                oldest=f"{oldest:.6f}",
            )
            for message in history:
                if not self._is_top_level(message):
                    continue
                if not self._is_wanted_author(message):
                    continue
                signals.append(self._signal(channel_name, channel_id, message))
        retval = signals
        return retval


def _build_agentmail_transport(cfg: dict) -> Source:
    workspace = cfg.get("workspace")
    if not workspace:
        raise RuntimeError(
            "slack-workspace source (transport: agentmail) needs 'workspace' "
            "(a label for humans/meta — credentials are resolved per transport, "
            "not from this field)"
        )

    inbox = cfg.get("inbox") or os.environ.get("AGENTMAIL_EMAIL")
    if not inbox:
        raise RuntimeError(
            "slack-workspace source (transport: agentmail) needs 'inbox' in "
            "config or AGENTMAIL_EMAIL in env"
        )

    key_env = cfg.get("api_key_env", "AGENTMAIL_API_KEY")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(
            f"slack-workspace source (transport: agentmail) needs the API key in ${key_env}"
        )

    agentmail = AgentMailSource(
        inbox=inbox,
        api_key=api_key,
        limit=int(cfg.get("limit", 50)),
        from_contains=cfg.get("from_contains"),
        subject_contains=cfg.get("subject_contains"),
        fetch_body=bool(cfg.get("fetch_body", True)),
        include_trash=bool(cfg.get("include_trash", False)),
        label=f"slack-workspace:{workspace}",
        timeout=int(cfg.get("timeout", 30)),
    )
    retval = _AgentmailTransport(
        agentmail=agentmail,
        workspace=workspace,
        channels=cfg.get("channels") or [],
    )
    return retval


def _build_session_transport(cfg: dict) -> Source:
    workspace = cfg.get("workspace")
    if not workspace:
        raise RuntimeError(
            "slack-workspace source (transport: session) needs 'workspace' "
            "(a label for Signal.source/meta, not a credential)"
        )
    subdomain = cfg.get("subdomain")
    if not subdomain:
        raise RuntimeError(
            "slack-workspace source (transport: session) needs 'subdomain' "
            "(the <subdomain> in <subdomain>.slack.com)"
        )
    channels = cfg.get("channels") or []
    if not channels:
        raise RuntimeError(
            "slack-workspace source (transport: session) needs a non-empty "
            "'channels' list of channel names or conversation ids"
        )
    from_users = cfg.get("from_users") or []
    if not isinstance(from_users, list):
        raise RuntimeError(
            "slack-workspace source (transport: session): 'from_users' must be a "
            "list of user ids"
        )

    # Local import on purpose: pycookiecheat is optional, and the #40 import
    # isolation is per module. Importing it at module scope would take the
    # agentmail transport down on a venv without it.
    try:
        import pycookiecheat
    except ImportError as exc:
        raise RuntimeError(
            "slack-workspace source (transport: session) needs the optional "
            "dependency pycookiecheat: pip install '.[slack-session]'"
        ) from exc

    browser_name = str(cfg.get("browser", "chrome"))
    try:
        browser = pycookiecheat.BrowserType(browser_name)
    except ValueError as exc:
        known = ", ".join(b.value for b in pycookiecheat.BrowserType)
        raise RuntimeError(
            f"slack-workspace source (transport: session): unknown browser "
            f"{browser_name!r}; pycookiecheat knows: {known}"
        ) from exc

    def _read_cookies(url: str) -> dict[str, str]:
        retval = pycookiecheat.get_cookies(url, browser=browser)
        return retval

    client = _SessionClient(
        subdomain=subdomain,
        cookie_reader=_read_cookies,
        timeout=int(cfg.get("timeout", 30)),
    )
    retval = _SessionTransport(
        client=client,
        workspace=workspace,
        channels=list(channels),
        since_hours=float(cfg.get("since_hours", 24)),
        include_threads=bool(cfg.get("include_threads", True)),
        from_users=[str(u) for u in from_users],
    )
    return retval

_TRANSPORTS: dict[str, Callable[[dict], Source]] = {
    "agentmail": _build_agentmail_transport,
    "session": _build_session_transport,
}


@register_source("slack-workspace")
def build(cfg: dict) -> Source:
    transport_name = cfg.get("transport", "agentmail")
    builder = _TRANSPORTS.get(transport_name)
    if builder is None:
        known = ", ".join(sorted(_TRANSPORTS))
        raise RuntimeError(
            f"slack-workspace source: unknown transport {transport_name!r}; known: {known}"
        )
    retval = builder(cfg)
    return retval
