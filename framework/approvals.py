"""`agent approvals` — execute what a trusted human said `do` to (issue #21).

The second scheduled entrypoint. `agent run` ends by recording proposals (see
`framework/plugins/approval_destination.py`); this pass, on its own launchd
clock every few minutes, reads the approval channel's recent history, finds
replies from trusted users, and runs the matching proposals' action steps.
Run-and-exit, state on disk, safe to double-fire: a proposal is resolved under
the store's lock exactly once, and a second pass racing the first finds it no
longer pending.

Config, in the workspace's `config.json`:

    "approvals": {
      "channel_id": "C0123456789",
      "bot_token_env": "SOME_BOT_TOKEN",
      "trusted_user_ids": ["U0123456789"],
      "hours": 24
    },
    "actions": {
      "publish-post": {
        "steps": [
          {"type": "wordpress-publish", "mode": "publish"},
          {"type": "slack-post", "webhook_env": "SOME_WEBHOOK", "text": "Published: {url}"}
        ]
      }
    }

`channel_id` is the channel the proposals were posted to, read with the bot
token in `bot_token_env` (scopes: `channels:history` or `groups:history` for
a private channel, plus `chat:write` for the outcome message). `hours` bounds
how far back history is read (default 24). The webhook that posted the
proposal cannot read replies; a bot token can, which is why both exist.

What counts as a reply: a message whose text is exactly `do <id>` or
`skip <id>` (whitespace-trimmed, one id) from a user in `trusted_user_ids`.
Anything else is ignored, including "do all", a reaction, or a `do` from
anyone not on the list. No blanket approval exists. If a trusted user says
both `do p7` and `skip p7` before the pass runs, the later message wins,
which is the reading a human would give a change of mind.

Executing `do`: the action's steps run in order. Each step is built through
the registry from its own block, gets the proposal's payload merged with every
earlier step's returned dict, and its own returned dict is merged in for the
next. A step that raises stops the sequence: the proposal becomes `failed`
naming the step, later steps do not run, and nothing is retried. Retrying a
publish is not obviously safe (the first attempt may have half-succeeded), so
retry is a human decision made by reading the outcome message and the store.

Whatever happened, one outcome message goes to the channel through
`chat.postMessage`, and one summary line is printed, unconditionally, in the
shape the runner uses (issue #31): `agent=<name> approvals=N executed=N
skipped=N failed=N`.

Order inside a `do` matters: steps run, then the store is updated, then the
channel is told. The store first, because the steps' side effects are already
real and a crash between them and the status write would leave a `pending`
proposal that the next pass would execute again. If posting the outcome fails,
the status is already right and the error propagates.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from framework.config import ConfigError
from framework.proposals import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    ProposalStore,
    proposals_path,
)
from framework.registry import build_action

CONFIG_FILENAME = "config.json"
_SLACK_API = "https://slack.com/api"
_REPLY_RE = re.compile(r"^(do|skip)\s+(\S+)$")
_DEFAULT_HOURS = 24


class ApprovalsConfig:
    def __init__(
        self,
        agent: str,
        workspace: Path,
        channel_id: str,
        bot_token: str,
        trusted_user_ids: list[str],
        hours: float,
        actions: dict[str, Any],
        timeout: int = 30,
    ):
        self.agent = agent
        self.workspace = workspace
        self.channel_id = channel_id
        self.bot_token = bot_token
        self.trusted_user_ids = trusted_user_ids
        self.hours = hours
        self.actions = actions
        self.timeout = timeout


def load_approvals_config(workspace: str | Path) -> ApprovalsConfig:
    """Read the `approvals` and `actions` blocks of `<workspace>/config.json`.

    Deliberately reads only what this pass needs. The source/scorer/destination
    blocks are `agent run`'s business (issue #43 owns the full loader), and
    this pass must not fail because, say, a source plugin's dependency is
    missing on the machine that only executes approvals.
    """
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ConfigError(f"workspace directory not found: {workspace_path}")
    config_path = workspace_path / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{config_path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be an object")

    agent = raw.get("agent")
    if not agent:
        raise ConfigError(f"{config_path}: 'agent' (name) is required")

    block = raw.get("approvals")
    if not isinstance(block, dict):
        raise ConfigError(f"{config_path}: 'approvals' must be an object")
    channel_id = block.get("channel_id")
    if not channel_id:
        raise ConfigError(f"{config_path}: 'approvals.channel_id' is required")
    token_env = block.get("bot_token_env")
    if not token_env:
        raise ConfigError(f"{config_path}: 'approvals.bot_token_env' is required")
    bot_token = os.environ.get(token_env)
    if not bot_token:
        raise ConfigError(f"approvals need the Slack bot token in ${token_env}")
    trusted = block.get("trusted_user_ids")
    if not isinstance(trusted, list) or not trusted or not all(isinstance(u, str) for u in trusted):
        raise ConfigError(
            f"{config_path}: 'approvals.trusted_user_ids' must be a non-empty list of user ids"
        )

    actions = raw.get("actions")
    if not isinstance(actions, dict):
        raise ConfigError(f"{config_path}: 'actions' must be an object of named actions")
    for name, action in actions.items():
        steps = action.get("steps") if isinstance(action, dict) else None
        if not isinstance(steps, list) or not steps:
            raise ConfigError(f"{config_path}: 'actions.{name}.steps' must be a non-empty list")
        for i, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or not step.get("type"):
                raise ConfigError(
                    f"{config_path}: 'actions.{name}.steps[{i}]' must be an object with a 'type'"
                )

    retval = ApprovalsConfig(
        agent=str(agent),
        workspace=workspace_path,
        channel_id=str(channel_id),
        bot_token=bot_token,
        trusted_user_ids=[str(u) for u in trusted],
        hours=float(block.get("hours", _DEFAULT_HOURS)),
        actions=actions,
        timeout=int(block.get("timeout", 30)),
    )
    return retval


class SlackApiError(RuntimeError):
    """Slack answered, but with `ok: false`."""


def _slack_call(method: str, token: str, timeout: int, **params: Any) -> dict[str, Any]:
    if method == "chat.postMessage":
        resp = requests.post(
            f"{_SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=params,
            timeout=timeout,
        )
    else:
        resp = requests.get(
            f"{_SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=timeout,
        )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or not body.get("ok"):
        error = body.get("error") if isinstance(body, dict) else "unreadable response"
        raise SlackApiError(f"{method}: {error}")
    retval = body
    return retval


def read_replies(cfg: ApprovalsConfig) -> dict[str, dict[str, str]]:
    """Map proposal id -> {"verb", "user", "ts"} from trusted replies.

    Walks `conversations.history` over the last `hours`, oldest first, so a
    later reply about the same id overrides an earlier one.
    """
    oldest = f"{time.time() - cfg.hours * 3600:.6f}"
    messages: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"channel": cfg.channel_id, "oldest": oldest, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        body = _slack_call("conversations.history", cfg.bot_token, cfg.timeout, **params)
        messages.extend(m for m in body.get("messages", []) if isinstance(m, dict))
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not body.get("has_more") or not cursor:
            break

    retval: dict[str, dict[str, str]] = {}
    for message in sorted(messages, key=lambda m: float(m.get("ts", 0))):
        user = message.get("user")
        if user not in cfg.trusted_user_ids:
            continue
        match = _REPLY_RE.match(str(message.get("text", "")).strip())
        if not match:
            continue
        retval[match.group(2)] = {
            "verb": match.group(1),
            "user": str(user),
            "ts": str(message.get("ts", "")),
        }
    return retval


def _run_steps(
    cfg: ApprovalsConfig, proposal: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the proposal's action steps in order; return (results, error).

    `error` is None when every step ran, else a message naming the step that
    stopped the sequence. `results` holds one entry per step attempted.
    """
    name = proposal["action"]
    action = cfg.actions.get(name)
    results: list[dict[str, Any]] = []
    if action is None:
        known = ", ".join(sorted(cfg.actions)) or "(none)"
        retval = (results, f"action {name!r} is not in this workspace's config; known: {known}")
        return retval

    merged: dict[str, Any] = dict(proposal.get("payload") or {})
    context = {
        "workspace": str(cfg.workspace),
        "agent": cfg.agent,
        "proposal_id": proposal["id"],
        "action": name,
    }
    for i, step_cfg in enumerate(action["steps"], start=1):
        step_type = step_cfg.get("type")
        try:
            step = build_action(step_cfg)
            result = step.execute(merged, context)
        except Exception as exc:
            results.append({"step": i, "type": step_type, "status": "failed", "error": str(exc)})
            retval = (results, f"step {i} ({step_type}) failed: {exc}")
            return retval
        if not isinstance(result, dict):
            results.append(
                {"step": i, "type": step_type, "status": "failed", "error": "returned non-dict"}
            )
            retval = (results, f"step {i} ({step_type}) returned {type(result).__name__}, not a dict")
            return retval
        merged.update(result)
        results.append({"step": i, "type": step_type, "status": "ok", "result": result})
    retval = (results, None)
    return retval


