"""`agent approvals` — execute what a trusted human said `do` to (issue #21).

The second scheduled entrypoint. `agent run` ends by recording proposals (see
`framework/plugins/approval_destination.py`); this pass, on its own launchd
clock every few minutes, reads the approval channel's recent history, finds
replies from trusted users, and runs the matching proposals' action steps.
Run-and-exit, state on disk, safe to double-fire: before any step runs, the
proposal is *claimed* (`pending` -> `running`, a compare-and-set under the
store's lock), and a second pass racing the first fails its claim and does
nothing. Only after the steps does it move to `done` or `failed`.

Config, in the workspace's `config.json`:

    "approvals": {
      "schedule": "every 5m",
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
a private channel, plus `chat:write` for the outcome message). `schedule` is
read only by `agent install-schedule` (framework/schedule.py). `hours` bounds
how far back history is read (default 24). The webhook that posted the
proposal cannot read replies; a bot token can, which is why both exist.

What counts as a reply: a top-level channel message whose text is exactly
`do <id>` or `skip <id>` (whitespace-trimmed, the full id including its random
suffix) from a user in `trusted_user_ids`, posted *after* the proposal was
recorded. Anything else is ignored, including "do all", a reaction, a `do`
from anyone not on the list, or a `do` that predates the proposal it names.
No blanket approval exists, and nothing said before a proposal existed can
approve it. If a trusted user says both `do p7-k3f` and `skip p7-k3f` before
the pass runs, the later message wins, which is the reading a human would
give a change of mind.

Why a time bound rather than a thread: the proposal is announced through an
incoming webhook, and webhooks do not return the message `ts`, so there is no
thread to require replies in. `conversations.history` returns only top-level
messages, so a reply typed *in a thread* under the announcement is never seen
and the proposal stays pending. Reply in the channel, not in the thread.

Executing `do`: the action's steps run in order. Each step is built through
the registry from its own block, gets the proposal's payload merged with every
earlier step's returned dict, and its own returned dict is merged in for the
next. A step that raises stops the sequence: the proposal becomes `failed`
naming the step, later steps do not run, and nothing is retried. Retrying a
publish is not obviously safe (the first attempt may have half-succeeded), so
retry is a human decision made by reading the outcome message and the store.

A proposal can also be resolved by clicking a button on its message instead of
replying, when the deployment runs `agent approvals --listen` (issue #74, see
framework/approval_buttons.py). That path shares this module's claim, its step
runner and its store, so a click and a reply racing each other cannot both
execute: whichever claims first runs, the other is told the proposal is already
spoken for. It adds one variant, `draft`, which forces the WordPress step to
`mode: draft` and skips the announcement steps after it; a reply is always the
`post` variant. The variant is recorded on the proposal.

Whatever happened, one outcome message goes to the channel through
`chat.postMessage`, and one summary line is printed, unconditionally, in the
shape the runner uses (issue #31): `agent=<name> status=ok approvals=N
executed=N skipped=N failed=N stuck=N`.

Order inside a `do` matters: claim, then steps (each recorded in the store as
it completes), then the terminal status, then the channel is told. If posting
the outcome fails, the status is already right and the error propagates.

A proposal still `running` when a pass starts, whose claiming pid is gone (or
whose claim is older than `hours`), is one an earlier pass claimed and never
finished: the process died mid-steps. A live claiming pid is just an
overlapping pass still at work and is left alone. A stuck one is never re-run
automatically, because the steps it did complete (say, the publish) are real
and re-running them is exactly the duplicate the claim exists to prevent.
Every pass reports it on stderr and counts it in the summary line as
`stuck=N`; the channel is told once, with the steps the store has recorded as
done. Re-running it is a human decision: edit the store and set the proposal
back to `pending` (or to `failed` to close it). The `do` that claimed it is
still in the channel, so if it is inside the `hours` window the next pass runs
every step again at once, including the ones that already happened; otherwise
a fresh `do` is needed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from framework.config import CONFIG_FILENAME, ConfigError
from framework.proposals import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    ProposalStore,
    created_ts,
    proposals_path,
)
from framework.registry import build_action
# Moved to framework/slack.py (issue #86), where the whole posting path lives;
# re-exported until the approvals path imports it from there directly.
from framework.slack import SlackApiError, slack_call as _slack_call

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
        app_token: str = "",
        listen_for: list[Path] | None = None,
        action_page: Any = None,
    ):
        self.agent = agent
        self.workspace = workspace
        self.channel_id = channel_id
        self.bot_token = bot_token
        self.trusted_user_ids = trusted_user_ids
        self.hours = hours
        self.actions = actions
        self.timeout = timeout
        self.app_token = app_token
        self.listen_for = listen_for or []
        # An `ActionPageConfig` or None. Typed loosely because
        # framework/action_page.py imports this module, so naming its type
        # here would be a cycle; the loader builds it.
        self.action_page = action_page


def load_approvals_config(workspace: str | Path) -> ApprovalsConfig:
    """Read the `approvals` and `actions` blocks of `<workspace>/config.json`.

    Deliberately reads only what this pass needs, rather than going through
    `framework.config.load_workspace`. That loader requires `source`, `scorer`
    and `destination` to be objects with a `type`, none of which this pass
    uses; a proposal already recorded should still be executable or skippable
    while an operator has the source block half-edited. The file checks and
    error wording match `load_workspace`'s.
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
    # Read only by `--listen` (framework/approval_buttons.py), and only if the
    # config names it: a workspace that approves by reply needs no app token,
    # and demanding one here would break every existing deployment.
    app_token_env = block.get("app_token_env")
    app_token = ""
    if app_token_env:
        app_token = os.environ.get(app_token_env, "")
        if not app_token:
            raise ConfigError(
                f"approvals need the Slack app-level token in ${app_token_env} "
                f"(named by {config_path}: 'approvals.app_token_env')"
            )

    # Other workspaces whose button clicks this workspace's listener also
    # serves (issue #103): Slack hands an app's clicks to any of its Socket Mode
    # connections, so every workspace posting cards through one app has to be
    # served by the one listener that app has. Paths are relative to the
    # workspaces root, i.e. sibling workspace names.
    listen_for_raw = block.get("listen_for", [])
    if not isinstance(listen_for_raw, list) or not all(
        isinstance(w, str) and w.strip() for w in listen_for_raw
    ):
        raise ConfigError(
            f"{config_path}: 'approvals.listen_for' must be a list of workspace names"
        )
    listen_for = [(workspace_path.parent / w).resolve() for w in listen_for_raw]

    # The link surface (issue #124). Absent is the normal case: every
    # deployment before #124 approves in Slack only. Imported here rather than
    # at module scope because action_page.py imports this module for the claim
    # and the step runner.
    try:
        from framework.action_page import load_action_page
    except ImportError as exc:
        # This function is documented to raise ConfigError, and its callers --
        # `cli.py`'s handler and `listener_routes`, which names the workspace
        # that failed -- catch only that. A broken module here would otherwise
        # surface as a bare traceback from the one place built to explain
        # itself.
        raise ConfigError(f"the action page module could not be imported: {exc}") from exc

    action_page = load_action_page(block, config_path)

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
        app_token=app_token,
        listen_for=listen_for,
        action_page=action_page,
    )
    return retval


