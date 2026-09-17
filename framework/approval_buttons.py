"""Approval buttons over Socket Mode (issue #74).

Approving by reply works, but the case that matters most -- a drafted blog
post -- wants one click and a third option the reply grammar has no room for:
publish it, park it in the editor as a draft, or throw it away. So a proposal
is posted as Block Kit with three buttons, and this module is what happens
when one is clicked.

    Post     run the action as configured
    Draft    the same sequence, with the WordPress step forced to `mode: draft`
             and everything after it skipped, so nothing is announced
    Discard  mark the proposal skipped; no step runs

Why Socket Mode. A button needs Slack to deliver the interaction somewhere,
and the alternative is a public HTTPS endpoint for a fleet of agents that runs
on one laptop. Socket Mode dials out instead, so `agent approvals --listen` is
a long-running client holding that connection, installed by `install-schedule`
with `KeepAlive` (framework/schedule.py) rather than an interval. Config:

    "approvals": {
      "channel_id": "C0123456789",
      "bot_token_env": "SOME_BOT_TOKEN",
      "app_token_env": "SOME_APP_TOKEN",
      "trusted_user_ids": ["U0123456789"],
      "schedule": "every 5m"
    }

`app_token_env` names the env var holding the app-level token (`xapp-...`,
scope `connections:write`); it is read only by `--listen`. Both tokens are read
from the environment by the name a config key gives, never written down here.

The polling pass is not replaced by this. A laptop asleep when someone clicks
gets the interaction on reconnect, but a click that is never delivered at all
leaves the proposal pending, and a `do <id>` reply still resolves it. Both
paths go through the same compare-and-set claim (`pending` -> `running` under
the store's lock, framework/proposals.py), so a click and a reply racing each
other cannot both run the steps: the loser is told the proposal is already
spoken for and does nothing.

Two things a reader should not be surprised by:

  - **A proposal resolved by reply keeps its buttons.** The proposal is posted
    through an incoming webhook, which returns no message `ts`, so the polling
    pass has no handle to edit. Clicking a button afterwards is safe -- the
    claim fails, nothing runs, and the clicker gets an ephemeral line saying
    what the proposal already is. A click is what supplies the `ts` that lets
    *this* path edit the message in place.
  - **Draft is refused for an action with no `wordpress-publish` step.** There
    is nothing to draft, and running the sequence anyway would publish exactly
    what the operator was trying not to publish. The proposal stays pending,
    so Post or Discard still work.

Nothing here logs a token, a payload or a message body: the stdout line per
interaction is the proposal id, the verdict and the user id, which is what an
operator needs to see in `approvals.listen.log`.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

from framework.approvals import (
    ApprovalsConfig,
    _now_iso,
    _run_steps,
    _slack_call,
    has_draftable_step,
    human_time,
    steps_detail,
)
from framework.proposals import (
    ProposalStore,
    proposals_path,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)

ACTION_POST = "approval:post"
ACTION_DRAFT = "approval:draft"
ACTION_DISCARD = "approval:discard"

VARIANT_POST = "post"
VARIANT_DRAFT = "draft"

_ACTION_VARIANTS = {
    ACTION_POST: VARIANT_POST,
    ACTION_DRAFT: VARIANT_DRAFT,
}

# Slack rejects a section whose text runs past 3000 characters, and a digest
# body md can be longer than that. Split on line boundaries instead, well
# under the limit, rather than truncating content a human is being asked to
# approve.
_SECTION_LIMIT = 2800


class ListenerError(RuntimeError):
    """The listener cannot start: missing dependency, token or config key."""


def _sections(text: str) -> list[dict[str, Any]]:
    """`text` as one or more mrkdwn section blocks, split on line boundaries."""
    blocks: list[dict[str, Any]] = []
    chunk = ""
    for line in text.split("\n"):
        # A single line longer than the limit is split mid-line; there is no
        # boundary left to prefer.
        while len(line) > _SECTION_LIMIT:
            if chunk:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
                chunk = ""
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": line[:_SECTION_LIMIT]}}
            )
            line = line[_SECTION_LIMIT:]
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > _SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
            chunk = line
        else:
            chunk = candidate
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    retval = blocks
    return retval


def proposal_blocks(text: str, proposal_id: str, action: str) -> list[dict[str, Any]]:
    """The proposal message: the rendered content, then the three buttons.

    `text` is exactly what the webhook would have posted without buttons, so
    the content a human reads is the same either way and this stays out of the
    way of whatever builds it (issue #73 owns the Markdown-to-mrkdwn pass).
    """
    blocks = _sections(text)
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Proposal `{proposal_id}` · {action} · "
                    f'or reply `do {proposal_id}` / `skip {proposal_id}`',
                }
            ],
        }
    )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"approval:{proposal_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_POST,
                    "text": {"type": "plain_text", "text": "Post"},
                    "style": "primary",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DRAFT,
                    "text": {"type": "plain_text", "text": "Draft"},
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DISCARD,
                    "text": {"type": "plain_text", "text": "Discard"},
                    "style": "danger",
                    "value": proposal_id,
                },
            ],
        }
    )
    retval = blocks
    return retval


def outcome_blocks(text: str, outcome: str) -> list[dict[str, Any]]:
    """The same content with the buttons replaced by what happened."""
    blocks = _sections(text)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]})
    retval = blocks
    return retval


def _content_text(message: dict[str, Any]) -> str:
    """The content part of the clicked message, without its context/buttons.

    Read back from the message Slack hands us rather than from the store: the
    proposal records the payload, not the rendered text, and the point of
    editing in place is that the content a human approved stays exactly as it
    was.
    """
    parts = []
    for block in message.get("blocks") or []:
        if block.get("type") != "section":
            continue
        text = (block.get("text") or {}).get("text")
        if text:
            parts.append(text)
    retval = "\n".join(parts) if parts else str(message.get("text") or "")
    return retval


def outcome_line(status: str, variant: str, user_id: str, when: str, detail: str) -> str:
    """The context line the message is edited down to after a click."""
    who = f"<@{user_id}>"
    if status == STATUS_SKIPPED:
        retval = f":wastebasket: Discarded by {who} at {when}."
        return retval
    if status == STATUS_DONE:
        verb = "Drafted" if variant == VARIANT_DRAFT else "Posted"
        retval = f":white_check_mark: {verb} by {who} at {when}. {detail}"
        return retval
    retval = f":x: Failed after {who} clicked at {when}. {detail}"
    return retval


class SlackResponder:
    """The two ways this module talks back: edit the message, or one user only.

    A thin seam rather than the full web client: the listener is verified
    offline with a fake in its place, and everything it needs is two calls.
    """

    def __init__(self, bot_token: str, timeout: int = 30):
        self._bot_token = bot_token
        self._timeout = timeout

    def update(self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]) -> None:
        _slack_call(
            "chat.update",
            self._bot_token,
            self._timeout,
            channel=channel,
            ts=ts,
            text=text,
            blocks=blocks,
        )

    def ephemeral(self, response_url: str, text: str) -> None:
        """Say something to the clicker alone, visible to nobody else.

        `response_url` rather than `chat.postEphemeral`: it is what the
        interaction hands us, it needs no extra scope, and an approval refusal
        should not be an audit-visible message in the channel.
        """
        resp = requests.post(
            response_url,
            json={"response_type": "ephemeral", "replace_original": False, "text": text},
            timeout=self._timeout,
        )
        resp.raise_for_status()


def handle_block_action(
    cfg: ApprovalsConfig, store: ProposalStore, payload: dict[str, Any], responder: Any
) -> str:
    """Act on one button click; return the one-line summary for the log.

    Ordering is the reply path's: claim first, then steps, then the terminal
    status, then the message. Every exit that does not claim leaves the
    proposal exactly as it was, so the reply path can still resolve it.
    """
    actions = payload.get("actions") or []
    if not actions:
        retval = "ignored: interaction with no actions"
        return retval
    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in (ACTION_POST, ACTION_DRAFT, ACTION_DISCARD):
        retval = f"ignored: unknown action_id {action_id!r}"
        return retval

    proposal_id = str(action.get("value") or "")
    user_id = str((payload.get("user") or {}).get("id") or "")
    response_url = str(payload.get("response_url") or "")
    channel = str((payload.get("channel") or {}).get("id") or "")
    message = payload.get("message") or {}
    ts = str(message.get("ts") or (payload.get("container") or {}).get("message_ts") or "")
    clicked_at = str(action.get("action_ts") or "")

    if user_id not in cfg.trusted_user_ids:
        responder.ephemeral(
            response_url,
            f"Only an approver listed for this agent can act on proposal {proposal_id}. "
            "Nothing ran.",
        )
        retval = f"proposal={proposal_id} verdict=refused user={user_id} (not trusted)"
        return retval

    proposal = store.get(proposal_id)
    if proposal is None:
        responder.ephemeral(
            response_url, f"Proposal {proposal_id} is not in this agent's store. Nothing ran."
        )
        retval = f"proposal={proposal_id} verdict=unknown user={user_id}"
        return retval

    decided = {
        "decided_by": user_id,
        "decided_ts": clicked_at,
        "decided_via": "button",
    }
    when = human_time(clicked_at)

    if action_id == ACTION_DISCARD:
        if store.transition(proposal_id, STATUS_PENDING, STATUS_SKIPPED, **decided) is None:
            retval = _already(store, responder, response_url, proposal_id, user_id)
            return retval
        line = outcome_line(STATUS_SKIPPED, VARIANT_POST, user_id, when, "")
        _update(responder, channel, ts, message, line)
        retval = f"proposal={proposal_id} verdict=discard user={user_id}"
        return retval

    variant = _ACTION_VARIANTS[action_id]
    if variant == VARIANT_DRAFT and not has_draftable_step(cfg, proposal["action"]):
        responder.ephemeral(
            response_url,
            f"Proposal {proposal_id} runs '{proposal['action']}', which has no "
            "wordpress-publish step, so there is nothing to draft. It is still pending: "
            "Post runs it as configured, Discard throws it away.",
        )
        retval = f"proposal={proposal_id} verdict=draft-refused user={user_id} (no draftable step)"
        return retval

    claimed = store.transition(
        proposal_id,
        STATUS_PENDING,
        STATUS_RUNNING,
        claimed_at=_now_iso(),
        claimed_pid=os.getpid(),
        variant=variant,
        **decided,
    )
    if claimed is None:
        retval = _already(store, responder, response_url, proposal_id, user_id)
        return retval

    results, error = _run_steps(cfg, store, proposal, variant=variant)
    if error is None:
        store.transition(proposal_id, STATUS_RUNNING, STATUS_DONE, results=results)
        status = STATUS_DONE
        detail = steps_detail(results)
    else:
        store.transition(proposal_id, STATUS_RUNNING, STATUS_FAILED, results=results, error=error)
        status = STATUS_FAILED
        detail = f"{error} Steps: {steps_detail(results)}"
    _update(responder, channel, ts, message, outcome_line(status, variant, user_id, when, detail))
    retval = f"proposal={proposal_id} verdict={variant} status={status} user={user_id}"
    return retval


def _already(
    store: ProposalStore, responder: Any, response_url: str, proposal_id: str, user_id: str
) -> str:
    """Someone (or the polling pass) got there first; say so, run nothing."""
    current = store.get(proposal_id) or {}
    status = current.get("status", "gone")
    responder.ephemeral(
        response_url,
        f"Proposal {proposal_id} is already {status}"
        + (f" (by <@{current['decided_by']}>)" if current.get("decided_by") else "")
        + ". Nothing ran.",
    )
    retval = f"proposal={proposal_id} verdict=already-{status} user={user_id}"
    return retval


def _update(
    responder: Any, channel: str, ts: str, message: dict[str, Any], line: str
) -> None:
    """Edit the clicked message in place, buttons replaced by the outcome."""
    if not (channel and ts):
        print(
            "agent: interaction carried no channel/ts; the message keeps its buttons "
            "(clicking again is refused, the proposal is already resolved)",
            file=sys.stderr,
        )
        return
    content = _content_text(message)
    responder.update(channel, ts, f"{content}\n\n{line}", outcome_blocks(content, line))


def run_listener(cfg: ApprovalsConfig) -> int:
    """Hold a Socket Mode connection and act on clicks until killed.

    The only long-running entrypoint in the framework. It owns no clock and no
    state of its own: every decision goes through the same store and the same
    claim as the polling pass, so stopping it loses nothing but the buttons.
    """
    if not cfg.app_token:
        raise ListenerError(
            "approvals --listen needs an app-level token: set 'approvals.app_token_env' "
            "in the workspace config to the name of the variable holding it (xapp-...)"
        )
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as exc:
        raise ListenerError(
            "approvals --listen needs the Socket Mode client: "
            "pip install '.[approvals-buttons]' (slack_sdk, websocket-client)"
        ) from exc

    store = ProposalStore(proposals_path(cfg.workspace))
    responder = SlackResponder(cfg.bot_token, cfg.timeout)
    client = SocketModeClient(app_token=cfg.app_token)

    def on_request(socket_client, request: Any) -> None:
        # Ack first, always: Slack retries an un-acked interaction, and a retry
        # of a click whose steps are already running is exactly the double
        # execution the claim exists to prevent. The claim would refuse it, but
        # the operator should not have to read about it either.
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type != "interactive":
            return
        payload = request.payload or {}
        if payload.get("type") != "block_actions":
            return
        try:
            summary = handle_block_action(cfg, store, payload, responder)
        except Exception as exc:
            # One bad interaction must not take the listener down: the next
            # click, and the polling pass, still work. The proposal is left
            # wherever the claim put it, which `stuck=N` reports.
            print(f"agent: interaction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        print(f"agent={cfg.agent} {summary} at={_now_iso()}", flush=True)

    client.socket_mode_request_listeners.append(on_request)
    print(f"agent={cfg.agent} listening for approval clicks (socket mode)", flush=True)
    client.connect()
    try:
        _block_forever()
    except KeyboardInterrupt:
        print(f"agent={cfg.agent} listener stopped", flush=True)
    finally:
        client.close()
    retval = 0
    return retval


def _block_forever() -> None:
    """Sleep until killed. Separate so a test can drive `run_listener` without it."""
    from threading import Event

    Event().wait()
