"""Plugin implementations, imported for their registration side effects.

Each concrete plugin module calls `@register_source/scorer/destination(...)`
at import time. Add a new plugin by adding a row to `_PLUGIN_MODULES` below,
mapping its module name to the `type:` names it registers.

Wave 1 lands the first real plugins:
  - `reddit` source      (issue #2)
  - `slack` destination  (issue #3)
  - `regex` scorer       (issue #4)
  - `agentmail` source   (issue #12)

Later additions:
  - `github-org` source  (issue #13)
  - `digest` destination (issues #13, #20) - summarizes a run into one message
  - `slack-workspace` source (issue #19)
  - `ics` source         (issue #22)

Why a table rather than eight import statements (issue #39). Importing every
plugin unconditionally means one plugin's optional third-party dependency can
break the import of all of them, and therefore every agent - including agents
that never touch that plugin. Merging #35 did exactly that: `ics_source` does
`import icalendar` at module scope, the live venv did not have `icalendar`, and
the Reddit agent, which has nothing to do with calendars, died at startup.

So `import_all()` imports each module in isolation and tolerates `ImportError`,
recording which module failed and why. It deliberately does *not* catch
`Exception`: an optional dependency that is not installed is a known,
recoverable condition, whereas a plugin whose module-level code raises anything
else is broken and should stay loud.

Tolerating the failure here is only half of it. The registry turns a config
that names a failed plugin into an immediate, specific startup error (see
`framework/registry.py`), so tolerance never becomes silence for an agent that
actually asked for the plugin.

The table is what makes that error name the plugin rather than just the module:
a module that fails to import never reaches its `@register_source` call, so
after the failure nothing else in the process knows which `type:` names it was
going to claim. That knowledge has to be declared out-of-band, here.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass

_PLUGIN_MODULES: dict[str, tuple[str, ...]] = {
    "agentmail_source": ("agentmail",),
    "digest_destination": ("digest",),
    "github_org_source": ("github-org",),
    "ics_source": ("ics",),
    "reddit_source": ("reddit",),
    "regex_scorer": ("regex",),
    "slack_destination": ("slack",),
    "slack_workspace_source": ("slack-workspace",),
}


@dataclass(frozen=True)
class PluginImportFailure:
    """One plugin module that could not be imported, and why."""

    module: str
    types: tuple[str, ...]
    error: ImportError

    @property
    def qualified_module(self) -> str:
        retval = f"{__name__}.{self.module}"
        return retval

    def reason(self) -> str:
        retval = f"{type(self.error).__name__}: {self.error}"
        return retval


def import_all() -> dict[str, PluginImportFailure]:
    """Import every plugin module; return failures keyed by `type:` name.

    A module that imports cleanly has registered its factories by the time this
    returns. A module that raised `ImportError` contributes one entry per
    `type:` name it would have registered, so the registry can name the plugin
    the config asked for rather than only the module that broke.
    """
    retval: dict[str, PluginImportFailure] = {}
    for module, types in _PLUGIN_MODULES.items():
        try:
            importlib.import_module(f"{__name__}.{module}")
        except ImportError as exc:
            failure = PluginImportFailure(module=module, types=types, error=exc)
            for type_name in types:
                retval[type_name] = failure
    return retval
