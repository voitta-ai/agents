"""Plugin implementations, imported for their registration side effects.

Each concrete plugin module calls `@register_source/scorer/destination(...)`
at import time. Add new plugins by importing them here so `load_plugins()`
picks them up.

Wave 1 lands the first real plugins:
  - `reddit` source      (issue #2)
  - `slack` destination  (issue #3)
  - `regex` scorer       (issue #4)
  - `agentmail` source   (issue #12)

Later additions:
  - `github-org` source  (issue #13)
  - `digest` destination (issues #13, #20) — summarizes a run into one message
  - `slack-workspace` source (issue #19)
  - `ics` source         (issue #22)
"""
from __future__ import annotations

from framework.plugins import agentmail_source  # noqa: F401
from framework.plugins import digest_destination  # noqa: F401
from framework.plugins import github_org_source  # noqa: F401
from framework.plugins import ics_source  # noqa: F401
from framework.plugins import reddit_source  # noqa: F401
from framework.plugins import regex_scorer  # noqa: F401
from framework.plugins import slack_destination  # noqa: F401
from framework.plugins import slack_workspace_source  # noqa: F401
