"""Slack-workspace source — read community workspaces we don't administer (issue #19).

Every Slack bot token this repo holds is scoped to a workspace we administer, so
there is no bot path into a community workspace, and getting an app installed
there needs admin approval we are unlikely to get. See issue #19 for the full
feasibility writeup.

## Two transports, one shipped

`transport:` selects the implementation so a second transport can be added
later without reshaping the config or the Source interface:

  - `agentmail` (shipped here) — the workspace's own notification/digest
    emails are forwarded to an AgentMail inbox; this plugin is a thin
    composition wrapper over the existing `agentmail` source
    (`framework/plugins/agentmail_source.py`). Zero new auth, unambiguously
    within ToS, identical in shape to an already-deployed agent.
    Coarse granularity: you get what Slack chooses to email, not full channel
    history, and per-message links rather than threads.

  - `session` (NOT implemented) — an `xoxc` + `d`-cookie session token reads
    as the logged-in user with full history and threads. A working client
    exists in the voitta-ai/skillz catalog
    (`slack-xoxc-session-client`), but whether to automate a human session
    against a community workspace is an explicit human authorization decision
    that has not been made (same posture as issue #18 for LinkedIn). Selecting
    `transport: session` raises rather than silently doing something else.

## Channel scoping is best-effort

AgentMail's message list carries no structured channel field (see
agentmail_source.py's docstring on the shape it hands back) — Slack's digest
emails are just subject/sender/body. So `channels:` here is a coarse text
filter over the combined subject+body, matching the "coarse granularity"
tradeoff issue #19 accepted for transport A. It is not a substitute for the
Slack API's channel-scoped history that transport B would give.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
from typing import Callable

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
    # Deliberately not implemented. A working xoxc/session client exists
    # (voitta-ai/skillz: slack-xoxc-session-client) but whether to automate a
    # logged-in human session against a community workspace is an explicit
    # authorization call this PR does not make. Fail loudly and specifically
    # rather than silently falling back to another transport.
    raise RuntimeError(
        "slack-workspace source: transport 'session' is not implemented. "
        "Using an xoxc session token requires an explicit authorization "
        "decision that has not been made (see issue #19). Use "
        "transport: agentmail, or get that decision made first."
    )


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