def _now_iso() -> str:
    retval = datetime.now(timezone.utc).isoformat(timespec="seconds")
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


# How long a claim made by the action page may sit `running` before a pass
# calls it stuck. Short, because the pid test cannot apply to it (see
# `_is_stuck`) and the handler that made the claim either finished in
# milliseconds or is not coming back.
_LINK_CLAIM_STALE_SECONDS = 900

DRAFTABLE_STEP = "wordpress-publish"
VARIANT_POST = "post"
VARIANT_DRAFT = "draft"


def has_draftable_step(cfg: ApprovalsConfig, action_name: str) -> bool:
    """Whether `action_name` has a step the `draft` variant can draft."""
    action = cfg.actions.get(action_name)
    steps = (action or {}).get("steps") or []
    retval = any(step.get("type") == DRAFTABLE_STEP for step in steps)
    return retval


def plan_steps(steps: list[dict[str, Any]], variant: str) -> list[tuple[int, dict | None, str]]:
    """The steps to run for `variant`, as (number, config-or-None, why-skipped).

    `post` is the sequence as configured. `draft` forces the WordPress step to
    `mode: draft` and skips everything after it: those later steps are the
    announcement, by construction -- they consume the publish result -- and
    announcing a draft is the one thing the Draft button exists to avoid.
    Nothing before the publish step is skipped; it is what produced the content.
    """
    if variant != VARIANT_DRAFT:
        retval = [(i, dict(step), "") for i, step in enumerate(steps, start=1)]
        return retval
    last_draftable = 0
    for i, step in enumerate(steps, start=1):
        if step.get("type") == DRAFTABLE_STEP:
            last_draftable = i
    plan: list[tuple[int, dict | None, str]] = []
    for i, step in enumerate(steps, start=1):
        if i > last_draftable:
            plan.append((i, None, "skipped: draft announces nothing"))
            continue
        step = dict(step)
        if step.get("type") == DRAFTABLE_STEP:
            step["mode"] = "draft"
        plan.append((i, step, ""))
    retval = plan
    return retval


