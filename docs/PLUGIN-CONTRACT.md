# Plugin construction contract (frozen — Wave 1 builds against this)

This is the contract that issue #1 freezes. Wave 1 issues (#2 Reddit source,
#3 Slack destination, #4 regex scorer) each implement one plugin against it,
independently. Do not change the signatures below without re-syncing all three.

## The roles

Defined as Protocols in `framework/interfaces.py`:

| Role          | Method                               | Returns                 |
| ------------- | ------------------------------------ | ----------------------- |
| `Source`      | `fetch() -> list[Signal]`            | raw signals for one run |
| `Scorer`      | `score(signal: Signal) -> ScoredSignal` | signal + int score   |
| `Destination` | `send(scored: ScoredSignal) -> None` | delivery side effect    |
| `Action`      | `execute(payload: dict, context: dict) -> dict` | results for later steps |

`Action` is the fourth kind, added for approval-gated steps (issue #21). It is
not part of the `agent run` loop: a destination records a proposal, and a
separate `agent approvals` pass runs the proposal's action steps once a trusted
human has replied `do <id>`. `payload` is the proposal content merged with the
results of earlier steps; `context` carries run-level facts such as the
workspace path and proposal id. The returned dict is merged into the payload
the next step sees, so a step exposes values by name (`{url}`) to those after it.
Register with `register_action(type_name)`; the CLI builds steps with
`build_action(cfg)`, where `cfg` is one entry of an action's `steps` list.

`Signal(id, text, url, source, meta)` — `id` is the dedupe key.
`ScoredSignal(signal, score, reasons)`.

## The construction contract

A plugin is a **factory** registered under a `type` name. The factory receives
**that component's entire config block** — the mapping under `source:` /
`scorer:` / `destination:` in the agent workspace's `config.json`, including its own
`type` key — and returns an instance implementing the role's Protocol.

```python
# framework/plugins/reddit_source.py   (issue #2)
from framework.interfaces import Signal, Source
from framework.registry import register_source


class RedditSource:
    def __init__(self, subreddits: list[str]):
        self.subreddits = subreddits

    def fetch(self) -> list[Signal]:
        ...


@register_source("reddit")
def build(cfg: dict) -> Source:
    # cfg is the whole `source:` block: {"type": "reddit", "subreddits": [...]}
    return RedditSource(subreddits=cfg["subreddits"])
```

Then register it for discovery by adding one row to `_PLUGIN_MODULES` in
`framework/plugins/__init__.py`, mapping the module name to the `type:` names
it registers:

```python
_PLUGIN_MODULES: dict[str, tuple[str, ...]] = {
    ...
    "reddit_source": ("reddit",),
}
```

### Import failures are per-plugin (issue #39)

`load_plugins()` imports each module in that table separately and tolerates a
failed import of an *external* dependency. A plugin whose third-party package is
not installed does not prevent the others from registering, so an agent using
only `reddit` + `regex` + `slack` still runs on a venv missing `icalendar`.

Tolerated is not silent. Every failed module is reported on stderr at startup,
and if a config names a type that a failed module would have registered,
`build_source` / `build_scorer` / `build_destination` / `build_action` raise `PluginImportError`
immediately, quoting the original `ImportError`:

```
agent: source type 'ics' is provided by plugin module
'framework.plugins.ics_source', which failed to import:
ModuleNotFoundError: No module named 'icalendar'
```

Two consequences for plugin authors:

- **Declare your types in the table.** A module that fails to import never
  reaches its `@register_source` call, so the table is the only place that
  knows which types it owns. Get the row wrong and the error message for a
  missing dependency degrades to a plain unknown-type error.
- **Only an external `ImportError` is tolerated.** The test is
  `ImportError.name`: a module outside `framework` is an optional dependency and
  is recorded, while anything under `framework` — a mistyped `register_source`
  import, a sibling module deleted in a refactor — propagates and fails the
  whole load. Module-level code raising anything other than `ImportError` fails
  the load too. A plugin broken by a refactor is not the same thing as a plugin
  missing an optional package, and must not be quiet just because no agent in
  the current config happens to use it.

Adding a plugin with a new third-party dependency means adding it to
`pyproject.toml` *and* reinstalling the venv the live agents run from
(`.venv/bin/pip install -e .`). This change contains the blast radius of
forgetting; it does not remove the step.

Signatures to register against:

```python
register_source(type_name: str)       # decorates  (cfg: dict) -> Source
register_scorer(type_name: str)       # decorates  (cfg: dict) -> Scorer
register_destination(type_name: str)  # decorates  (cfg: dict) -> Destination
register_action(type_name: str)       # decorates  (cfg: dict) -> Action
```

## Rules for Wave 1

- **Read only your own block.** The factory gets its component's dict and
  nothing else. Cross-component fields do not exist at construction time; if a
  plugin needs a value, it lives in that plugin's own config block.
- **No shared schema.** Each plugin owns the keys under its block. `subreddits`
  is the Reddit source's business; `routes`/`workspace` are the Slack
  destination's; there is deliberately no central schema to coordinate on.
- **Routing/tiering is the destination's job, not the framework's.** The runner
  calls `destination.send()` for every non-duplicate scored signal; the Slack
  destination decides channel-by-score (hot vs firehose) and whether to drop
  below-threshold signals. See `routes` in the reddit-leads example config.
- **Fetch has no side effects beyond fetching.** Scoring is pure. Delivery is
  the only place external writes happen.

## Optional hooks: `Destination.flush()` and `Source.commit()`

Neither is part of its Protocol; the runner discovers both with `getattr`, so
plugins that do not need them are unaffected.

- `flush()` on a destination is called once after the last `send()` (issue
  #26). Batching and digest destinations buffer in `send()` and deliver here.
- `commit()` on a source is called once after `seen.save()` has succeeded
  (issue #46). It is for a source whose fetch window is its own state — the
  `github-releases` source keeps a `last_run` timestamp so that "what shipped
  since we last looked" is an explicit question rather than an over-fetch that
  SeenStore trims. Advance that window in `commit()`, never in `fetch()`: a run
  that fetched and then failed to deliver never reaches `commit()`, so the next
  run re-fetches the same window and SeenStore drops what did get through.
  At-least-once, never a silent skip. It is not called on a `partial` run
  either: an unscored signal is left unmarked so the next run retries it, and
  that only works if the next run's window still contains it.

## The run loop (already implemented, `framework/runner.py`)

```
signals = source.fetch()
for s in signals:
    if seen.has(s.id): continue
    try:
        scored = scorer.score(s)
    except ScoreError:
        unscored += 1          # not sent, not marked: retried next run
        continue
    destination.send(scored)
    pending.append(s.id)
destination.flush()            # only if the destination defines it
for id in pending: seen.mark(id)
seen.save()
if not unscored:
    source.commit()            # only if the source defines it; never on partial
return 1 if unscored else 0
```

Single pass, exits. Safe to double-run: `SeenStore` dedupes by `Signal.id`.
Every run prints one summary line,
`agent=<name> status=<ok|partial|commit_failed|failed> fetched=N new=N sent=N unscored=N seen=N`.

### Scorer errors: one signal, or the whole run

A scorer that cannot score *this* signal, while others may still score fine,
raises `framework.interfaces.ScoreError` (or a subclass, such as the LLM
scorer's `LLMScoreError` for a reply it could not read even after a retry). The
runner skips that signal: it is not sent and not marked seen, so the next run
retries it, and one stderr line names its id and the error. The rest of the run
is delivered and deduped normally, then the run reports `status=partial` and
exits 1.

Any other exception from `score()` aborts the run with `status=failed` and
nothing marked seen. Raise something other than `ScoreError` when the failure
would hit every signal alike: a missing credential, or every model in the chain
being down (`framework.llm.LLMError`, deliberately not a `ScoreError`).
Cadence is declarative (`schedule:` in config) and wired into cron/launchd
externally — the framework runs no daemon.

## Invocation

```
agent run --workspace <workspaces-root>/<name>
# or, without installing the console script:
python -m framework run --workspace <workspaces-root>/<name>
```

The workspace holds `config.json`; dedupe state defaults to
`<workspace>/state/seen.json` (`--state` overrides). `--config` is gone and
exits 2 naming `--workspace`.

Unknown `type:` fails fast with exit code 2 and lists what is registered.
