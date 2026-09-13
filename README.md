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

The repo holds code and placeholder examples only:

```
framework/                       core: plugin interfaces, entrypoint contract, state/dedupe
agents/<name>/config.example.json one committed, placeholder-only example per agent
```

A deployed agent lives in a **workspace**: one directory per agent, outside any
git checkout, holding everything that agent owns at runtime. A merge to master
is then not a deploy, and nothing an agent writes lands in the working tree.

```
<workspaces-root>/               $AGENT_WORKSPACES_ROOT, else ~/agent-workspaces
  _shared/                       cross-agent files (reserved; nothing reads it yet)
  <agent-name>/
    config.json                  the agent's declaration; the only file a human edits
    state/seen.json              SeenStore dedupe state (default; --state overrides)
    logs/                        stdout/stderr when scheduled
```

```
agent run --workspace <workspaces-root>/<agent-name>
```

`run` always takes the explicit workspace path; the root default is only for
helpers that take an agent *name*. Config is JSON: copy an example in as
`config.json` and fill in real values. A `"_comment"` key anywhere is ignored by
the framework and by every plugin, and is where decisions worth keeping go.

## Agents

- `agents/reddit-leads/` — first agent, porting the pain-signal Reddit
  monitor currently running out of `hq/leads/signals/`. See issue #8.
- `agents/slack-workspace/` — digests community Slack workspaces we
  participate in but don't administer, via their own notification emails
  forwarded to AgentMail. See issue #19.

## Operating notes

### Secrets live in the environment, never in config

Each destination names the env var holding its credential (`webhook_env`,
`api_key_env`). Real per-agent `config.json` files live in workspaces outside
the checkout; only `config.example.json` is committed. The framework itself only
ever reads `os.environ` when it builds a destination — how those variables get
there is the deployment's business, not this repo's.

In our own launchd deployment the agents are invoked through `bash -lc`, so the
login profile supplies the values and no credential is stored in a plist. If you
schedule them another way, whatever wraps them has to load the environment
itself; nothing in this repo does it for you.

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
3. Nothing to rebuild or redeploy — but the *scheduler* has to see the new
   value. Agents read the env var on every run, so with a `bash -lc`-style
   wrapper that re-sources the profile each tick they recover on their own. If
   your wrapper captured the environment once at load time, reload it, or you
   will keep failing delivery while trusting this runbook.

While the webhook is dead an agent exits non-zero and delivers nothing. The
runner marks dedupe state only after delivery succeeds, so an undelivered signal
is **not** burned — it is eligible to go out on the next good run.

That is at-least-once, not durable queueing, and the difference matters during a
long outage. There is no stored backlog: recovery depends on the *source* still
returning the same signals on the next fetch. A bounded feed makes that
time-limited — the reddit source reads the newest `limit_per_feed` entries of a
rolling `/new.rss`, so items that fall out of that window during a prolonged
outage are gone regardless of dedupe state. Short outage, nothing lost; multi-day
outage on a busy feed, assume gaps. Durable undelivered storage would be needed
for a stronger guarantee, and does not exist today.

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
