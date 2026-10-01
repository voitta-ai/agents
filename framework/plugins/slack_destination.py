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
behaviour most agents here use.

Credentials come from the environment, never from config: a route names its own
env var via `webhook_env` or `bot_token_env`. That keeps live webhooks and
tokens out of git — the failure this repo already had to clean up twice
(issue #7).

A route posts either through an incoming webhook (`webhook_env`) or through
`chat.postMessage` (`bot_token_env` plus `channel_id`), exactly one of the two
(issue #86). The API form is what makes `username` and `icon_emoji` work at
all: a modern incoming webhook ignores both, so two agents sharing one webhook
arrive under the same name however they are configured. A destination-level
`bot_token_env` is the default for every route, so one token can serve several
routes that each name their own `channel_id`.

Slack incoming webhooks are bound to a channel when they are created, so with
`webhook_env` the `channel` key is a label for humans reading the config, not
routing. With `bot_token_env` the routing is `channel_id`, and `channel` stays
the label.

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

from framework.interfaces import Destination, ScoredSignal
from framework.drafter import DEFAULT_SYSTEM, DraftError, Drafter, load_grounding
from framework.llm import LLMError, build_client
from framework.registry import register_destination
from framework.slack import (
    SlackIdentity,
    SlackTransport,
    check_footer,
    identity_from_config,
    post_message,
    transport_from_config,
)


class _Route:
    def __init__(
        self,
        transport: SlackTransport,
        min_score: int,
        channel_label: str = "",
        footer: str = "",
        show_reasons: bool = True,
        identity: SlackIdentity | None = None,
    ):
        self.transport = transport
        self.min_score = min_score
        self.channel_label = channel_label
        self.footer = footer
        self.show_reasons = show_reasons
        self.identity = identity or SlackIdentity()


class SlackDestination:
    def __init__(
        self,
        routes: list[_Route],
        timeout: int = 30,
        mode: str = "stream",
        drafter: Drafter | None = None,
    ):
        if mode not in ("stream", "batch"):
            raise ValueError(f"slack destination: mode must be 'stream' or 'batch', got {mode!r}")
        # Highest bar first, so the first match wins and a hot signal is not
        # also sent to the firehose.
        self._routes = sorted(routes, key=lambda r: r.min_score, reverse=True)
        self._timeout = timeout
        self._mode = mode
        self._drafter = drafter
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
        batched list of ten is otherwise missing. It is separated by a blank
        line and set in italics, because it is the one part of the message the
        agent wrote rather than quoted, and a reader skimming a channel has to
        be able to tell those apart without re-reading.
        """
        sig = scored.signal
        body = sig.text
        if compact and isinstance(sig.meta, dict) and sig.meta.get("title"):
            body = sig.meta["title"]
        text = f"**[{scored.score}]** {body}"
        if sig.url:
            text += f"\n{sig.url}"
        if route.show_reasons and scored.reasons:
            # A blank line and italics, because the two halves of this message
            # have different authors. Everything above is the poster's own
            # words; everything below is ours about them. Run together on
            # consecutive lines they read as one block, and an operator
            # skimming a channel cannot tell which sentences to trust as
            # quotation and which to read as judgement.
            text += "\n\n" + "\n".join(f"• _{reason}_" for reason in scored.reasons)
        if self._drafter is not None and not compact:
            text += "\n\n" + self._draft_block(scored)
        return text

    def _draft_block(self, scored: ScoredSignal) -> str:
        """The drafted sections, or a visible note saying why there are none.

        A failed draft never costs the lead. The signal is the thing worth
        having, and dropping a real lead because a second model call misfired
        would be a bad trade -- but a lead that looked complete while silently
        carrying no draft would be worse than either, so the reason is rendered
        exactly where the draft would have been.
        """
        try:
            drafted = self._drafter.draft(scored)
        except (DraftError, LLMError) as exc:
            retval = f"_draft unavailable — {str(exc)[:200]}_"
            return retval
        parts = []
        for name, value in drafted.items():
            label = name.replace("_", " ")
            parts.append(f"*{label}*\n{value}")
        retval = "\n\n".join(parts)
        return retval

    def _post(self, route: _Route, text: str) -> None:
        post_message(
            route.transport,
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


_TRANSPORT_KEYS = ("webhook_env", "bot_token_env", "channel_id")


def _transport_cfg(cfg: dict, defaults: dict) -> dict:
    """A route's transport keys, falling back to the destination-level ones.

    One bot token named once at the top, with a `channel_id` per route, is the
    shape a two-tier agent wants; a route naming its own `webhook_env` still
    overrides all of it.
    """
    retval = {key: cfg[key] for key in _TRANSPORT_KEYS if key in cfg}
    if not any(key in retval for key in ("webhook_env", "bot_token_env")):
        retval = {key: defaults[key] for key in _TRANSPORT_KEYS if key in defaults} | retval
    return retval


def _footer(cfg: dict, label: str, default: str) -> str:
    """`footer` for this route, falling back to the destination-level one."""
    value = cfg.get("footer", default)
    if not isinstance(value, str):
        raise ValueError(f"slack destination: {label} 'footer' must be a string, got {value!r}")
    # Here rather than only at post time: a footer too long to leave room for a
    # message is knowable before the run fetches, scores or asks a model
    # anything, and this names the config key that has to change.
    check_footer(value, f"slack destination: {label} 'footer'")
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
    timeout: int = 30,
    transport_defaults: dict | None = None,
) -> _Route:
    retval = _Route(
        transport=transport_from_config(
            _transport_cfg(cfg, transport_defaults or {}),
            f"slack destination: {label}",
            timeout,
        ),
        min_score=int(cfg.get("min_score", 0)),
        channel_label=cfg.get("channel", ""),
        footer=_footer(cfg, label, footer),
        show_reasons=_show_reasons(cfg, label, show_reasons),
        identity=identity_from_config(cfg, f"slack destination: {label}", identity),
    )
    return retval


_DRAFT_RESERVED = frozenset({"grounding", "max_grounding_chars", "prompt", "fields", "system",
                            "max_signal_chars", "llm", "_comment", "_workspace", "_root"})


def _build_drafter(cfg: dict) -> Drafter | None:
    """The optional `drafter` block (issue #83), or None.

    Only `mode: stream` renders a draft: a batched message is a compact list of
    many signals, and a per-signal draft inside one would be unreadable and
    would cost one model call per line of it.
    """
    block = cfg.get("drafter")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise RuntimeError("slack destination: 'drafter' must be an object")
    if cfg.get("mode", "stream") != "stream":
        raise RuntimeError(
            "slack destination: 'drafter' needs mode 'stream'; a batched message is a "
            "compact list of many signals, and a draft per line would be unreadable"
        )

    prompt = block.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise RuntimeError("slack destination: 'drafter' needs a 'prompt'")

    fields = block.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RuntimeError(
            "slack destination: 'drafter' needs 'fields', an object of name -> "
            "description written for the model"
        )
    bad = sorted(n for n, d in fields.items() if not isinstance(d, str) or not d.strip())
    if bad:
        raise RuntimeError(
            f"slack destination: drafter fields {', '.join(bad)} need a non-empty description"
        )

    grounding_paths = block.get("grounding")
    if not isinstance(grounding_paths, list) or not all(
        isinstance(p, str) and p.strip() for p in grounding_paths
    ):
        raise RuntimeError(
            "slack destination: 'drafter' needs 'grounding', a non-empty list of file "
            "paths. A draft makes claims about us, and claims need provenance"
        )
    grounding = load_grounding(
        grounding_paths,
        int(block.get("max_grounding_chars", 60000)),
        "slack destination drafter",
    )

    # The drafter's own chain if it names one, else the deployment's waterfall.
    llm_cfg = dict(block)
    for key in ("_workspace", "_root"):
        if key in cfg and key not in llm_cfg:
            llm_cfg[key] = cfg[key]
    llm = build_client(llm_cfg, what="slack destination drafter")

    retval = Drafter(
        llm=llm,
        grounding=grounding,
        prompt=prompt,
        fields=fields,
        system=block.get("system") or DEFAULT_SYSTEM,
        max_signal_chars=int(block.get("max_signal_chars", 4000)),
    )
    return retval


@register_destination("slack")
def build(cfg: dict) -> Destination:
    timeout = int(cfg.get("timeout", 30))
    footer = _footer(cfg, "destination", "")
    show_reasons = _show_reasons(cfg, "destination", True)
    identity = identity_from_config(cfg, "slack destination", None)
    declared = cfg.get("routes")
    if isinstance(declared, list) and declared:
        routes = [
            _build_route(
                r,
                r.get("channel") or f"route[{i}]",
                footer,
                show_reasons,
                identity,
                timeout,
                cfg,
            )
            for i, r in enumerate(declared)
        ]
    else:
        # Flat single-route form, for agents that only ever post to one channel.
        routes = [
            _build_route(
                cfg, cfg.get("channel") or "destination", footer, show_reasons, identity, timeout
            )
        ]

    drafter = _build_drafter(cfg)

    retval = SlackDestination(
        routes=routes,
        timeout=timeout,
        mode=cfg.get("mode", "stream"),
        drafter=drafter,
    )
    return retval
