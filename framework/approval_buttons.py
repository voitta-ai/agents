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

That token must belong to an app nothing else connects with. Slack spreads an
app's events across all of its Socket Mode connections, and `on_request` acks
every envelope but keeps only clicks. Pointed at a chat bot's app, this
listener silently swallows a share of that bot's mentions. See README, "Which
Slack app the listener connects as".

The polling pass is not replaced by this. A laptop asleep when someone clicks
gets the interaction on reconnect, but a click that is never delivered at all
leaves the proposal pending, and a `do <id>` reply still resolves it. Both
paths go through the same compare-and-set claim (`pending` -> `running` under
the store's lock, framework/proposals.py), so a click and a reply racing each
other cannot both run the steps: the loser is told the proposal is already
spoken for and does nothing.

Three things a reader should not be surprised by:

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
  - **Changing `channel_id` strands the proposals already posted.** A proposal
    records no channel of its own, so a click is checked against whatever
    `channel_id` says now. Move an agent to a new approval channel with
    proposals still pending in the old one and their buttons refuse as "wrong
    channel", visibly only to whoever clicked. The reply path has the same
    shape -- it only reads the configured channel's history -- so the fix is
    the same either way: resolve what is pending before moving the agent, or
    edit `state/proposals.json` and let them be re-proposed.

Nothing here logs a token, a payload, a message body or a `response_url`: the
stdout line per interaction is the proposal id, the verdict and the user id,
which is what an operator needs to see in `approvals.listen.log`. A
`response_url` is a bearer capability (30 minutes, five uses), and the
`requests` exception for a failed post carries the URL it posted to, so both
ways of talking back sanitise their failures into `ResponderError` before
anything can print them.

A click is acted on only if it came from the configured `channel_id` and from
a user in `trusted_user_ids`; either check failing is an ephemeral refusal and
nothing else.

One listener serves every workspace that posts cards through its app (issue
#103): the listening workspace names the others in `approvals.listen_for`, each
button's value is `<workspace>/<proposal id>`, and `route_block_action` hands
the click to that workspace's store and config, where the channel and trust
checks above are that workspace's own. Slack would split clicks between two
connections on one app, so a second listener is never the answer.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

from framework.approvals import (
    ApprovalsConfig,
    load_approvals_config,
    _now_iso,
    _run_steps,
    _slack_call,
    has_draftable_step,
    human_time,
    steps_detail,
)
from framework.config import ConfigError
from framework.proposals import (
    ProposalStore,
    proposals_path,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)
from framework.slack import SLACK_BLOCK_LIMIT, split_for_slack

ACTION_POST = "approval:post"
ACTION_DRAFT = "approval:draft"
ACTION_DISCARD = "approval:discard"

VARIANT_POST = "post"
VARIANT_DRAFT = "draft"

_ACTION_VARIANTS = {
    ACTION_POST: VARIANT_POST,
    ACTION_DRAFT: VARIANT_DRAFT,
}



class ListenerError(RuntimeError):
    """The listener cannot start: missing dependency, token or config key."""


def _sections(text: str) -> list[dict[str, Any]]:
    """`text` as one or more mrkdwn section blocks (issue #85).

    `split_for_slack` is the repo's one splitting rule, shared with the webhook
    path: paragraph boundaries first, then lines, and inside a line only when
    one line is itself longer than the limit -- and there, on a grapheme
    boundary, so a family emoji or a flag pair is never cut in half. This used
    to split by code-point index at an arbitrary 2800, which could leave a
    stray joiner at the head of the next block, and disagreed with the other
    path about where a long line may be cut. Both now stop at
    `SLACK_BLOCK_LIMIT`, which is Slack's actual limit for a section's text.
    """
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": part}}
        for part in split_for_slack(text, SLACK_BLOCK_LIMIT)
    ]
    retval = blocks
    return retval


def proposal_blocks(
    text: str,
    proposal_id: str,
    action: str,
    post_label: str = "Post",
    draft_button: bool = True,
    ref: str | None = None,
) -> list[dict[str, Any]]:
    """The proposal message: the rendered content, then the buttons.

    `text` is exactly what the webhook would have posted without buttons, so
    the content a human reads is the same either way and this stays out of the
    way of whatever builds it (issue #73 owns the Markdown-to-mrkdwn pass).

    `post_label` renames the first button, which runs the action as
    configured: "Post" is wrong for an action that drafts something for a
    human to send (issue #76). `draft_button: false` leaves out Draft, which
    only means anything for an action with a `wordpress-publish` step and is
    refused for any other.

    `ref` is what each button carries back to the listener, normally
    `<workspace>/<proposal id>`, so one listener can serve every workspace that
    posts through the same app (issue #103). Without it the bare id is used,
    which is what every card posted before #103 carries.
    """
    value = ref or proposal_id
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
                    "text": {"type": "plain_text", "text": post_label},
                    "style": "primary",
                    "value": value,
                },
                *(
                    [
                        {
                            "type": "button",
                            "action_id": ACTION_DRAFT,
                            "text": {"type": "plain_text", "text": "Draft"},
                            "value": value,
                        }
                    ]
                    if draft_button
                    else []
                ),
                {
                    "type": "button",
                    "action_id": ACTION_DISCARD,
                    "text": {"type": "plain_text", "text": "Discard"},
                    "style": "danger",
                    "value": value,
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
        # "Approved", not "Posted": the first button runs whatever the action
        # is, and for one that drafts something for a human to send, "Posted"
        # would claim the very thing that did not happen.
        verb = "Drafted" if variant == VARIANT_DRAFT else "Approved"
        retval = f":white_check_mark: {verb} by {who} at {when}. {detail}"
        return retval
    retval = f":x: Failed after {who} clicked at {when}. {detail}"
    return retval


class ResponderError(RuntimeError):
    """Talking back to Slack failed. Carries no URL, no token, no body.

    Raised in place of the underlying `requests` exception, whose message
    embeds the request URL. For `response_url` that URL *is* the credential --
    a bearer capability good for 30 minutes and five uses -- and its ordinary
    failure modes (expired, used up, a Slack-side blip) would otherwise write
    it into `approvals.listen.error.log`, which the listener's catch-all
    prints to. Losing the URL from the message costs nothing: there is exactly
    one response_url per interaction, and the proposal id in the same line
    says which.
    """


def _sanitised(what: str, exc: Exception) -> ResponderError:
    """`exc` without whatever URL it was carrying."""
    detail = getattr(getattr(exc, "response", None), "status_code", None)
    if detail is not None:
        retval = ResponderError(f"{what} failed: HTTP {detail}")
        return retval
    retval = ResponderError(f"{what} failed: {type(exc).__name__}")
    return retval


class SlackResponder:
    """The two ways this module talks back: edit the message, or one user only.

    A thin seam rather than the full web client: the listener is verified
    offline with a fake in its place, and everything it needs is two calls.
    Both sanitise their failures (see `ResponderError`).
    """

    def __init__(self, bot_token: str, timeout: int = 30):
        self._bot_token = bot_token
        self._timeout = timeout

    def update(self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]) -> None:
        try:
            _slack_call(
                "chat.update",
                self._bot_token,
                self._timeout,
                channel=channel,
                ts=ts,
                text=text,
                blocks=blocks,
            )
        except Exception as exc:
            # The API URL is not secret, but the token is in the headers and a
            # transport error can quote a redirect target; one rule for both
            # calls is easier to keep true than two.
            raise _sanitised("chat.update", exc) from None

    def ephemeral(self, response_url: str, text: str) -> None:
        """Say something to the clicker alone, visible to nobody else.

        `response_url` rather than `chat.postEphemeral`: it is what the
        interaction hands us, it needs no extra scope, and an approval refusal
        should not be an audit-visible message in the channel.
        """
        try:
            resp = requests.post(
                response_url,
                json={"response_type": "ephemeral", "replace_original": False, "text": text},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise _sanitised("ephemeral reply", exc) from None


def split_ref(value: str) -> tuple[str | None, str]:
    """A button value as (workspace name or None, proposal id).

    `<workspace>/<id>` since issue #103; a bare id on every card posted before
    it. A workspace name is a directory name, so it cannot contain the `/`.
    """
    if "/" in value:
        workspace, proposal_id = value.rsplit("/", 1)
        retval = (workspace, proposal_id)
        return retval
    retval = (None, value)
    return retval


class Route:
    """One workspace a listener serves: its config, its store, its responder."""

    def __init__(self, cfg: ApprovalsConfig):
        self.cfg = cfg
        self.store = ProposalStore(proposals_path(cfg.workspace))
        self.responder = SlackResponder(cfg.bot_token, cfg.timeout)


def route_block_action(routes: dict[str, Route], payload: dict[str, Any]) -> tuple[str, str]:
    """Send one click to the workspace that owns its proposal (issue #103).

    Returns (agent name for the log line, summary). A `<workspace>/<id>` value
    goes to that workspace, or is refused if this listener does not serve it.
    A bare id, from a card posted before values carried the workspace, goes to
    the one store that holds it; held by none or by several, it is refused and
    the `do <id>` reply path is named, which still works.
    """
    any_route = next(iter(routes.values()))
    actions = payload.get("actions") or []
    if not actions or actions[0].get("action_id") not in (ACTION_POST, ACTION_DRAFT, ACTION_DISCARD):
        # Nothing to route; the handler says why it is ignored.
        retval = (any_route.cfg.agent, handle_block_action(
            any_route.cfg, any_route.store, payload, any_route.responder
        ))
        return retval
    workspace, proposal_id = split_ref(str(actions[0].get("value") or ""))
    response_url = str(payload.get("response_url") or "")
    user_id = str((payload.get("user") or {}).get("id") or "")
    if workspace is not None:
        route = routes.get(workspace)
        if route is None:
            any_route.responder.ephemeral(
                response_url,
                f"Proposal {proposal_id} belongs to '{workspace}', which this listener "
                f"does not serve (add it to 'approvals.listen_for'). Nothing ran; "
                f"reply \"do {proposal_id}\" in its approval channel instead.",
            )
            retval = (any_route.cfg.agent,
                      f"proposal={proposal_id} verdict=refused user={user_id} "
                      f"(workspace {workspace!r} not served)")
            return retval
    else:
        holders = [r for r in routes.values() if r.store.get(proposal_id) is not None]
        if len(holders) == 1 or len(routes) == 1:
            route = holders[0] if holders else any_route
        else:
            where = (
                f"is in {len(holders)} of this listener's workspaces, so the click "
                "cannot tell which"
                if holders
                else "is in none of this listener's workspaces"
            )
            any_route.responder.ephemeral(
                response_url,
                f"Proposal {proposal_id} {where}. Nothing ran; reply "
                f"\"do {proposal_id}\" in the approval channel instead.",
            )
            retval = (any_route.cfg.agent,
                      f"proposal={proposal_id} verdict=refused user={user_id} "
                      f"(bare id in {len(holders)} workspaces)")
            return retval
    retval = (route.cfg.agent, handle_block_action(
        route.cfg, route.store, payload, route.responder, proposal_id=proposal_id
    ))
    return retval


def handle_block_action(
    cfg: ApprovalsConfig,
    store: ProposalStore,
    payload: dict[str, Any],
    responder: Any,
    proposal_id: str | None = None,
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

    if proposal_id is None:
        proposal_id = split_ref(str(action.get("value") or ""))[1]
    user_id = str((payload.get("user") or {}).get("id") or "")
    response_url = str(payload.get("response_url") or "")
    channel = str((payload.get("channel") or {}).get("id") or "")
    message = payload.get("message") or {}
    ts = str(message.get("ts") or (payload.get("container") or {}).get("message_ts") or "")
    clicked_at = str(action.get("action_ts") or "")

    # The reply path is scoped to one channel by construction: it only ever
    # reads `conversations.history` for `channel_id`. A click arrives from
    # wherever the message was, so the same scope is checked here rather than
    # assumed. In a one-app-per-workspace deployment nothing else can carry a
    # matching action_id, but that is a property of the deployment, not of
    # this code.
    if channel != cfg.channel_id:
        responder.ephemeral(
            response_url,
            f"Proposal {proposal_id} is approved in this agent's own approval channel, "
            "not here. Nothing ran.",
        )
        retval = f"proposal={proposal_id} verdict=refused user={user_id} (wrong channel)"
        return retval

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

    routes = listener_routes(cfg)
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
            agent, summary = route_block_action(routes, payload)
        except Exception as exc:
            # One bad interaction must not take the listener down: the next
            # click, and the polling pass, still work. The proposal is left
            # wherever the claim put it, which `stuck=N` reports.
            print(f"agent: interaction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        print(f"agent={agent} {summary} at={_now_iso()}", flush=True)

    client.socket_mode_request_listeners.append(on_request)
    served = ", ".join(sorted(routes))
    print(f"agent={cfg.agent} listening for approval clicks (socket mode) for {served}", flush=True)
    try:
        # Inside the try: a rejected app token fails the handshake here, and
        # launchd restarts this job (KeepAlive), so the socket has to be closed
        # on the way out or every restart leaks one.
        client.connect()
        _block_forever()
    except KeyboardInterrupt:
        print(f"agent={cfg.agent} listener stopped", flush=True)
    finally:
        client.close()
    retval = 0
    return retval


def listener_routes(cfg: ApprovalsConfig) -> dict[str, Route]:
    """This workspace plus every one it names in `approvals.listen_for`.

    Keyed by workspace directory name, which is what a card's button value
    carries. Each served workspace keeps its own channel, trusted users, bot
    token and actions; only the app-level token, and so the one connection, is
    shared. A served workspace that does not load stops the listener at start,
    rather than refusing its clicks one at a time later.
    """
    routes = {cfg.workspace.name: Route(cfg)}
    for path in cfg.listen_for:
        try:
            other = load_approvals_config(path)
        except ConfigError as exc:
            raise ListenerError(f"approvals.listen_for: {path.name}: {exc}") from exc
        if path.name in routes:
            raise ListenerError(f"approvals.listen_for: {path.name} is listed twice")
        routes[path.name] = Route(other)
    retval = routes
    return retval


def _block_forever() -> None:
    """Sleep until killed. Separate so a test can drive `run_listener` without it."""
    from threading import Event

    Event().wait()