def steps_detail(results: list[dict[str, Any]]) -> str:
    """One human-readable line naming each step and how it went."""
    retval = ", ".join(f"{r['type']} {r['status']}" for r in results) or "no steps run"
    return retval


def human_time(slack_ts: str) -> str:
    """A Slack `ts` (epoch seconds, as a string) as a readable UTC time."""
    try:
        moment = datetime.fromtimestamp(float(slack_ts), tz=timezone.utc)
    except (TypeError, ValueError):
        retval = _now_iso()
        return retval
    retval = moment.isoformat(timespec="seconds")
    return retval


def _run_steps(
    cfg: ApprovalsConfig,
    store: ProposalStore,
    proposal: dict[str, Any],
    variant: str = VARIANT_POST,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run the claimed proposal's action steps in order; return (results, error).

    `error` is None when every step ran, else a message naming the step that
    stopped the sequence. `results` holds one entry per step attempted, and is
    written to the store after each step so that a pass dying mid-sequence
    leaves a record of what already happened. A step the variant skips is
    recorded too, as `status: "skipped"`, so the store says what did not run
    as plainly as what did.
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
        "variant": variant,
    }
    for i, step_cfg, skipped in plan_steps(action["steps"], variant):
        if step_cfg is None:
            results.append(
                {"step": i, "type": action["steps"][i - 1].get("type"), "status": "skipped",
                 "note": skipped}
            )
            store.transition(proposal["id"], STATUS_RUNNING, STATUS_RUNNING, results=results)
            continue
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
        store.transition(proposal["id"], STATUS_RUNNING, STATUS_RUNNING, results=results)
    retval = (results, None)
    return retval


def _stuck_text(proposal: dict[str, Any]) -> str:
    pid = proposal["id"]
    done = ", ".join(
        f"{r['type']} {r['status']}" for r in proposal.get("results") or []
    ) or "none"
    retval = (
        f":warning: Proposal {pid} ({proposal['action']}) is stuck in 'running': an approvals "
        f"pass claimed it at {proposal.get('claimed_at')} (pid {proposal.get('claimed_pid')}) "
        f"and did not finish. Steps recorded as done: {done}. It will not be re-run; "
        "an operator has to edit the proposals store to resolve it."
    )
    return retval


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, TypeError, ValueError):
        retval = False
        return retval
    except PermissionError:
        retval = True
        return retval
    retval = True
    return retval


def _resolved_elsewhere(cfg: ApprovalsConfig, store: ProposalStore) -> int:
    """Proposals resolved by a button or a link inside the `hours` window.

    This pass counts what *it* did, so a proposal resolved by a click or a tap
    between two passes simply stopped being `pending` and left no trace in any
    line an operator reads -- `approvals=0 executed=0 ... stuck=0`, a quiet
    agent, while the listener was working through leads. The count is derived
    from the store rather than tracked, so it needs no new state and double
    counts nothing: the same window the reply path already reads.
    """
    cutoff = time.time() - cfg.hours * 3600
    retval = 0
    try:
        resolved = store.with_status(STATUS_DONE) + store.with_status(STATUS_SKIPPED)
    except Exception:
        # This runs inside the `finally` that prints the summary line, which is
        # printed unconditionally. A raise here would replace whatever actually
        # went wrong with a failure to count, so an unreadable store costs the
        # count and nothing else.
        retval = -1
        return retval
    for proposal in resolved:
        if proposal.get("decided_via") not in ("button", "link"):
            continue
        when = proposal.get("resolved")
        if not when:
            continue
        try:
            if datetime.fromisoformat(when).timestamp() >= cutoff:
                retval += 1
        except ValueError:
            continue
    return retval


