"""Slack destination — the v1 reference Destination implementation (issue #3).

Tiered routing, carried over from hq/leads/signals/config.yaml, expressed as the
`routes` list the scaffolded example config already declared:

    destination:
      type: slack
      routes:
        - min_score: 30
          webhook_env: SLACK_LEADS_WEBHOOK_URL
          channel: "#leads"
        - min_score: 10
          webhook_env: SLACK_FIREHOSE_WEBHOOK_URL
          channel: "#leads-firehose"

A signal is delivered to the **highest** route it qualifies for, and only that
one — a hot lead is not also firehosed, because double-posting the best signals
is how you train yourself to ignore a channel.

One route is a perfectly good configuration: that is the single-channel
behaviour the toptal agent uses.

Webhook URLs come from the environment, never from config: each route names its
own env var via `webhook_env`. That keeps live webhooks out of git — the failure
this repo already had to clean up twice (issue #7).

Slack incoming webhooks are bound to a channel when they are created, so the
`channel` key here is a label for humans reading the config, not routing.

Two per-route keys shape what a message says (issue #64). Both may also be set
once at the top level of the destination block, where they are the default for
every route that does not set its own; the flat single-route form reads them
from the same place it reads `webhook_env`.

    username      the name this route's messages are posted under, and
    icon_emoji    the icon beside them (`:rocket:`, with the colons) or
    icon_url      an image url instead. Unset means Slack uses the webhook's
                  own identity, exactly as before. Several agents post through
                  one webhook, so without these two digests landing seconds
                  apart read as one message (issue #77).
    footer        a string appended verbatim as the last line of every message
                  this route sends, streamed or batched. It exists for standing
                  lines that must appear on every message, so nothing here
                  wraps, truncates or formats it, and no other key can suppress
                  it. Empty (the default) appends nothing.
    show_reasons  render `ScoredSignal.reasons` as a bulleted block under the
                  score. Default true: both scorers fill `reasons` in (the llm
                  scorer puts its rationale and the answering model there), and
                  a lead posted without them is harder to triage. A signal with
                  no reasons renders exactly as it did before this key existed,
                  and so does any signal under `show_reasons: false`.

Message text is Markdown on the way in and mrkdwn on the way out: it goes
through `framework.slack.post_message`, which converts it and splits a message
over the block limit into several, in order (issue #73). That is why the header
here is written `**[42]**` and not `*[42]*` — the text this module builds is
Markdown like everything else it carries, and the one post site renders it.

Sheets/email destinations are explicitly out of scope for v1.

Contract: see docs/PLUGIN-CONTRACT.md — routing and gating are the
destination's job, not the framework's.
"""
from __future__ import annotations

import os

from framework.interfaces import Destination, ScoredSignal
from framework.registry import register_destination
from framework.slack import SlackIdentity, identity_from_config, post_message


class _Route:
    def __init__(
        self,
        webhook_url: str,
        min_score: int,
        channel_label: str = "",
        footer: str = "",
        show_reasons: bool = True,
        identity: SlackIdentity | None = None,
    ):
        self.webhook_url = webhook_url
        self.min_score = min_score
        self.channel_label = channel_label
        self.footer = footer
        self.show_reasons = show_reasons
        self.identity = identity or SlackIdentity()


