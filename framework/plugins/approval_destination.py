"""Approval destination: turn a run's output into a proposal (issue #21).

Wraps another destination and, at `flush()`, records what that destination
*would* have posted as a pending proposal in `<workspace>/state/proposals.json`
instead of letting it post. The rendered content goes to a Slack webhook with
one line per proposed action:

    Proposal p7-k3f: publish-post. Reply "do p7-k3f" or "skip p7-k3f".

A separate `agent approvals` pass (see `framework/approvals.py`) reads the
channel, finds those replies from trusted users, and runs the action's steps.
Nothing in this module executes anything.

Config block:

    "destination": {
      "type": "approval",
      "webhook_env": "SOME_WEBHOOK",
      "wrap": {"type": "digest", "...": "the destination whose output becomes the proposal"},
      "actions": ["publish-post"],
      "timeout": 30
    }

`wrap` is any destination block; it is built through the registry exactly as a
top-level destination would be, so its own keys (and its own `webhook_env`,
which it still needs to construct) are its business. It must define
`render() -> dict` with at least `title` and `body_md`; the digest destination
does (same getattr discovery the runner uses for `flush()`). Wrapping a
destination that cannot render is a configuration error and fails at build
time, not at the end of a run that has already fetched and scored.

`actions` names entries of the workspace config's top-level `"actions"` map,
which this destination never reads: it records the name, and the approvals
pass looks the steps up when a human says `do`. One proposal is recorded per
name, each with its own id, so a run whose output feeds two actions can have
one approved and the other skipped.

The workspace path is not part of the config block a human writes. The CLI
injects it as `_workspace` before building, because the registry hands a
factory only its own block (docs/PLUGIN-CONTRACT.md) and the proposals store
lives beside the dedupe state, not beside the config. It passes `_workspace`
and `_root` on to the `wrap` block it builds, since the CLI never sees that one.

Two behaviours worth knowing:

  - A run with nothing to propose (the wrapped `render()` says `empty`) posts
    the wrapped destination's empty message and records nothing. Asking a
    human to approve "Nothing new this run" would train them to type `skip`
    without reading, which defeats the gate.
  - If `render()` raises, the error is posted to the webhook and re-raised.
    The runner then marks nothing seen and the next run re-proposes, which is
    the at-least-once behaviour the rest of the framework has.

The proposal message goes out through `framework.slack.post_message`, so the
drafted Markdown arrives as mrkdwn and a long proposal is split into several
messages rather than truncated (issue #73). What is *recorded* is untouched:
`body_md` in the proposals store stays Markdown, because the publish step that
later renders it wants Markdown, not Slack's dialect. `username` /
`icon_emoji` / `icon_url` (issue #77) name the proposal's message.

**Announced first, stored second** (issue #73 review). `flush()` reserves each
proposal's id, posts the announcement, and only then writes the proposals to
disk, because a long digest is now posted as several messages and a failure
part-way through used to leave a live, approvable proposal whose instructions
never reached the channel. In the new order a failed announcement stores
nothing: the runner marks no signals seen and the next run proposes again. The
accepted cost is the other half of at-least-once — a failure after the last
part was posted means the channel holds an announcement for a proposal that
was never stored, and the next run posts a second one with a new id. A stale
id nobody can act on is a dead message; a live proposal nobody was told how to
approve is a silent one, and this repo pays for silence.

The instruction lines (`Reply "do <id>"`) sit directly under the title, above
the digest, for the same reason: they have to be in the first message of a
split, not the last.

The proposal is the delivery. The runner marks signals seen after `flush()`
returns, and this `flush()` returns only once the proposal is on disk and
announced, so the seen-store semantics need no change: an approved-then-failed
action is a matter for the proposal's own status, not for re-fetching signals.
"""
from __future__ import annotations

import os
from pathlib import Path

