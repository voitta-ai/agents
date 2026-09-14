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

  - `channels` are names. They are resolved to ids once per run through
    `conversations.list` (public and private, archived excluded, paginated).
    An unknown name raises and lists what the workspace does have, so a typo
    is a startup error and not an empty digest.
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
    permalink from `chat.getPermalink`. `meta` carries `workspace`,
    `channel` (the name), `user` (display name), `reply_count` and
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

Config keys (session transport):

  workspace       label for Signal.source / meta, required
  subdomain       the `<subdomain>` in `<subdomain>.slack.com`, required
  browser         pycookiecheat browser name (chrome, chromium, brave,
                  firefox, slack); default chrome
  channels        list of channel names, required and non-empty
  since_hours     lookback window; default 24
  include_threads fold thread replies into the parent; default true
  timeout         per-request HTTP timeout in seconds; default 30

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
        "conversations.history",
        "conversations.replies",
        "chat.getPermalink",
        "users.info",
        "users.list",
    }
)

_USER_AGENT = "Mozilla/5.0"
_PAGE_LIMIT = 200
_MAX_RATE_LIMIT_RETRIES = 5


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
            raise RuntimeError(
                f"slack-workspace source (transport: session): {method} on "
                f"{self.subdomain} failed: {result.get('error', 'unknown error')}"
            )
        retval = result
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
    ):
        self._client = client
        self._workspace = workspace
        self._channel_names = [c.lstrip("#") for c in channels]
        self._since_hours = since_hours
        self._include_threads = include_threads
        self._user_names: dict[str, str] = {}

    def _resolve_channels(self) -> dict[str, str]:
        """Map configured channel names to ids; unknown names are a hard error."""
        listed = self._client.paginate(
            "conversations.list",
            "channels",
            types="public_channel,private_channel",
            exclude_archived="true",
        )
        by_name = {c["name"]: c["id"] for c in listed if c.get("name") and c.get("id")}
        missing = [n for n in self._channel_names if n not in by_name]
        if missing:
            raise RuntimeError(
                f"slack-workspace source (transport: session): channel(s) "
                f"{', '.join(missing)} not found in {self._workspace}; the "
                f"workspace has: {', '.join(sorted(by_name))}"
            )
        retval = {n: by_name[n] for n in self._channel_names}
        return retval

    def _user_name(self, message: dict[str, Any]) -> str:
        user_id = message.get("user")
        if not user_id:
            # Bot and integration posts carry no user; fall back to what the
            # message itself says.
            retval = message.get("username") or message.get("bot_id") or "unknown"
            return retval
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
            lines.append(f"  {self._user_name(reply)}: {reply.get('text', '')}")
        retval = lines
        return retval

    def _signal(self, channel_name: str, channel_id: str, message: dict[str, Any]) -> Signal:
        ts = message["ts"]
        reply_count = int(message.get("reply_count") or 0)
        lines = [message.get("text", "")]
        if self._include_threads and reply_count > 0:
            lines.extend(self._replies_text(channel_id, ts))
        permalink = self._client.call(
            "chat.getPermalink", channel=channel_id, message_ts=ts
        ).get("permalink", "")
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
                "transport": "session",
            },
        )
        return retval

    def fetch(self) -> list[Signal]:
        self._user_names = {}
        oldest = time.time() - self._since_hours * 3600
        signals: list[Signal] = []
        for channel_name, channel_id in self._resolve_channels().items():
            history = self._client.paginate(
                "conversations.history",
                "messages",
                channel=channel_id,
                oldest=f"{oldest:.6f}",
            )
            for message in history:
                if not self._is_top_level(message):
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
            "'channels' list of channel names"
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
