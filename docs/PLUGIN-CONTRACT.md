# Plugin construction contract (frozen — Wave 1 builds against this)

This is the contract that issue #1 freezes. Wave 1 issues (#2 Reddit source,
#3 Slack destination, #4 regex scorer) each implement one plugin against it,
independently. Do not change the signatures below without re-syncing all three.

## The three roles

Defined as Protocols in `framework/interfaces.py`:

| Role          | Method                               | Returns                 |
| ------------- | ------------------------------------ | ----------------------- |
| `Source`      | `fetch() -> list[Signal]`            | raw signals for one run |
| `Scorer`      | `score(signal: Signal) -> ScoredSignal` | signal + int score   |
| `Destination` | `send(scored: ScoredSignal) -> None` | delivery side effect    |

`Signal(id, text, url, source, meta)` — `id` is the dedupe key.
`ScoredSignal(signal, score, reasons)`.

## The construction contract

A plugin is a **factory** registered under a `type` name. The factory receives
**that component's entire config block** — the mapping under `source:` /
`scorer:` / `destination:` in the agent's `config.yaml`, including its own
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

Then register it for discovery by adding one import line to
`framework/plugins/__init__.py`:

```python
from framework.plugins import reddit_source  # noqa: F401
```

Signatures to register against:

```python
register_source(type_name: str)       # decorates  (cfg: dict) -> Source
register_scorer(type_name: str)       # decorates  (cfg: dict) -> Scorer
register_destination(type_name: str)  # decorates  (cfg: dict) -> Destination
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

## The run loop (already implemented, `framework/runner.py`)

```
signals = source.fetch()
for s in signals:
    if seen.has(s.id): continue
    destination.send(scorer.score(s))
    seen.mark(s.id)
seen.save()
return 0
```

Single pass, exits. Safe to double-run: `SeenStore` dedupes by `Signal.id`.
Cadence is declarative (`schedule:` in config) and wired into cron/launchd
externally — the framework runs no daemon.

## Invocation

```
agent run --config agents/<name>/config.yaml
# or, without installing the console script:
python -m framework run --config agents/<name>/config.yaml
```

Unknown `type:` fails fast with exit code 2 and lists what is registered.