from framework.interfaces import Destination, ScoredSignal
from framework.proposals import ProposalStore, proposals_path
from framework.registry import build_destination, register_destination
from framework.slack import SlackIdentity, identity_from_config, post_message


class ApprovalDestination:
    def __init__(
        self,
        wrapped: Destination,
        actions: list[str],
        store: ProposalStore,
        webhook_url: str,
        timeout: int = 30,
        identity: SlackIdentity | None = None,
    ):
        render = getattr(wrapped, "render", None)
        if not callable(render):
            raise RuntimeError(
                "approval destination: the wrapped destination "
                f"{type(wrapped).__name__} has no render(); only destinations "
                "that can hand back their output (e.g. 'digest') can be wrapped"
            )
        self._wrapped = wrapped
        self._render = render
        self._actions = actions
        self._store = store
        self._webhook_url = webhook_url
        self._timeout = timeout
        self._identity = identity or SlackIdentity()

    def send(self, scored: ScoredSignal) -> None:
        self._wrapped.send(scored)

    def _post(self, text: str) -> None:
        post_message(
            self._webhook_url, text, timeout=self._timeout, identity=self._identity
        )

    def flush(self) -> None:
        try:
            rendered = self._render()
        except Exception as exc:
            self._post(f":warning: proposal not generated — {exc}")
            raise
        if not isinstance(rendered, dict) or "body_md" not in rendered:
            raise RuntimeError(
                "approval destination: wrapped render() must return a dict with "
                f"'body_md', got {type(rendered).__name__}"
            )

        title = rendered.get("title") or "Proposal"
        body = rendered["body_md"]
        footer = rendered.get("footer")
        if footer:
            body += f"\n\n{footer}"

        if rendered.get("empty"):
            self._post(f"**{title}**\n\n{body}")
            return

        payload = dict(rendered)
        reserved = [self._store.reserve(action, payload) for action in self._actions]
        lines = [
            f'Proposal {p["id"]}: {p["action"]}. Reply "do {p["id"]}" or "skip {p["id"]}".'
            for p in reserved
        ]
        # Announce first, store second, and put the instructions above the
        # digest rather than after it (see the note in the module docstring).
        self._post(f"**{title}**\n\n" + "\n".join(lines) + "\n\n" + body)
        for proposal in reserved:
            self._store.add(proposal)


@register_destination("approval")
def build(cfg: dict) -> Destination:
    env_name = cfg.get("webhook_env")
    if not env_name:
        raise RuntimeError("approval destination needs a 'webhook_env'")
    webhook_url = os.environ.get(env_name)
    if not webhook_url:
        raise RuntimeError(f"approval destination needs the webhook URL in ${env_name}")

    wrap = cfg.get("wrap")
    if not isinstance(wrap, dict) or not wrap.get("type"):
        raise RuntimeError("approval destination needs a 'wrap' block with a 'type'")

    actions = cfg.get("actions")
    if not isinstance(actions, list) or not actions or not all(
        isinstance(a, str) and a for a in actions
    ):
        raise RuntimeError("approval destination needs 'actions': a non-empty list of action names")

    workspace = cfg.get("_workspace")
    if not workspace:
        raise RuntimeError(
            "approval destination needs the workspace path; the CLI injects it as "
            "'_workspace' (this destination cannot be built outside `agent run`)"
        )

    # The wrapped destination is built here, not by the CLI, so it gets the
    # run-level paths from this block: a wrapped `digest` needs both for its
    # LLM waterfall (see framework/llm.py).
    wrap["_workspace"] = workspace
    if cfg.get("_root"):
        wrap["_root"] = cfg["_root"]

    retval = ApprovalDestination(
        wrapped=build_destination(wrap),
        actions=list(actions),
        store=ProposalStore(proposals_path(Path(workspace))),
        webhook_url=webhook_url,
        timeout=int(cfg.get("timeout", 30)),
        identity=identity_from_config(cfg, "approval destination"),
    )
    return retval