class SlackDestination:
    def __init__(self, routes: list[_Route], timeout: int = 30, mode: str = "stream"):
        if mode not in ("stream", "batch"):
            raise ValueError(f"slack destination: mode must be 'stream' or 'batch', got {mode!r}")
        # Highest bar first, so the first match wins and a hot signal is not
        # also sent to the firehose.
        self._routes = sorted(routes, key=lambda r: r.min_score, reverse=True)
        self._timeout = timeout
        self._mode = mode
        self._buffered: dict[int, list[str]] = {}

    def _format(self, route: _Route, scored: ScoredSignal, compact: bool = False) -> str:
        """Render one signal.

        Streaming keeps the full `signal.text` — that is what deployed agents
        already post, and changing it would silently alter their output.
        Batching uses `meta.title` when a source provides one, because ten full
        post bodies in a single Slack message is unreadable. Sources without a
        title fall back to the full text either way.

        The reasons block follows the url, in both modes: it is two short lines
        that say why this signal scored what it did, which is exactly what a
        batched list of ten is otherwise missing.
        """
        sig = scored.signal
        body = sig.text
        if compact and isinstance(sig.meta, dict) and sig.meta.get("title"):
            body = sig.meta["title"]
        text = f"**[{scored.score}]** {body}"
        if sig.url:
            text += f"\n{sig.url}"
        if route.show_reasons and scored.reasons:
            text += "\n" + "\n".join(f"• {reason}" for reason in scored.reasons)
        return text

    def _post(self, route: _Route, text: str) -> None:
        post_message(
            route.webhook_url,
            text,
            timeout=self._timeout,
            footer=route.footer,
            identity=route.identity,
        )

    def _route_for(self, scored: ScoredSignal) -> _Route | None:
        for route in self._routes:
            if scored.score >= route.min_score:
                return route
        return None

    def send(self, scored: ScoredSignal) -> None:
        route = self._route_for(scored)
        if route is None:
            return
        if self._mode == "stream":
            self._post(route, self._format(route, scored))
            return
        self._buffered.setdefault(id(route), []).append(
            self._format(route, scored, compact=True)
        )

    def flush(self) -> None:
        """Emit one message per route in batch mode. No-op when streaming."""
        if self._mode != "batch":
            return
        for route in self._routes:
            lines = self._buffered.get(id(route))
            if not lines:
                continue
            header = f"**{len(lines)} signal(s)**"
            if route.channel_label:
                header += f" — {route.channel_label}"
            self._post(route, header + "\n\n" + "\n\n".join(lines))
        self._buffered.clear()


def _footer(cfg: dict, label: str, default: str) -> str:
    """`footer` for this route, falling back to the destination-level one."""
    value = cfg.get("footer", default)
    if not isinstance(value, str):
        raise ValueError(f"slack destination: {label} 'footer' must be a string, got {value!r}")
    return value


def _show_reasons(cfg: dict, label: str, default: bool) -> bool:
    """`show_reasons` for this route, falling back to the destination-level one.

    Only a real boolean: a string "false" is a config typo that would otherwise
    turn the block on and read as if the operator had turned it off.
    """
    value = cfg.get("show_reasons", default)
    if not isinstance(value, bool):
        raise ValueError(
            f"slack destination: {label} 'show_reasons' must be true or false, got {value!r}"
        )
    return value


def _build_route(
    cfg: dict,
    label: str,
    footer: str = "",
    show_reasons: bool = True,
    identity: SlackIdentity | None = None,
) -> _Route:
    env_name = cfg.get("webhook_env")
    if not env_name:
        raise RuntimeError(f"slack destination: {label} needs a 'webhook_env'")
    webhook_url = os.environ.get(env_name)
    if not webhook_url:
        raise RuntimeError(f"slack destination: {label} needs the webhook URL in ${env_name}")
    retval = _Route(
        webhook_url=webhook_url,
        min_score=int(cfg.get("min_score", 0)),
        channel_label=cfg.get("channel", ""),
        footer=_footer(cfg, label, footer),
        show_reasons=_show_reasons(cfg, label, show_reasons),
        identity=identity_from_config(cfg, f"slack destination: {label}", identity),
    )
    return retval


@register_destination("slack")
def build(cfg: dict) -> Destination:
    footer = _footer(cfg, "destination", "")
    show_reasons = _show_reasons(cfg, "destination", True)
    identity = identity_from_config(cfg, "slack destination", None)
    declared = cfg.get("routes")
    if isinstance(declared, list) and declared:
        routes = [
            _build_route(r, r.get("channel") or f"route[{i}]", footer, show_reasons, identity)
            for i, r in enumerate(declared)
        ]
    else:
        # Flat single-route form, for agents that only ever post to one channel.
        routes = [
            _build_route(cfg, cfg.get("channel") or "destination", footer, show_reasons, identity)
        ]

    retval = SlackDestination(
        routes=routes,
        timeout=int(cfg.get("timeout", 30)),
        mode=cfg.get("mode", "stream"),
    )
    return retval
