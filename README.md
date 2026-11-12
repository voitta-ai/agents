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

A workspace gets one job, two, or three: `agent run` on `schedule`, plus
`<label>.approvals` on `approvals.schedule` when the config has an `approvals`
block, plus `<label>.approvals.listen` when that block says `"listen": true`.
The listener is the one job with no schedule: `KeepAlive` instead of an
interval (see "Buttons instead of replies"). A job the config no longer asks
for is removed by the next install.

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
  forwarded to AgentMail. See issue #19. An inbox that looks empty is usually
  the `trash` label, which the AgentMail listing hides unless `include_trash`
  is set (issue #61).
- `agents/slack-workspace/` with `transport: session` — reads channels of such
  a workspace as the logged-in browser user (`d` cookie decrypted from the
  local browser store, `xoxc` token scraped per run, nothing stored).
  `channels` takes names or conversation ids (`C...`/`G...`, and `D...` for a
  direct message, which has no name to resolve), and `from_users` keeps only
  the authors named, so a bot's DM reads without your own replies (issue #61).
  On an Enterprise Grid org a user session cannot list channels or ask for
  permalinks, so names raise with ids named as the remedy and links are
  constructed (`meta.permalink_source`); ids work there unchanged (issue #69).
  A signal's `meta` carries `job_url` (the message's first link) and, when a
  bot posts a distinct one, `apply_url`, so a destination can link what the
  message is about rather than only the message (issue #76).
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

### Buttons instead of replies

Replying `do <id>` works, but the case that matters most -- a drafted blog post
-- wants one click, and a third answer the reply grammar has no room for. With
`"buttons": true` on the `approval` destination, a proposal is posted as Block
Kit with three buttons (issue #74):

- **Post** runs the action as configured.
- **Draft** runs the same sequence with the `wordpress-publish` step forced to
  `mode: draft`, and skips every step after it, so the post lands in the editor
  and nothing is announced. An action with no `wordpress-publish` step has
  nothing to draft: the click is refused, to that user only, and the proposal
  stays pending.
- **Discard** marks the proposal skipped. No step runs.

After a click the message is edited in place to say what happened, by whom and
when, so the channel shows the outcome instead of a stale prompt. Which variant
ran is recorded on the proposal, next to who decided it and whether it came
from a button or a reply.

A button needs Slack to deliver the click somewhere, which normally means a
public HTTPS endpoint. Socket Mode dials out instead:

```
agent approvals --listen --workspace <workspaces-root>/<agent-name>
```

is a long-running client holding that connection. It needs an app-level token
(`xapp-...`, scope `connections:write`, and Socket Mode plus Interactivity
turned on for the app), named by `approvals.app_token_env` the same way the bot
token is named by `bot_token_env`. Install it with `"listen": true` in the
`approvals` block; `install-schedule` then writes a third job,
`<label>.approvals.listen`, with `KeepAlive` and no interval, so launchd
restarts it whenever it exits -- a dropped connection or a slept laptop becomes
a reconnect. It needs `pip install '.[approvals-buttons]'` (slack_sdk,
websocket-client).

#### Which Slack app the listener connects as

**One Socket Mode consumer per Slack app.** Slack spreads an app's events and
interactions across all of that app's open Socket Mode connections, and each
one goes to only one of them. Whatever else connects with the same `xapp-`
token takes a share of the traffic. The listener acks every envelope and keeps
only button clicks, so anything else sent to it is dropped without a trace.

- **Never use a chat agent's app.** If `app_token_env` names the app-level
  token of a bot that answers `@`-mentions over Socket Mode, the listener
  silently eats a share of those mentions. The bot sees nothing, logs nothing,
  and its own connection stays healthy, so the miss looks random. This
  happened: a listener on a chat agent's app dropped a share of its
  mentions for a week before anyone traced it.
- **Use one shared app for the agents' approvals,** not one app per agent. It
  posts the cards (its bot token goes in `bot_token_env`, so the clicks come
  back to the same app) and holds the one Socket Mode connection
  (`app_token_env`). It must be invited to every approval channel. Create it
  from [`docs/slack-app-manifest.yaml`](docs/slack-app-manifest.yaml), which
  carries the full scope set.
- **One listener per app, serving every workspace on it** (issue #103). Exactly
  one workspace per app sets `"listen": true` and names the others in
  `approvals.listen_for`, as sibling workspace names:

  ```json
  "approvals": {"listen": true, "app_token_env": "SOME_APP_TOKEN",
                "listen_for": ["slack-workspace"], "...": "..."}
  ```

  A card's buttons carry `<workspace>/<proposal id>`, and the listener sends
  each click to that workspace's own store, channel, trusted users, bot token
  and actions; only the connection is shared. The other workspaces keep their
  `approvals` block (channel, `bot_token_env`, `trusted_user_ids`) and their
  polling pass, but set no `listen` and need no `app_token_env`. A second
  `"listen": true` on the same app is a second connection, and Slack would split
  the clicks between the two. A served workspace whose config does not load
  stops the listener at startup. Cards posted before #103 carry a bare id; the
  listener finds the one store that holds it, and refuses (pointing at
  `do <id>`) if none or several do.

An incoming webhook never receives anything, so webhooks can be shared freely.
They do not count as consumers.

The polling pass stays installed next to it, and is the reason a click that
never arrives is not a lost proposal: `do <id>` still resolves it. Both paths
take the same `pending -> running` claim, so a click racing a reply cannot run
the steps twice -- whichever claims first runs, and the other is told the
proposal is already spoken for. One consequence to know: a proposal resolved by
*reply* keeps its buttons, because the webhook that posted it returns no message
`ts` to edit. Clicking them afterwards runs nothing and says so, to the clicker
only.

One operator action to know about: a proposal carries no channel of its own, so
changing `approvals.channel_id` while proposals are still pending strands them.
Their buttons refuse as "wrong channel", visibly only to whoever clicked, and
the reply path will not see a `do` either, since it reads only the configured
channel's history. Resolve what is pending before moving an agent to a new
approval channel, or edit `state/proposals.json` and let the next run propose
them again.

### A link instead of Slack: the action page

Both paths above need a Slack account in the agent's workspace. The people who
decide on leads are not all in Slack and do not all want to be, which is the
reason Signal became a second interface at all. So a proposal can also carry a
link: a blurb goes to whatever surface the person reads, the link opens a page
with the same verdicts the buttons offer, and the decision happens there
(issue #124).

```json
"approvals": {
  "channel_id": "C0123456789",
  "bot_token_env": "SOME_BOT_TOKEN",
  "trusted_user_ids": ["U0123456789"],
  "action_page": {
    "base_url": "https://example.invalid/leads",
    "bind": "127.0.0.1",
    "port": 8731,
    "post_label": "Draft outreach"
  }
}
```

`base_url` is how the link reads to whoever receives it, which is not
necessarily how the process is reached: a tunnel or a reverse proxy sits in
between, so the two are separate keys and nothing guesses. It is the only one a
posting run needs; `bind` and `port` are read by the listener that serves the
page. `post_label` renames the first verdict, for the same reason the card can.
A workspace with no `action_page` behaves exactly as before and posts no link.

The page is served by the same long-running process as the buttons:

```
agent approvals --listen --workspace <workspaces-root>/<agent-name>
```

`--listen` now needs *either* an app-level token (for buttons) or an
`action_page` (for links), or both. A deployment that wants only links needs no
Slack app, no app token and no Socket Mode.

**The surfaces do not need reconciling, because there is only one of them.**
The same lead announced to a Slack channel and a Signal group is one proposal
with one link, so resolving it resolves it everywhere by construction. There is
no cross-surface state to converge, no window in which two surfaces both act,
and no half-closed item. A further surface -- email, SMS, a web digest -- needs
only to render a blurb and a URL, and needs no way to send a decision back,
which is what made approving from Signal expensive (that is issue #121, a
receive loop on an account only one process can hold; a link needs none of it).

A tap, a button click and a `do <id>` reply all take the proposal through the
same `pending -> running` claim, so no two of them can run the steps. Verified
against this implementation: eight concurrent taps on one link run the action
exactly once and tell the other seven the proposal is already spoken for, and a
tap is refused for a proposal the polling pass has already claimed, in both
directions.

Resolving from the page **edits the Block Kit card in place**, so a lead closed
by a link does not keep live buttons in Slack. That needs the message `ts`,
which only the API transport returns, so a card posted through a webhook gets a
separate outcome message instead. Clicking a stale card was always safe -- the
claim refuses it -- but a reader scrolling the channel had no way to tell the
lead was decided, which is the kind of thing that erodes trust in the channel
rather than breaking anything.

Two reporting consequences of a second process resolving proposals:

- `agent approvals` prints `elsewhere=N`, the proposals a button or a link
  resolved inside the `hours` window. Without it a pass reports
  `approvals=0 executed=0 ... stuck=0` -- a quiet agent -- while the listener
  works through leads in a different log.
- A claim made by the page records `claimed_via: "link"`, and the stuck check
  treats a live pid as proving nothing for those. The pid it records is the
  *listener's*, which outlives any one request by design, so the test that
  catches a polling pass dying mid-steps could never fire for a link claim: a
  tap whose steps ran and then failed to be written sat invisible for the whole
  `hours` window. Link claims are judged on age alone, with a 15-minute bound,
  and a request that raises after claiming closes the proposal as failed on its
  way out rather than leaving it `running`.

#### The URL is the credential

This is the part to decide deliberately. The reply path trusts a Slack user id
and so does the button path; a link has no identity behind it. Whoever holds
the URL can act, and in a group chat that means every member, anyone who joins
later and scrolls back, and any device backup. Choose it for a group whose
members would all have been in `trusted_user_ids` anyway.

What bounds it:

- The link carries its own 32-byte `secrets.token_urlsafe` token, compared with
  `secrets.compare_digest` -- **not** the proposal id, which is short by design
  (a counter plus three random base-36 characters: ample against someone typing
  `do p10` into a channel, nowhere near enough for something reachable over
  HTTP).
- A token resolves exactly one proposal, never the store, and is useless
  against any other lead.
- Single use falls out of the claim: the first verdict moves the proposal off
  `pending`, and every later request is told what it already is. There is no
  separate "token spent" state to get out of step.
- A miss returns one identical page whether the token is wrong or the workspace
  is not served, so the page is not an oracle for guessing tokens.
- The token must not leak outward. The page sends `Referrer-Policy:
  no-referrer` and marks links `rel="noreferrer noopener"`, because the blurb is
  about somebody else's web page and a plain anchor would hand the token to that
  site in the `Referer` header. The default request logging is turned off, since
  the request line contains the token. It is still in the reader's browser
  history and in the logs of whatever the request passes through, so `base_url`
  should point at something private -- a tailnet address, or a tunnel you
  control, not a public host.
- **No link is written onto a Slack card that already has buttons.** The buttons
  do the same job there and better, and putting the token in the message would
  store it in Slack, in push notifications and in email digests, and re-post it
  verbatim when a button resolves the proposal. A card with no buttons
  (`buttons: false`) does carry the link. Not because a Slack reader needs it --
  the same line already says `Reply "do pX" or "skip pX"`, which is the reply
  path and needs no token -- but because that message is also how the lead
  reaches somebody who is *not* in Slack, which is the whole reason the page
  exists. It is a real cost knowingly taken: the token is then in Slack's
  message history, its push notifications, its email digests and its search
  index, and it stays valid until the proposal is resolved. Set `buttons: true`
  for a channel where everyone can click, and no token is written.
- Nothing records a `decided_by` for a link verdict. The other two paths store
  the Slack user id that acted; a link has no identity behind it, and a value
  there would read as an actor in the store. `decided_via: "link"` is the whole
  truth, and for a group of several people that is all the audit trail there
  is -- which is part of what is being chosen.

#### A GET must never act

This is the correctness condition for the whole design, not a preference.
Signal, Slack and iMessage all build link previews by fetching the URL **from
the recipient's device**. A link whose GET resolves the proposal would be
resolved by the message rendering on somebody's phone, before a human has read
it -- the same mechanism that made "unsubscribe" links unusable for a decade,
courtesy of corporate mail scanners.

So `GET` and `HEAD` render and change nothing; only `POST` resolves. The POST
carries a form token the GET minted, which also stops a third-party page
driving a verdict on a reader's behalf. A restart of the listener between the
GET and the POST invalidates that token and the page says to reload: the cost of
keeping no server-side session.

The blurb is a scraped post, which is to say attacker-controlled text. It is
HTML-escaped and rendered as preformatted text, the only anchor is the lead's
own URL after an `http`/`https` scheme check, and no Markdown becomes markup --
a stored cross-site scripting bug on a page that resolves proposals would hand
over both the token and the verdict.

#### This is not an internet-facing server

`bind` defaults to loopback and a non-loopback bind prints a warning at
startup, because the page has no authentication beyond the link token and no
rate limiting worth the name. Put it behind a tailnet or a tunnel you control.

Two properties are worth knowing rather than discovering. Responses are
HTTP/1.0, so every one closes its connection: with keep-alive, any handler that
answers *without* reading the request body becomes a request-smuggling
primitive, because the unread bytes are parsed as the next request on that
connection -- an adversarial review got a POST to a nonexistent token to
execute a verdict framed in its body, and the same trick through a connection-
pooling proxy returns the next victim's page, which contains their link token.
One connection per response removes the whole class, including the paths
`BaseHTTPRequestHandler` answers by itself where there is nothing to drain
from. `Transfer-Encoding` is refused outright for the same reason.

And concurrency is capped at 64 connections. Enough held connections therefore
make the page unavailable rather than exhausting the machine's threads -- which
would take the Socket Mode listener in the same process down with it. A
per-client cap would be the usual answer and is the wrong one here: behind a
proxy every connection arrives from the proxy.

The 15-second handler timeout is **not** a request deadline, and this was
measured rather than assumed: it is a per-socket-operation timeout, so a client
that sends one byte every ten seconds resets it on every byte and holds its
slot for as long as it likes. An idle connection is reclaimed after 15s; a
trickling one is not. Sixty-four of those hold every slot and the person who
needs to approve something gets a refused connection until they stop. The cap
still does its real job -- the listener in the same process survives -- but the
page does not recover on its own while a slow client is deliberate about it.
This is accepted rather than solved, for the same reason as the per-client cap:
the fix is a request deadline the standard library does not offer here, and the
page is reachable only from loopback or a private tunnel.

An unauthenticated request is also not free. Every request reads the whole
store once under the store's lock, before any credential check -- a bogus token
costs the same read as a real one -- and that is the same lock the Slack button
path and the `agent approvals` pass take. Flooding the page therefore slows
those down: measured on a 547 KB store, 64 concurrent bogus-token requests took
a `transition()` from 10ms to 494ms at the 95th percentile. One read per
request rather than two is what the code does now; the rest is the same
accepted exposure as above.

### Drafting several things at once: `digest` fields

An action whose steps publish a post *and* announce it needs more than one
piece of text, and a digest that writes one blob of prose has only `{body_md}`
to give them. `fields` (issue #63) asks the model for a JSON object instead,
and every key becomes a template name for the steps:

```json
"wrap": {
  "type": "digest",
  "webhook_env": "SOME_WEBHOOK",
  "prompt": "...",
  "min_score": 60,
  "fields": {
    "title": "A post title, under 70 characters.",
    "body_md": "The post itself, Markdown.",
    "blurb_twitter": "Under 240 characters, plain text, no hashtags."
  }
}
```

A step can then say `{"type": "twitter-post", "text": "{blurb_twitter}"}`. The
values are descriptions for the model, not defaults, and `body_md` has to be
one of them. `empty`, `footer`, `items` and `model` are refused as field names:
`render()` sets those itself, and a field named `empty` would make a written
digest look like a quiet run and be dropped. Without a `fields` key the digest
behaves exactly as before: one prose summary in `body_md`.

The proposal message shows the model's `title` and `body_md`, then lists the
other fields under `also drafted:` with their text, so what is approved is what
goes out. If the model does not come back with a JSON object carrying every
field (or any of them is empty), the run asks once more and then fails without
recording a proposal — nothing is marked seen, so the next run drafts again
rather than publishing half an announcement.

Alongside the drafted fields, `render()` returns values the digest computed from
the signals rather than from the reply (issue #75), as `meta_values` and as
plain template names. The first is `repos`: the distinct `meta["repo"]` of the
items the model was shown, comma-separated, so a release-notes post can be
tagged with the repos that actually released. Use those for anything that must
not move when the model phrases its answer differently — a taxonomy, a routing
key — and a drafted field for prose. `meta_values` and `repos` are refused as
field names, so a config cannot end up asking the model for a value it already
has.

`min_score` (default 0, no gate) drops signals scoring below it before the
model sees them, and they are not in the item count either. They are still
marked seen. With a gate set, the item list the model reads also carries each
item's score and the scorer's reasons; without one it does not, so a digest
configured before this existed reads exactly the prompt it always did.

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
  "token_env": "WORDPRESS_ACCESS_TOKEN", "categories": ["releases"],
  "tags": ["release-notes", "{repos}"]}`.
  `mode` is `draft` (safety valve; `url` is the preview) or `publish` (`url` is
  the public permalink). The token comes from `token_env`, else the macOS
  Keychain item `wpcom_token` the `wordpress-com-publish` skill writes; missing
  or rejected tokens fail loud naming that skill as the fix. See issue #47.
  `categories` and `tags` entries are templates (issue #75): an entry that
  expands to a comma-separated string becomes separate names, blanks are
  dropped, and duplicates collapse case-insensitively keeping the first
  spelling, so standing tags and per-run `{repos}` can overlap freely.

## Operating notes

### Slack renders mrkdwn, not Markdown

Everything this repo posts to a Slack webhook goes through one function,
`framework/slack.py::post_message`, which converts Markdown to Slack's own
mrkdwn on the way out (issue #73): headings become bold lines, `[text](url)`
becomes `<url|text>`, `**bold**` becomes `*bold*`, list markers become bullets,
and code spans and fenced blocks are left exactly as written. Emphasis fires
only on word boundaries, so `https://example.com/a__b__c` keeps its underscores:
a URL that lost a pair is a different URL. `__x__` is not treated as bold at all,
on purpose: what this repo posts is release notes and scorer reasons about code,
where `__init__` is an ordinary word and `**bold**` is how anyone here writes
bold. `&`, `<` and `>` become entities in
literal text, per Slack's own rule, and never inside a link this repo built, a
blockquote marker or a code block. Text that holds none of those characters and
no Markdown is posted unchanged. Code in this repo that builds a header writes
Markdown (`**Title**`) for the same reason: there is one place that renders.

A message longer than a Slack block (3000 characters) is posted as **several
messages in order**, split at a paragraph boundary where there is one, never
truncated; a split inside a fenced block closes and reopens the fence, and a
cut inside a line falls between grapheme clusters so an emoji is never halved.
Parts are numbered (`_(part 2 of 3)_`), because another agent sharing the
webhook can post between them. Only the
Slack rendering changes: `body_md` in a recorded proposal, which is what a
WordPress publish step later sends, stays Markdown.

### Two ways to reach Slack, and why the second one matters

A Slack destination (`slack`, `digest`) and the `slack-post` action name either
an incoming webhook or a bot token, exactly one (issue #86):

```json
{"type": "digest", "webhook_env": "SOME_WEBHOOK", "prompt": "..."}
{"type": "digest", "bot_token_env": "SOME_BOT_TOKEN", "channel_id": "C0123456789",
 "prompt": "...", "username": "release notes", "icon_emoji": ":package:"}
```

Conversion, splitting, footers, part markers and blocks are identical on both.
Naming both, or neither, fails at build time. Two things differ:

- **`username` / `icon_emoji` / `icon_url` (issue #77) only work with a bot
  token.** A modern incoming webhook ignores them: the post returns `ok` and
  the message still arrives under the app's own name, so several agents sharing
  one webhook are indistinguishable however they are configured. Through
  `chat.postMessage` they are honoured, given the bot's `chat:write.customize`
  scope. The keys ride on the payload either way, since a webhook ignoring them
  is not an error.
- **Only a bot token gets a message `ts` back.** A webhook answers `ok` and
  nothing else, so a message posted through one can never be edited afterwards
  — which is why a proposal announced over a webhook keeps its buttons after it
  has been resolved. With a token, `post_message` returns the `ts` of every
  message it posted and `update_message` can edit it in place.

The bot needs `chat:write`, plus `chat:write.customize` for the name and icon,
and it has to be in the channel `channel_id` names. A route may carry its own
`channel_id` under one destination-level `bot_token_env`.

### Signal is a second interface, on an account of its own

A `signal-messenger` destination posts a scored signal to a Signal group
(issue #120), so an agent's output can land on a phone rather than in a
channel:

```json
"destination": {
  "type": "signal-messenger",
  "account_env": "AGENTS_SIGNAL_ACCOUNT",
  "group_id_env": "AGENTS_SIGNAL_GROUP_LEADS",
  "min_score": 10
}
```

**The type is `signal-messenger`, not `signal`.** The `approval` destination
already has a `mode: "signal"`, meaning one proposal per `Signal` — the
dataclass every source emits — and in a config that holds both, a destination
called `signal` reads as that one.

**It needs a Signal account this deployment owns.** One process owns a Signal
account at a time: `signal-cli`'s daemon holds it, and nothing else can open it
concurrently — the same shape as one Socket Mode consumer per Slack app. So
these agents register their own number rather than borrowing a conversational
agent's account and depending on its relay. Register it once with
`signal-cli register --captcha <token>` (the token comes from
`signalcaptchas.org/registration/generate.html`, is valid for minutes, and must
be used from the same external IP as the browser that solved it), then
`signal-cli verify <code>`. The number and the group id are named by env var,
never written in config: a `config.example.json` in this repo is public.

The dependency is the `signal-cli` binary (`brew install signal-cli`), not a
Python package, so there is no optional extra to install — a missing binary
fails when the destination is built, naming it. That is all the build checks:
an unregistered account or a group id this account is not in costs a subprocess
and the account lock to verify, so it is left to fail on the first send rather
than on every run that sends nothing.

#### Two transports, because only one process can hold the account

`transport` picks how the message leaves. The default, `signal-cli`, is the
subprocess above. `gateway` sends through voitta-gateway, a service that owns
the account and serves every agent behind one daemon:

```json
"destination": {
  "type": "signal-messenger",
  "transport": "gateway",
  "gateway": {
    "url": "http://127.0.0.1:<port>",
    "conversation": "<alias>",
    "token_file": "<workspace>/gateway-token",
    "label": "<the label the gateway prefixes, or omitted>",
    "wait_s": 10
  },
  "min_score": 10
}
```

They are exclusive and the switch is a flip, not a fallback. Once the
gateway's daemon holds the account the subprocess path cannot open it, so a
transport that fell back to `signal-cli` would be a second process contending
for an account another already has — it would hang and then drop the message,
which is worse than refusing.

**The group id is not in this block.** `conversation` is an alias the gateway
maps to the real conversation, so a private group's id stays in the gateway's
own config and never reaches a consumer, a consumer's logs or a failure it
raises. That is the whole reason for the alias.

**The token is a file, not an env var.** Two reasons, and the second is the
one that decided it: a scheduled run reads the environment launchd gives it
rather than a login shell's, and a variable in the environment is inherited by
every child the run spawns — and these runs spawn LLM CLIs. The file must be
mode 0600; the build refuses a group- or world-readable one, names the mode it
found, and says to have the token reissued, because a credential other
accounts could read is a credential to treat as disclosed.

**`wait_s` is what keeps "a clean exit means delivered" true.** The gateway
answers `202 queued` the moment it accepts a message, and a queued message can
still fail — as an event a run-and-exit agent has already exited before seeing.
So the send asks the gateway to hold the response until Signal takes it:
`200` means the message left, `502` means refused for good, and `202` means it
was still in flight at the deadline, which includes a retry of a message still
in flight. Only `200` returns; both others raise, so a caller that marks a
signal delivered when the send returns can never mark one that did not arrive.

A `200` carries no Signal timestamp. Its status is `sent`, `delivered`, `read`
or `deleted` -- a retry arriving after Signal has acked sees `delivered` or
`read`, and `deleted` is a message that went out and was then deleted -- and
all four mean it left. Any other status raises rather than being guessed at.
The handle is the gateway's own `message_id`, which its edit, delete and
reaction endpoints take; the gateway keeps the Signal timestamp itself. So
`post_message` returns a `message_id` under this transport and a Signal
timestamp under the other, and the two are not interchangeable. The pending case raises its own type,
`SignalSendPending`, a sibling of the refusal rather than a kind of it,
because `except SignalGatewayError` is what a caller writes to mean "refused,
give up" and pending must not be caught by it. A read timeout after the request
was sent is pending for the same reason -- the gateway may well have queued the
message, so calling it a refusal would tell the caller not to retry something
in flight. The HTTP timeout therefore has to outlast `wait_s` by a margin,
checked when the config is read: a gateway answering a moment after its own
deadline is answering 202, and losing that to a socket timeout produces exactly
the confusion the distinction exists to avoid. The wait's cap is the gateway's,
30 seconds; a larger number in config is refused here rather than silently
clamped there.

The next run resubmits under the same content-derived idempotency key, which
waits on the message already in flight rather than sending a second copy. Two
bounds on that promise. It lasts as long as the gateway's `idempotency_ttl_s`,
24 hours by default, so an agent whose runs are further apart than the TTL gets
a fresh key for the same content and the resubmit does post a second copy. And
because the key covers the text and its styles with no run or time component, a
caller that deliberately sends identical text twice inside the TTL gets the
earlier message's outcome replayed -- `duplicate: true`, reported as delivered,
with nothing new sent. That is right for a retry and wrong for a repeat, and
the two are indistinguishable from the content alone, so the flag is written to
stderr rather than swallowed. A source that re-lists the same items every run
is the shape to watch.

**`label` mirrors the gateway's own setting and has to match it.** The prefix
is applied by the gateway from its `consumers.<name>.label`, and it re-checks
the length *after* prefixing, so those bytes come out of the same 2000. No
endpoint reports that value back, which makes this the one field here that
duplicates state we do not own: a label set there and omitted here means every
full-size part is refused with `422 too_long`, because the arithmetic looks
correct from this side. So a 422 naming `too_long` says that explicitly rather
than relaying the gateway's message — a mismatched label is the only thing that
explains it. A label longer here than there is merely wasteful. `limit` belongs
to the transport for this reason, and the footer is checked against the
transport's limit rather than Signal's.

**Signal takes plain text plus styled ranges, not markup.** Everything this
repo sends goes through `framework/signal_messenger.py::post_message`, which is
to Signal what `framework/slack.py` is to Slack: code that builds a message
writes Markdown, and one place renders it. `to_signal()` returns the text with
every marker removed and the `(start, length, STYLE)` ranges that reproduce
them, which go out as `--text-style`. Signal accepts BOLD, ITALIC,
STRIKETHROUGH, MONOSPACE and SPOILER, and rejects anything else with
`Invalid style:`. Headings become bold lines and list markers become bullets,
as on Slack. There is no link style — Signal linkifies bare URLs itself — so
`[label](url)` renders as `label (url)` and the URL stays visible.

**Offsets are UTF-16 code units, the limit is UTF-8 bytes.** signal-cli's own
help says the first in capitals, and it matters: an emoji is two units, so a
message with one ahead of a bold word would otherwise style the wrong letters.
The limit is the other unit — `ServiceConfig.MAX_MESSAGE_SIZE_BYTES` is 2000
and `ManagerImpl` tests it as `Utf8.size(messageText())` — so `_units()` counts
offsets, `_bytes()` counts capacity, and no function mixes them: in `_cut()`
the fit test is bytes while the offset it carries forward is units. Counting
capacity in units under-counts every non-ASCII character, and the bullet this
renderer writes is one unit and three bytes. That mistake is quiet rather than
loud: past 2000 bytes signal-cli does not refuse the message, it truncates the
text and attaches the remainder as a file, so the send succeeds and the reader
gets a cut message with the styled tail inside an attachment.

A message over Signal's 2000-byte limit is split into several in order,
numbered `(part 2 of 3)`, exactly as the Slack path splits on a block limit —
and for the same reason, since another message can arrive between the parts.
A style range cannot be reopened across messages, so the message is rendered
once and the text and its ranges are split together: a bold span that straddles
a boundary becomes a bold range at the end of one part and another at the start
of the next. A footer is appended after the rendering and never converted,
which is the contract the Slack path states — a standing compliance line means
the characters it was written with. A footer long enough to leave no room for
content is refused where the config is read, before the run has paid for a
fetch or an LLM call.

Two things this destination deliberately does not do. It has no `routes`: the
hot/firehose split is a Slack channel pattern, and a Signal group is somebody's
phone. And it does not take a digest — `digest` picks its transport at
construction, in Slack's shape, so sending one to Signal is a separate piece of
work.

### Secrets live in the environment, never in config

Each destination names the env var holding its credential (`webhook_env`,
`bot_token_env`, `api_key_env`). Real per-agent `config.json` files live in workspaces outside
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
