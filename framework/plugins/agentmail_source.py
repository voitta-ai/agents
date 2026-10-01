"""AgentMail source — email-inbox Source implementation (issue #12).

Turns messages in an AgentMail inbox into Signals. Email is how several signal
types arrive with no usable API behind them: job-board notifications, community
digests, calendar invitations. Forward them to an inbox and this becomes the
ingestion path.

Credentials come from the environment, never config. The API key's env var is
named by `api_key_env` (default `AGENTMAIL_API_KEY`), matching the way the Slack
destination names its `webhook_env`, so an operator can run two agents against
different credentials without editing plugin code.

## Bodies are not in the list response

Measured against a live inbox, and the reason this plugin is not a thin wrapper:

  - `GET /v0/inboxes/{inbox}/messages` returns `{count, limit, next_page_token,
    messages}`, and each message carries `message_id`, `subject`, `from`, `to`,
    `preview`, `timestamp`, `thread_id`, `labels` — but **no body and no URL**.
    `preview` is capped at 200 characters.
  - `GET /v0/inboxes/{inbox}/messages/{message_id}` additionally returns `text`,
    `html` and `extracted_text`. On a sample message that was 1553 characters of
    text versus a 200-character preview.

Scoring a truncated preview silently changes results — a keyword in paragraph
three simply is not there to match. So `fetch_body` (default on) hydrates each
message that passes the filters with a second request. Filtering happens
*before* hydration, so a shared inbox costs one request plus one per keeper,
not one per message.

Set `fetch_body: false` for high-volume inboxes where the subject line is
genuinely enough.

AgentMail exposes no permalink field, so `Signal.url` is empty; consumers should
render `meta.subject` and `meta.from` instead of relying on a link.

## Trashed messages are hidden by default

The list endpoint omits anything labelled `trash` unless `include_trash=true` is
passed. That label can be applied by something outside this repo — another
client on the same inbox, a provider-side rule — so an inbox can look empty
while holding everything an agent wants. Measured on a live inbox: the default
listing returned 1 message and `include_trash: true` returned 50, 49 of them
labelled `trash`.

`include_trash` (default `false`) is therefore a config key, not a heuristic.
Nothing here guesses that a quiet inbox means trashed mail; an operator seeing
an unexpectedly empty inbox checks the label and sets the key. Trashed messages
keep their labels in `meta.labels`, so a scorer can still tell them apart.

## Pagination

The listing is paged: a response carries `next_page_token` when more messages
match, and `page_token` continues from it. `fetch()` follows that until it has
`limit` messages or the pages run out, so `limit` bounds the run rather than the
page. Unknown query parameters are ignored by the API rather than rejected,
which is why the token parameter is spelled exactly `page_token`. A response
that is not an object with a `messages` list raises: a source that cannot read
its own API must not report an empty inbox.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
from urllib.parse import quote

import requests

from framework.interfaces import Signal, Source
from framework.registry import register_source

_API_BASE = "https://api.agentmail.to/v0"


class AgentMailSource:
    def __init__(
        self,
        inbox: str,
        api_key: str,
        limit: int = 50,
        from_contains: list[str] | None = None,
        subject_contains: list[str] | None = None,
        fetch_body: bool = True,
        include_trash: bool = False,
        label: str = "agentmail",
        timeout: int = 30,
    ):
        self.inbox = inbox
        self.limit = limit
        self.include_trash = include_trash
        # An inbox is often shared across agents, so filter to what this one
        # cares about. Empty filters accept everything.
        self.from_contains = [s.lower() for s in (from_contains or [])]
        self.subject_contains = [s.lower() for s in (subject_contains or [])]
        self.fetch_body = fetch_body
        self.label = label
        self.timeout = timeout
        self._api_key = api_key

    @property
    def _headers(self) -> dict[str, str]:
        retval = {"Authorization": f"Bearer {self._api_key}"}
        return retval

    def _matches(self, sender: str, subject: str) -> bool:
        sender_l, subject_l = sender.lower(), subject.lower()
        if self.from_contains and not any(f in sender_l for f in self.from_contains):
            return False
        if self.subject_contains and not any(s in subject_l for s in self.subject_contains):
            return False
        return True

    def _list_messages(self) -> list[dict]:
        """Recent messages, following `next_page_token` up to `limit`."""
        messages: list[dict] = []
        page_token = ""
        while len(messages) < self.limit:
            params: dict[str, str] = {"limit": str(self.limit - len(messages))}
            if self.include_trash:
                params["include_trash"] = "true"
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{_API_BASE}/inboxes/{self.inbox}/messages",
                headers=self._headers,
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            # Fail loud on a shape this code cannot read: a silently empty run
            # is the failure mode this repo keeps paying for, and an API that
            # changed shape is exactly when a source stops seeing anything.
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                raise RuntimeError(
                    f"agentmail source: unexpected response from the messages endpoint for "
                    f"{self.inbox}: expected an object with a 'messages' list, got "
                    f"{type(payload).__name__} with keys "
                    f"{sorted(payload) if isinstance(payload, dict) else 'n/a'}"
                )
            page = payload["messages"]
            messages.extend(page)
            page_token = payload.get("next_page_token") or ""
            if not page_token or not page:
                break
        retval = messages[: self.limit]
        return retval

    def _body_for(self, message_id: str) -> str:
        """Full text for one message. Message ids contain '<', '>' and '@', so
        they must be percent-encoded into the path."""
        resp = requests.get(
            f"{_API_BASE}/inboxes/{self.inbox}/messages/{quote(message_id, safe='')}",
            headers=self._headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        msg = resp.json()
        retval = msg.get("extracted_text") or msg.get("text") or msg.get("preview") or ""
        return retval

    def fetch(self) -> list[Signal]:
        signals: list[Signal] = []
        for msg in self._list_messages():
            message_id = msg.get("message_id") or msg.get("id")
            if not message_id:
                continue
            subject = msg.get("subject", "") or ""
            sender = msg.get("from", "") or ""
            if not self._matches(sender, subject):
                continue

            body = msg.get("preview", "") or ""
            if self.fetch_body:
                body = self._body_for(str(message_id)) or body

            signals.append(
                Signal(
                    id=str(message_id),
                    text=f"{subject}\n\n{body}".strip(),
                    url="",  # AgentMail exposes no permalink
                    source=self.label,
                    meta={
                        "from": sender,
                        "to": msg.get("to", ""),
                        "subject": subject,
                        "timestamp": msg.get("timestamp", ""),
                        "thread_id": msg.get("thread_id", ""),
                        "labels": msg.get("labels", []),
                    },
                )
            )
        retval = signals
        return retval


@register_source("agentmail")
def build(cfg: dict) -> Source:
    inbox = cfg.get("inbox") or os.environ.get("AGENTMAIL_EMAIL")
    if not inbox:
        raise RuntimeError("agentmail source needs 'inbox' in config or AGENTMAIL_EMAIL in env")

    key_env = cfg.get("api_key_env", "AGENTMAIL_API_KEY")
    api_key = os.environ.get(key_env)
    if not api_key:
        raise RuntimeError(f"agentmail source needs the API key in ${key_env}")

    retval = AgentMailSource(
        inbox=inbox,
        api_key=api_key,
        limit=int(cfg.get("limit", 50)),
        from_contains=cfg.get("from_contains"),
        subject_contains=cfg.get("subject_contains"),
        fetch_body=bool(cfg.get("fetch_body", True)),
        include_trash=bool(cfg.get("include_trash", False)),
        label=cfg.get("label", "agentmail"),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
