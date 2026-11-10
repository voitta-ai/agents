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
`post_label` renames the Post button (an action that drafts for a human wants
"Draft application", not "Post"), and `draft_button: false` drops the Draft
button for an action with no WordPress step to draft (issue #76).

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

**`mode: "signal"`** (issue #65). The default, `mode: "run"`, is everything
above: one proposal per action per run, from the wrapped destination's whole
rendered output. A lead agent needs one proposal per *lead*, so each can be
approved or skipped on its own. In signal mode the wrapped destination is a
`slack` destination and is never allowed to post: it is used for two things
only, its gate (`accepts()`, its routes' `min_score`) and its rendering of one
lead (`render_signal()`, draft and footer included). So a lead looks exactly as
it did when the same block was the top-level destination, and moving an agent
onto approvals changes what can be done with a lead, not how it reads.

    "destination": {
      "type": "approval",
      "mode": "signal",
      "bot_token_env": "SOME_BOT_TOKEN",
      "channel_id": "C0123456789",
      "buttons": true,
      "max_proposals": 10,
      "wrap": {"type": "slack", "routes": [{"min_score": 60}], "footer": "..."},
      "actions": ["draft-application"]
    }

The wrapped block may leave out its transport: it never posts, so it inherits
this block's `bot_token_env` / `channel_id` / `webhook_env` rather than making
an operator name one it does not use.

At `flush()` the signals the gate accepts are ranked by score and the top
`max_proposals` (default 10) become proposals, one per configured action, each
with its own message. The rest are not dropped silently: one closing message
names how many were left out and lists their links. They are marked seen like
every other signal, so that message is their only record. A run with nothing
above the gate posts nothing at all, as a streaming lead agent does.

Each proposal's payload is the lead itself, for an action to template from:
`text`, `url`, `title` (the source's `meta.title`, else the first line of the
text), `score`, `reasons`, `meta`, and `rendered`, the message as posted. The
same announce-first, store-second rule holds across the whole run: every id is
reserved, every message posted, and only then is anything stored, in one
write.
"""
from __future__ import annotations

from pathlib import Path

import requests

from framework.action_page import TOKEN_FIELD, action_url, base_url_for, mint_token
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

MODE_RUN = "run"
MODE_SIGNAL = "signal"
_DEFAULT_MAX_PROPOSALS = 10
_TRANSPORT_KEYS = ("webhook_env", "bot_token_env", "channel_id")


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
        mode: str = MODE_RUN,
        max_proposals: int = _DEFAULT_MAX_PROPOSALS,
        post_label: str = "Post",
        draft_button: bool = True,
        workspace_name: str = "",
        action_base_url: str = "",
    ):
        needed = ("render",) if mode == MODE_RUN else ("accepts", "render_signal")
        missing = [name for name in needed if not callable(getattr(wrapped, name, None))]
        if missing:
            example = "'digest'" if mode == MODE_RUN else "'slack'"
            raise RuntimeError(
                f"approval destination: in mode {mode!r} the wrapped destination "
                f"{type(wrapped).__name__} needs {', '.join(n + '()' for n in missing)}; "
                f"wrap a destination that has it (e.g. {example})"
            )
        self._wrapped = wrapped
        # Looked up once and called through these, as `render` always was: the
        # Destination protocol declares none of the three.
        self._render = getattr(wrapped, "render", None)
        self._accepts = getattr(wrapped, "accepts", None)
        self._render_signal = getattr(wrapped, "render_signal", None)
        self._actions = actions
        self._store = store
        self._transport = transport
        self._timeout = timeout
        self._identity = identity or SlackIdentity()
        self._buttons = buttons
        self._mode = mode
        self._max_proposals = max_proposals
        self._post_label = post_label
        self._draft_button = draft_button
        self._workspace_name = workspace_name
        self._action_base_url = action_base_url
        self._signals: list[ScoredSignal] = []

    def send(self, scored: ScoredSignal) -> None:
        if self._mode == MODE_SIGNAL:
            # Held until flush(): the cap keeps the highest scores, which are
            # only known once every signal is in. The wrapped destination never
            # sees send(), so it cannot post.
            if self._accepts(scored):
                self._signals.append(scored)
            return
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
            "blocks": proposal_blocks(
                rendered,
                pid,
                action,
                self._post_label,
                self._draft_button,
                # Where the click is routed: one listener can serve several
                # workspaces on one app (issue #103).
                f"{self._workspace_name}/{pid}" if self._workspace_name else None,
            ),
        }
        body.update(self._identity.payload())
        # The `ts` is kept on the proposal (which `add_all` is about to store)
        # so that a verdict arriving from the action page can edit this card
        # instead of leaving live buttons on a closed lead. The webhook
        # transport returns "", and then there is nothing to edit and the link
        # path posts a separate outcome message instead.
        # Through the transport, not a raw requests.post. This call site was
        # written when the destination held a webhook URL, and posting the
        # payload straight at it was the same thing. It is not any more: the
        # transport may be a bot token plus a channel, and `post()` is what
        # knows the difference. Passing the object to `requests.post` instead
        # fails with `InvalidURL: Host '<SlackTransport object ...>'`, and only
        # ever on the buttons path, which is why nothing caught it until a
        # config turned buttons on.
        ts = self._transport.post(body)
        if ts:
            proposal["card_ts"] = str(ts)

    def _link(self, proposal: dict) -> str:
        """Mint this proposal's action link, or "" when none is configured.

        The token goes on the reserved proposal, which `add_all` writes, so a
        link that was announced is always one the page can resolve -- and a run
        that dies between announcing and storing leaves a link that resolves to
        nothing rather than to the wrong lead (issue #124).
        """
        retval = ""
        if not self._action_base_url or not self._workspace_name:
            return retval
        token = mint_token()
        proposal[TOKEN_FIELD] = token
        retval = action_url(self._action_base_url, self._workspace_name, token)
        return retval

    def _announce(self, title: str, body: str, reserved: list[dict]) -> None:
        """Post one rendered item with the instructions for its proposals."""
        if self._buttons:
            # One message per proposal: the buttons carry a single proposal id,
            # so two actions in one message would leave the second unclickable.
            for proposal in reserved:
                # Deliberately no action link on a card that has buttons. The
                # buttons do the same job in Slack and better, and the token is
                # a bearer capability: putting it in the message would store it
                # in Slack, in push notifications and in email digests, and
                # `_update` would re-post it verbatim when a button resolves
                # the proposal. A token still gets minted, because the link
                # goes out on the other surface.
                self._link(proposal)
                self._post_buttons(f"**{title}**\n\n{body}" if title else body, proposal)
            return
        lines = []
        for p in reserved:
            link = self._link(p)
            line = f'Proposal {p["id"]}: {p["action"]}. Reply "do {p["id"]}" or "skip {p["id"]}"'
            lines.append(f"{line}, or act on it here: {link}" if link else f"{line}.")
        # Instructions above the content rather than after it (see the note in
        # the module docstring).
        head = f"**{title}**\n\n" if title else ""
        self._post(head + "\n".join(lines) + "\n\n" + body)

    def _flush_signals(self) -> None:
        ranked = sorted(self._signals, key=lambda s: s.score, reverse=True)
        self._signals = []
        kept = ranked[: self._max_proposals]
        left_out = ranked[self._max_proposals :]
        reserved_all: list[dict] = []
        for scored in kept:
            rendered = self._render_signal(scored)
            if rendered is None:
                continue
            body = rendered["text"]
            if rendered.get("footer"):
                body += f"\n\n{rendered['footer']}"
            sig = scored.signal
            meta = sig.meta if isinstance(sig.meta, dict) else {}
            title = meta.get("title") or (sig.text.strip().splitlines() or [""])[0]
            payload = {
                "text": sig.text,
                "url": sig.url,
                "title": title,
                "score": scored.score,
                "reasons": list(scored.reasons),
                "meta": meta,
                "rendered": body,
            }
            reserved = [self._store.reserve(action, payload) for action in self._actions]
            self._announce("", body, reserved)
            reserved_all.extend(reserved)
        # Stored before the `left_out` notice, which is the one post here that
        # announces no proposal and so depends on nothing being stored. It used
        # to come first, and a webhook failure on a purely informational "3
        # more signals were not proposed" message discarded every proposal
        # already announced above it -- up to `max_proposals` cards and links,
        # all of them dead on arrival.
        if reserved_all:
            self._store.add_all(reserved_all)
        if left_out:
            links = "\n".join(f"• [{s.score}] {s.signal.url}" for s in left_out)
            self._post(
                f"{len(left_out)} more signal(s) passed the gate but were not proposed "
                f"this run (max_proposals is {self._max_proposals}):\n{links}"
            )

    def flush(self) -> None:
        if self._mode == MODE_SIGNAL:
            self._flush_signals()
            return
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
        # Announce first, store second.
        self._announce(title, body, reserved)
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

    mode = cfg.get("mode", MODE_RUN)
    if mode not in (MODE_RUN, MODE_SIGNAL):
        raise RuntimeError(
            f"approval destination: 'mode' must be {MODE_RUN!r} or {MODE_SIGNAL!r}, got {mode!r}"
        )
    max_proposals = cfg.get("max_proposals", _DEFAULT_MAX_PROPOSALS)
    if isinstance(max_proposals, bool) or not isinstance(max_proposals, int) or max_proposals < 1:
        raise RuntimeError(
            f"approval destination: 'max_proposals' must be a positive integer, got {max_proposals!r}"
        )
    if mode == MODE_SIGNAL and not any(
        key in wrap for key in ("webhook_env", "bot_token_env")
    ) and not any(
        key in route for route in wrap.get("routes") or [] if isinstance(route, dict)
        for key in ("webhook_env", "bot_token_env")
    ):
        # The wrapped destination never posts in signal mode, but building it
        # still builds its transports; lend it this block's.
        wrap.update({key: cfg[key] for key in _TRANSPORT_KEYS if key in cfg})

    retval = ApprovalDestination(
        wrapped=build_destination(wrap),
        actions=list(actions),
        store=ProposalStore(proposals_path(Path(workspace))),
        transport=transport,
        timeout=timeout,
        identity=identity_from_config(cfg, "approval destination"),
        buttons=bool(cfg.get("buttons", False)),
        mode=mode,
        max_proposals=max_proposals,
        post_label=str(cfg.get("post_label") or "Post"),
        draft_button=bool(cfg.get("draft_button", True)),
        workspace_name=Path(workspace).name,
        # Read from this workspace's own `approvals.action_page.base_url`
        # rather than from a key of this block: the listener reads it from
        # there, and two keys for one value is one of them going stale.
        action_base_url=base_url_for(workspace) or "",
    )
    return retval
