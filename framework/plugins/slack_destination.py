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

Sheets/email destinations are explicitly out of scope for v1.

Contract: see docs/PLUGIN-CONTRACT.md — routing and gating are the
destination's job, not the framework's.
"""
from __future__ import annotations

import os

import requests

from framework.interfaces import Destination, ScoredSignal
from framework.registry import register_destination


class _Route:
    def __init__(self, webhook_url: str, min_score: int, channel_label: str = ""):
        self.webhook_url = webhook_url
        self.min_score = min_score
        self.channel_label = channel_label


class SlackDestination:
    def __init__(self, routes: list[_Route], timeout: int = 30):
        # Highest bar first, so the first match wins and a hot signal is not
        # also sent to the firehose.
        self._routes = sorted(routes, key=lambda r: r.min_score, reverse=True)
        self._timeout = timeout

    def _format(self, scored: ScoredSignal) -> str:
        sig = scored.signal
        text = f"*[{scored.score}]* {sig.text}"
        if sig.url:
            text += f"\n{sig.url}"
        return text

    def send(self, scored: ScoredSignal) -> None:
        for route in self._routes:
            if scored.score >= route.min_score:
                resp = requests.post(
                    route.webhook_url,
                    json={"text": self._format(scored)},
                    headers={"Content-type": "application/json"},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                return


def _build_route(cfg: dict, label: str) -> _Route:
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
    )
    return retval


@register_destination("slack")
def build(cfg: dict) -> Destination:
    declared = cfg.get("routes")
    if isinstance(declared, list) and declared:
        routes = [
            _build_route(r, r.get("channel") or f"route[{i}]") for i, r in enumerate(declared)
        ]
    else:
        # Flat single-route form, for agents that only ever post to one channel.
        routes = [_build_route(cfg, cfg.get("channel") or "destination")]

    retval = SlackDestination(routes=routes, timeout=int(cfg.get("timeout", 30)))
    return retval
