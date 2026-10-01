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
      "bot_token_env": "SOME_BOT_TOKEN",
      "channel_id": "C0123456789",
      "wrap": {"type": "digest", "...": "the destination whose output becomes the proposal"},
      "actions": ["publish-post"],
      "timeout": 30
    }

`wrap` is any destination block; it is built through the registry exactly as a
top-level destination would be, so its own keys (and its own transport,
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

`buttons: true` (issue #74) posts the proposal as Block Kit with Post, Draft
and Discard buttons instead of the "reply do/skip" line, one message per named
action, since a button carries one proposal id. Turn it on together with
`agent approvals --listen` (framework/approval_buttons.py): without a listener
holding the Socket Mode connection, the buttons render but no interaction is
ever delivered. The reply path keeps working either way, and the reply grammar
is repeated in the message's context line so it is still discoverable.

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
messages rather than truncated (issue #73). With `buttons: true` the same
conversion runs before the Block Kit message is built, on the section text and
on the notification fallback alike, since a human reads both; the blocks carry
their own length handling, so that path does not split into several messages.
What is *recorded* is untouched either way: `body_md` in the proposals store
stays Markdown, because the publish step that later renders it wants Markdown,
not Slack's dialect. `username` / `icon_emoji` / `icon_url` (issue #77) name
the proposal's message in both modes.

**Announced first, stored second** (issue #73 review). `flush()` reserves each
proposal's id, posts the announcement, and only then writes the proposals to
disk, because a long digest is now posted as several messages and a failure
part-way through used to leave a live, approvable proposal whose instructions
never reached the channel. In the new order a failed announcement stores
nothing: the runner marks no signals seen and the next run proposes again. Two
configured actions are stored in one write (`add_all`), so a run cannot leave
one of them behind for a retry to duplicate. `buttons: true` posts one message
per proposal and is held to the same rule: every id is reserved first, every
message goes out, and only then is anything stored, so a failure on the second
of two button messages leaves neither clickable rather than one live. The
accepted cost is the other half of at-least-once — a failure after the last
part was posted means the channel holds an announcement for a proposal that
was never stored, and the next run posts a second one with a new id. A stale
id nobody can act on is a dead message; a live proposal nobody was told how to
approve is a silent one, and this repo pays for silence.

The instruction lines (`Reply "do <id>"`) sit directly under the title, above
the digest, for the same reason: they have to be in the first message of a
split, not the last. With `buttons: true` there are no such lines: the buttons
are the instruction, and the reply grammar already appears once in the context
line `proposal_blocks` builds.

The proposal is the delivery. The runner marks signals seen after `flush()`
returns, and this `flush()` returns only once the proposal is on disk and
announced, so the seen-store semantics need no change: an approved-then-failed
action is a matter for the proposal's own status, not for re-fetching signals.
"""
from __future__ import annotations

from pathlib import Path

import requests

from framework.approval_buttons import proposal_blocks
from framework.interfaces import Destination, ScoredSignal
from framework.proposals import ProposalStore, proposals_path
from framework.registry import build_destination, register_destination
from framework.slack import (
    SlackIdentity,
    SlackTransport,
    identity_from_config,
    post_message,
    to_mrkdwn,
    transport_from_config,
)


class ApprovalDestination:
    def __init__(
        self,
        wrapped: Destination,
        actions: list[str],
        store: ProposalStore,
        transport: SlackTransport,
        timeout: int = 30,
        identity: SlackIdentity | None = None,
        buttons: bool = False,
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
        self._transport = transport
        self._timeout = timeout
        self._identity = identity or SlackIdentity()
        self._buttons = buttons

    def send(self, scored: ScoredSignal) -> None:
        self._wrapped.send(scored)

    def _post(self, text: str) -> None:
        post_message(
            self._transport, text, timeout=self._timeout, identity=self._identity
        )

    def _post_buttons(self, text: str, proposal: dict) -> None:
        """One proposal as Block Kit: the content, then Post / Draft / Discard.

        Blocks carry their own length handling, so this does not go through
        `post_message`'s splitting, but it converts the same way: the section
        blocks and the notification fallback are both mrkdwn, because both are
        read by a human. `text` is the fallback a push notification and a
        screen reader get when the blocks are not rendered; the reply grammar
        lives in the context line `proposal_blocks` builds, and is deliberately
        not repeated here.
        """
        pid = proposal["id"]
        action = proposal["action"]
        rendered = to_mrkdwn(text)
        body: dict = {
            "text": f"{rendered}\n\nProposal {pid}: {action}.",
            "blocks": proposal_blocks(rendered, pid, action),
        }
        body.update(self._identity.payload())
        # Through the transport, not a raw requests.post. This call site was
        # written when the destination held a webhook URL, and posting the
        # payload straight at it was the same thing. It is not any more: the
        # transport may be a bot token plus a channel, and `post()` is what
        # knows the difference. Passing the object to `requests.post` instead
        # fails with `InvalidURL: Host '<SlackTransport object ...>'`, and only
        # ever on the buttons path, which is why nothing caught it until a
        # config turned buttons on.
        self._transport.post(body)

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
        # Every id is taken before anything is announced, and nothing is stored
        # until every announcement is out: both modes announce first and store
        # second, in one write.
        reserved = [self._store.reserve(action, payload) for action in self._actions]
        if self._buttons:
            # One message per proposal: the buttons carry a single proposal id,
            # so two actions in one message would leave the second unclickable.
            for proposal in reserved:
                self._post_buttons(f"**{title}**\n\n{body}", proposal)
            self._store.add_all(reserved)
            return

        lines = [
            f'Proposal {p["id"]}: {p["action"]}. Reply "do {p["id"]}" or "skip {p["id"]}".'
            for p in reserved
        ]
        # Announce first, store second, and put the instructions above the
        # digest rather than after it (see the note in the module docstring).
        self._post(f"**{title}**\n\n" + "\n".join(lines) + "\n\n" + body)
        self._store.add_all(reserved)


# Transport: this destination posts through `SlackTransport` like every other
# (issue #86). It used to take a raw webhook URL, which had two consequences
# worth naming, because both were silent. Its proposal messages ignored the
# `username`/`icon_emoji` it accepted and went out under whichever app owned the
# webhook. And `buttons: true` could never have worked: Block Kit buttons have
# to be tidied up once a proposal is decided, that edit is `chat.update`, and
# `SlackTransport.update()` refuses a webhook-posted message because Slack
# returns no `ts` for one -- pointing the caller at `bot_token_env` +
# `channel_id`, which this build() then rejected. Accepting the same transport
# config as the rest closes both.


@register_destination("approval")
def build(cfg: dict) -> Destination:
    timeout = int(cfg.get("timeout", 30))
    transport = transport_from_config(cfg, "approval destination", timeout)

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
        transport=transport,
        timeout=timeout,
        identity=identity_from_config(cfg, "approval destination"),
        buttons=bool(cfg.get("buttons", False)),
    )
    return retval
