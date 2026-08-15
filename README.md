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

## Operating notes

### Secrets live in the environment, never in config

Each destination names the env var holding its credential (`webhook_env`,
`api_key_env`). Real per-agent `config.yaml` files are gitignored; only
`config.yaml-example` is committed. Agents are launched with `bash -lc`, so the
login profile supplies the values and no credential is stored in a launchd plist.

### Slack incoming webhooks do not survive the app being reinstalled

**This will bite you, and the symptom is misleading.** Removing and re-adding a
Slack app permanently invalidates every incoming webhook URL it issued. The app
looks healthy afterwards — its bot token still authenticates — but posting to an
old URL returns:

```
HTTP 404  no_service
```

The URL in your environment is unchanged, so nothing looks wrong on our side.
There is no way to restore the old URL; Slack mints new ones.

Recovery:

1. On the Slack app → **Incoming Webhooks** → **Add New Webhook to Workspace**,
   once per channel the agents post to.
2. Update the corresponding env var in your profile.
3. Nothing to redeploy. Agents read the env var on every run, so they recover on
   their own next tick.

While the webhook is dead an agent exits non-zero and delivers nothing, but
**no signals are lost**: the runner marks dedupe state only after delivery
succeeds, so everything undelivered goes out on the next good run.

### Verifying a credential without exposing it

To check which of several similar-looking webhooks a config actually uses,
compare hashes rather than values:

```bash
printf '%s' "$SOME_WEBHOOK" | shasum -a 256 | cut -c1-12
```

Equal prefixes mean the same credential. This is how you confirm a deployment is
bound to the webhook you think it is, and which of several is safe to delete,
without printing a secret into a terminal, a log, or an agent transcript.

## Status

Early scaffold. See GitHub issues for the tracked build-out plan (#1-#13).