def _outcome_text(proposal: dict[str, Any], status: str, results: list, error: str | None) -> str:
    pid = proposal["id"]
    action = proposal["action"]
    if status == STATUS_SKIPPED:
        retval = f"Proposal {pid} ({action}): skipped."
        return retval
    steps = ", ".join(f"{r['type']} {r['status']}" for r in results) or "no steps run"
    if status == STATUS_DONE:
        retval = f"Proposal {pid} ({action}): done. Steps: {steps}."
        return retval
    retval = f":x: Proposal {pid} ({action}): failed. {error} Steps: {steps}."
    return retval


def run_approvals(cfg: ApprovalsConfig) -> int:
    """One pass: read replies, resolve every pending proposal that has one."""
    store = ProposalStore(proposals_path(cfg.workspace))
    pending = 0
    executed = 0
    skipped = 0
    failed = 0
    status = "failed"
    try:
        proposals = store.pending()
        pending = len(proposals)
        replies = read_replies(cfg) if proposals else {}

        for proposal in proposals:
            reply = replies.get(proposal["id"])
            if reply is None:
                continue
            decided = {"decided_by": reply["user"], "decided_ts": reply["ts"]}
            if reply["verb"] == "skip":
                store.resolve(proposal["id"], STATUS_SKIPPED, **decided)
                skipped += 1
                text = _outcome_text(proposal, STATUS_SKIPPED, [], None)
            else:
                results, error = _run_steps(cfg, proposal)
                if error is None:
                    store.resolve(proposal["id"], STATUS_DONE, results=results, **decided)
                    executed += 1
                    text = _outcome_text(proposal, STATUS_DONE, results, None)
                else:
                    store.resolve(
                        proposal["id"], STATUS_FAILED, results=results, error=error, **decided
                    )
                    failed += 1
                    text = _outcome_text(proposal, STATUS_FAILED, results, error)
            _slack_call(
                "chat.postMessage", cfg.bot_token, cfg.timeout, channel=cfg.channel_id, text=text
            )
        status = "ok"
    finally:
        print(
            f"agent={cfg.agent} status={status} approvals={pending} "
            f"executed={executed} skipped={skipped} failed={failed}"
        )
    retval = 0
    return retval