def _is_stuck(cfg: ApprovalsConfig, proposal: dict[str, Any]) -> bool:
    """A `running` proposal whose claiming pass is gone, or too old to trust.

    Two passes overlapping for a few seconds is normal and the second one must
    not cry wolf about the first, so `running` alone is not stuck. The pass
    that claimed it recorded its pid; on the single host these run on, a dead
    pid means it died mid-steps. Pids get reused, so a claim older than the
    `hours` window is stuck regardless of what that pid is doing now.
    """
    claimed_at = proposal.get("claimed_at")
    age = None
    if claimed_at:
        age = time.time() - datetime.fromisoformat(claimed_at).timestamp()
    too_old = True if age is None else age > cfg.hours * 3600

    # A claim made by the action page (issue #124) records the *listener's*
    # pid, which outlives any one request by design, so `_pid_alive` is always
    # true for it and the pid test -- the thing that detects a pass dying
    # mid-steps -- can never fire. Judge those on age alone, and on a much
    # shorter clock than `hours`: a request that has held a claim for a quarter
    # of an hour is not still working, and waiting out a 24-hour window to say
    # so means a lead whose steps really ran sits unreported for a day.
    if proposal.get("claimed_via") == "link":
        retval = True if age is None else age > _LINK_CLAIM_STALE_SECONDS
        return retval

    retval = too_old or not _pid_alive(proposal.get("claimed_pid"))
    return retval


def _report_stuck(cfg: ApprovalsConfig, store: ProposalStore) -> int:
    """Say, every pass, which proposals an earlier pass left `running`.

    stderr every time, the channel once per proposal. Nothing here changes a
    proposal's status: leaving `running` is the operator's call.
    """
    stuck = [p for p in store.running() if _is_stuck(cfg, p)]
    for proposal in stuck:
        print(
            f"agent: proposal {proposal['id']} ({proposal['action']}) is stuck in "
            f"'running' since {proposal.get('claimed_at')} (pid {proposal.get('claimed_pid')}); "
            "not re-run, edit state/proposals.json to resolve",
            file=sys.stderr,
        )
        if proposal.get("stuck_reported"):
            continue
        _slack_call(
            "chat.postMessage",
            cfg.bot_token,
            cfg.timeout,
            channel=cfg.channel_id,
            text=_stuck_text(proposal),
        )
        store.transition(proposal["id"], STATUS_RUNNING, STATUS_RUNNING, stuck_reported=True)
    retval = len(stuck)
    return retval


def _outcome_text(proposal: dict[str, Any], status: str, results: list, error: str | None) -> str:
    pid = proposal["id"]
    action = proposal["action"]
    if status == STATUS_SKIPPED:
        retval = f"Proposal {pid} ({action}): skipped."
        return retval
    steps = steps_detail(results)
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
    stuck = 0
    status = "failed"
    try:
        stuck = _report_stuck(cfg, store)
        proposals = store.pending()
        pending = len(proposals)
        replies = read_replies(cfg) if proposals else {}

        for proposal in proposals:
            pid = proposal["id"]
            reply = replies.get(pid)
            if reply is None:
                continue
            if float(reply["ts"]) <= created_ts(proposal):
                print(
                    f"agent: ignoring '{reply['verb']} {pid}' at ts {reply['ts']}: "
                    f"it predates the proposal ({proposal['created']})",
                    file=sys.stderr,
                )
                continue
            decided = {
                "decided_by": reply["user"],
                "decided_ts": reply["ts"],
                "decided_via": "reply",
            }
            if reply["verb"] == "skip":
                if store.transition(pid, STATUS_PENDING, STATUS_SKIPPED, **decided) is None:
                    continue
                skipped += 1
                text = _outcome_text(proposal, STATUS_SKIPPED, [], None)
            else:
                claimed = store.transition(
                    pid,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                    claimed_at=_now_iso(),
                    claimed_pid=os.getpid(),
                    variant=VARIANT_POST,
                    **decided,
                )
                if claimed is None:
                    # Another pass got there first; whatever it does is its
                    # to report.
                    continue
                results, error = _run_steps(cfg, store, proposal)
                if error is None:
                    store.transition(pid, STATUS_RUNNING, STATUS_DONE, results=results)
                    executed += 1
                    text = _outcome_text(proposal, STATUS_DONE, results, None)
                else:
                    store.transition(
                        pid, STATUS_RUNNING, STATUS_FAILED, results=results, error=error
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
            f"executed={executed} skipped={skipped} failed={failed} stuck={stuck} "
            f"elsewhere={_resolved_elsewhere(cfg, store)}"
        )
    retval = 0
    return retval
