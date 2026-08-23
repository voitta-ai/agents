# agents

Framework for small, single-purpose pain-signal / lead-finding agents that run
as short-lived, cron/launchd-invoked processes (not daemons).

## Shape

- **Source** — where signals come from (e.g. Reddit RSS). Plugin interface,
  one implementation per source. See `framework/interfaces.py`.
- **Scorer** — turns raw signals into a score (regex/keyword for v1; LLM-based
  waterfall scorer is a tracked future issue, not v1).
- **Destination** — where scored signals get sent (Slack first; email later).
  Two-tier routing (hot / firehose) is a Slack-destination concern, not a
  framework concern.
- **State** — the framework's own job: dedupe between runs so a double-fire
  from cron isn't harmful. See `framework/state.py`.

## What this framework does *not* own

No scheduler daemon. Each agent is invoked run-and-exit (cron/launchd owns the
clock); the agent declares its intended cadence in its own config as
documentation, nothing enforces it yet (see tracked future issue for an
`agent install-schedule` helper).

No built-in interactivity/HITL loop. [Shmobster](https://github.com/voitta-ai/shmobster)
is the preferred/default pattern for human-in-the-loop Slack interactivity
(we own it, it's Slack-native already) — not a hard dependency, just the
stated default until an agent has a real reason to do something else.

## Layout

```
framework/          core: plugin interfaces, entrypoint contract, state/dedupe
agents/<name>/       one dir per agent: its config + any agent-specific glue
```

## Agents

- `agents/reddit-leads/` — first agent, porting the pain-signal Reddit
  monitor currently running out of `hq/leads/signals/`. See issue #8.
- `agents/slack-workspace/` — digests community Slack workspaces we
  participate in but don't administer, via their own notification emails
  forwarded to AgentMail. See issue #19.

## Status

Early scaffold. See GitHub issues for the tracked build-out plan (#1-#13).
