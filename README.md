# agents

Framework for small, single-purpose pain-signal / lead-finding agents that run
as short-lived, cron/launchd-invoked processes (not daemons).

## Shape

- **Source** — where signals come from (e.g. Reddit RSS). Plugin interface,
  one implementation per source. See `framework/interfaces.py`.
- **Scorer** — turns raw signals into a score. `regex` is presence-based keyword
  matching; `llm` asks a model through the waterfall in `framework/llm.py` and is
  configured by a prompt rather than rules (issue #9).
- **Destination** — where scored signals get sent (Slack first; email later).
  Two-tier routing (hot / firehose) is a Slack-destination concern, not a
  framework concern.
- **State** — the framework's own job: dedupe between runs so a double-fire
  from cron isn't harmful. See `framework/state.py`.

## What this framework does *not* own

No scheduler daemon. Each agent is invoked run-and-exit (launchd owns the
clock); the agent declares its cadence as `schedule` in its config, and
`agent install-schedule` turns that into the launchd job (see Scheduling).

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
  _shared/
    llm.json                     the deployment's LLM waterfall (see below)
  <agent-name>/
    config.json                  the agent's declaration; the only file a human edits
    state/seen.json              SeenStore dedupe state (default; --state overrides)
    state/llm-parked.json        vendors parked for budget exhaustion, with expiries
    logs/                        run.log / run.error.log (and approvals.*) when scheduled
```

```
agent run --workspace <workspaces-root>/<agent-name>
```

`run` always takes the explicit workspace path; the root default is only for
helpers that take an agent *name*. Config is JSON: copy an example in as
`config.json` and fill in real values. A `"_comment"` key anywhere is ignored by
the framework and by every plugin, and is where decisions worth keeping go.

## LLM waterfall

Every LLM-backed plugin (the `llm` scorer, the `digest` destination) calls one
ordered chain of vendors through LiteLLM's Router (`framework/llm.py`, issue
#44). The chain is `<workspaces-root>/_shared/llm.json`, one per deployment; a
plugin block with its own `"llm": {...}` of the same shape uses that instead.
Copy `docs/llm.example.json` to start. Each row names the env var holding its
key (`api_key_env`), never the key; a `codex/chatgpt/<model>` row runs on the
ChatGPT subscription with no key, reading the codex CLI's `~/.codex/auth.json`
(`codex login` when it expires). A vendor that answers with a budget-exhaustion
error is parked for `budget_park_sec` (or until the date it names, at most 31
days out) in
`<workspace>/state/llm-parked.json`; delete an entry to un-park it early.
Fallbacks and parks print one `llm:` line each on stderr.

## Scheduling

```
agent install-schedule --workspace <dir> [--label <label>] [--venv <path>] [--force] [--dry-run]
agent uninstall-schedule --workspace <dir> [--label <label>] [--force]
```

`install-schedule` (macOS) writes `~/Library/LaunchAgents/<label>.plist` from
the workspace's `config.json` and loads it with `launchctl bootstrap`, booting
out the loaded job first, so re-running it is how a changed schedule is
deployed. The label defaults to `com.agents.<agent>`; pass `--label` to keep an
existing deployment's name, and pass the same `--label` to uninstall. The job
runs `/bin/bash -lc "exec <venv>/bin/agent run --workspace <dir>"` (the login
profile supplies credentials), from the workspace, logging to `<dir>/logs/`.
`--venv` defaults to the venv of the `agent` you ran. `--dry-run` prints the
plist(s) and does nothing else.

Every generated plist is marked with `AgentsFrameworkManaged` and
`AgentsFrameworkWorkspace` (the workspace path). Install and uninstall refuse
to touch a label whose plist lacks the marker, belongs to another workspace, or
is loaded from somewhere else, and print what it runs; `--force` takes it
over. If bootstrapping an updated plist fails, the previous plist is put back
and reloaded, and the error says whether that worked.

- `schedule` (required): `"every 30m"` / `"every 4h"` becomes `StartInterval`,
  counted from when the job loaded. A five-field cron string becomes
  `StartCalendarInterval`, aligned to the clock: `"0 */4 * * *"` fires at
  00:00, 04:00, ... 20:00, `"0 14 * * 3"` Wednesdays at 14:00, `"0 17 * * 0"`
  (or `7`) Sundays at 17:00. Fields take numbers, `A-B` ranges and comma lists;
  minute and hour also take `*/N`. Names, `@daily`, stepped ranges, `*/N` in
  the date fields, and restricting both day-of-month and day-of-week are
  rejected. A bad schedule fails before anything is written.
- `approvals.schedule` (default `"every 5m"`): when the config has an
  `approvals` block, a second job `<label>.approvals` runs `agent approvals`
  on this schedule, logging to `logs/approvals.log`. Removing the block and
  re-installing removes that job; uninstall removes both.

Re-installing boots out a job that is mid-pass, which terminates it. For
`agent run` that costs nothing (undelivered signals are not marked seen), but
an `agent approvals` pass killed mid-steps leaves its proposal stuck (see
Approval-gated actions), so avoid re-installing while one is executing.

## Agents

- `agents/reddit-leads/` — first agent, porting the pain-signal Reddit
  monitor currently running out of `hq/leads/signals/`. See issue #8.
- `agents/slack-workspace/` — digests community Slack workspaces we
  participate in but don't administer, via their own notification emails
  forwarded to AgentMail. See issue #19.
- `agents/slack-workspace/` with `transport: session` — reads named channels
  of such a workspace as the logged-in browser user (`d` cookie decrypted
  from the local browser store, `xoxc` token scraped per run, nothing stored).
  Read-only by construction: the client refuses every Slack method outside a
  fixed allowlist before any HTTP. Needs `pip install '.[slack-session]'`
  (pycookiecheat). See issue #45.
- `agents/release-notes/` — one signal per GitHub release across an org since
  the last run, for the weekly release-notes digest. Uses the `github-releases`
  source (issue #46), which keeps its own window in
  `<workspace>/state/github-releases.json` and advances it only after a run has
  delivered (`Source.commit()`, see `docs/PLUGIN-CONTRACT.md`).

## Approval-gated actions

Some agents end in something that should not happen without a human saying so:
publishing a post, sending an application. For those the destination is
`approval`, which wraps the destination that writes the content and records a
**proposal** instead of posting it (issue #21):

```json
"destination": {
  "type": "approval",
  "webhook_env": "SOME_WEBHOOK",
  "wrap": {"type": "digest", "webhook_env": "SOME_WEBHOOK", "prompt": "..."},
  "actions": ["publish-post"]
},
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
      {"type": "slack-post", "webhook_env": "SOME_WEBHOOK", "text": "Published: {url}"}
    ]
  }
}
```

The run posts the rendered content plus `Proposal p7-k3f: publish-post. Reply
"do p7-k3f" or "skip p7-k3f".` and writes the proposal to
`<workspace>/state/proposals.json`. The proposal is the delivery, so dedupe
state is marked as usual. Ids are a per-workspace counter plus a random suffix,
so a reply cannot be typed before the proposal exists; the whole id is required.

```
agent approvals --workspace <workspaces-root>/<agent-name>
```

is the second scheduled entrypoint (`approvals.schedule`, every 5 minutes by
default, installed as its own job by `install-schedule`). It reads the channel's
last `hours` of history with the bot token, and for each pending proposal a
user in `trusted_user_ids` has answered with exactly `do <id>` or `skip <id>`
**as a top-level channel message posted after the proposal was recorded**,
claims it (`pending` -> `running`, atomically, so two overlapping passes cannot
both execute it), runs the named action's `steps` in order or marks it
skipped, posts one outcome message, and prints `agent=<name> status=ok
approvals=N executed=N skipped=N failed=N stuck=N`. A failing step stops the
sequence, leaves the proposal `failed` naming the step, and nothing is retried.
Anything that is not exactly `do`/`skip` from a trusted user is ignored, and so
is a reply older than the proposal it names. Replies typed *in a thread* are
not seen (`conversations.history` returns top-level messages only); reply in
the channel. The bot token needs `channels:history` (or `groups:history` for a
private channel) and `chat:write`.

A proposal left `running` by a pass that is no longer alive (or claimed more
than `hours` ago) means that pass died mid-steps. It is never re-run on its
own: every pass reports it on stderr and as `stuck=N`, and the channel is told
once which steps the store recorded as done. Re-running it is a manual edit of
`state/proposals.json`: set its `status` back to `"pending"`, and the next pass
runs all of its steps again, including any that already happened, if the
original `do` is still inside the `hours` window (otherwise reply `do` again).
To close it instead, set `"failed"`.

Steps are **action** plugins, the fourth kind (`docs/PLUGIN-CONTRACT.md`).
Later steps see earlier results by `{name}` templating over the proposal's
payload merged with each step's returned dict. Shipped:

- `command` (`framework/plugins/command_action.py`) — runs an existing tool:
  `{"type": "command", "argv": ["/abs/path/tool", "--text", "{blurb} {url}"],
  "result_format": "json"}`. `argv[0]` must be a literal path (a `{` or `}`
  in it is a config error: the program never comes from proposal data); every
  later element is templated. No shell, so quotes and `$(...)` in post text
  reach the tool as one literal argument. Optional `cwd`, `timeout` (default
  120). At most one of `result_pattern` (regex with named groups, searched in
  stdout; groups become results) or `result_format: "json"` (last non-empty
  stdout line is a JSON object; keys become results); neither means no results. A nonzero exit fails the step
  with the exit code and the last 20 stderr lines, which the outcome message
  posts to the channel; exit 0 with stdout that does not parse as configured
  fails too. Output goes to temporary files, not memory: at most the last
  64 KiB of stderr and the first 1 MiB of stdout are read, and more stdout
  than that with a result mode set fails as too large. Messages name only
  `argv[0]`. See issue #48.
- `slack-post` (`webhook_env`, `text`).
- `twitter-post` (`framework/plugins/twitter_action.py`) — tweets `text` (a
  template) through the X API v2 with OAuth 1.0a user context; credentials
  from `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`,
  `TWITTER_ACCESS_SECRET` (names overridable via `api_key_env`,
  `api_secret_env`, `access_token_env`, `access_secret_env`), read when the
  step runs. Over 280 characters is an error before any request. Needs
  `pip install '.[twitter]'`. Returns `tweet_url`. A 2xx never fails the step
  (a body without the id returns `tweet_url: ""` and a stderr notice); a
  failed step may still have posted, so check the account before re-running
  by hand. See issue #48.
- `wordpress-publish` (`framework/plugins/wordpress_action.py`) — turns
  `payload["title"]` + `payload["body_md"]` (Markdown) into a WordPress.com post
  and returns `{"url", "post_id"}` for later steps. Config:
  `{"type": "wordpress-publish", "site": "<site>.wordpress.com", "mode": "draft",
  "token_env": "WORDPRESS_ACCESS_TOKEN", "categories": [], "tags": []}`.
  `mode` is `draft` (safety valve; `url` is the preview) or `publish` (`url` is
  the public permalink). The token comes from `token_env`, else the macOS
  Keychain item `wpcom_token` the `wordpress-com-publish` skill writes; missing
  or rejected tokens fail loud naming that skill as the fix. See issue #47.

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

### Migrating an existing agent into a workspace

An agent that was running out of a checkout (`--config <dir>/config.yaml`, state
in `<dir>/.seen-<agent>.json`) does not carry its dedupe state over on its own:
the workspace default is `<workspace>/state/seen.json`, and an empty one means
every signal the source still returns is new again. There is deliberately no
fallback read of the legacy path; the cutover is one operator step, done while
the scheduler is stopped:

1. Unload the agent's scheduled job so nothing runs mid-move.
2. Create the workspace and write `config.json` from the old YAML.
3. Union the old `.seen-<agent>.json` with whatever `state/seen.json` already
   holds (both are flat JSON arrays of ids) and write the result to
   `state/seen.json`. If the workspace was hand-run before the cutover, the two
   files have diverged and neither alone is complete.
4. Re-point the scheduled job at `agent run --workspace <workspace>` and load it
   (`agent install-schedule --workspace <workspace> --label <existing-label> --force`).
   `--force` is needed once: a hand-written plist has no install-schedule
   marker, so without it the install refuses and prints what the job runs.
   Check that output names the job you meant to replace before re-running with
   `--force`.
5. Hand-run once and check the summary line: `new=` should be small (what
   arrived during the move), not the size of the feed.

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
