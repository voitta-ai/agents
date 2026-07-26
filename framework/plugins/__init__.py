"""Plugin implementations, imported for their registration side effects.

Each concrete plugin module calls `@register_source/scorer/destination(...)`
at import time. Add new plugins by importing them here so `load_plugins()`
picks them up.

Wave 1 lands the first real plugins:
  - `reddit` source      (issue #2)  -> from . import reddit_source
  - `slack` destination  (issue #3)  -> from . import slack_destination
  - `regex` scorer       (issue #4)  -> from . import regex_scorer

Until those land, no `type:` resolves and `agent run` reports the unknown
type with the (empty) registered list — which is the intended frozen-contract
behavior, not a bug.
"""
from __future__ import annotations

# Wave 1 plugin imports go here, one per line, e.g.:
# from framework.plugins import reddit_source  # noqa: F401
