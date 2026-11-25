#!/usr/bin/env python3
"""Gate the plugin surface and the shipped example configs against the code.

Three checks, run by CI and runnable by hand:

1. **Every plugin module imports.** `framework.plugins.import_all()` tolerates
   the failure of an *external* import, so a plugin whose third-party
   dependency is missing degrades to a recorded failure rather than breaking
   every agent (see that module's docstring). That tolerance is right at
   runtime and wrong here: CI installs every extra, so a recorded failure means
   a dependency the code imports is declared in no extra at all. Exactly the
   shape of the icalendar incident -- `ics_source` imported `icalendar`, the
   venv did not have it, and the Reddit agent, which has nothing to do with
   calendars, died at startup.

2. **The `_PLUGIN_MODULES` table agrees with the decorators.** The table maps
   each module to the `type:` names it registers, and it has to, because a
   module that fails to import never reaches its `@register_source` call and
   nothing afterwards knows what it would have claimed. Being declared
   out-of-band means it can drift, and drift is silent: a plugin that registers
   `wordpress-publish` under a table entry saying `wordpress` produces a
   "plugin not found" error naming a type that looks correct in the table.

3. **Every `type:` in every shipped example resolves.** The walk is recursive,
   so it reaches composites (`multi` children, `fanout` destinations, action
   steps) and underscore-prefixed inert blocks alike. The inert ones matter
   most: the framework's loader reads components by name and rejects no extra
   keys, so a `_llm_scorer_example` is never built, never validated, and can
   name a plugin that no longer exists for as long as nobody copies it.

What this does NOT check is that an example *runs*. Building a component needs
credentials and, for the drafter, grounding files that ship as placeholders by
design. The honest gate is that every name resolves; running an example end to
end is a hand-run against real config.
"""
from __future__ import annotations

import json
import pathlib
import sys

from framework import registry
from framework.plugins import _PLUGIN_MODULES, import_all

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def registered_names() -> set[str]:
    retval = (
        set(registry._sources)
        | set(registry._scorers)
        | set(registry._destinations)
        | set(registry._actions)
    )
    return retval


def walk_types(node: object, path: str, out: list[tuple[str, str]]) -> None:
    """Collect every (json path, type name) pair anywhere in a config."""
    if isinstance(node, dict):
        kind = node.get("type")
        if isinstance(kind, str):
            out.append((path or "<root>", kind))
        for key, value in node.items():
            walk_types(value, f"{path}.{key}", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk_types(value, f"{path}[{index}]", out)


def main() -> int:
    problems: list[str] = []

    failures = import_all()
    for type_name, failure in sorted(failures.items()):
        problems.append(
            f"plugin '{type_name}' ({failure.qualified_module}) did not import "
            f"with every extra installed: {failure.reason()}. Its dependency "
            f"belongs in an extra in pyproject.toml."
        )

    known = registered_names()
    declared = {name for names in _PLUGIN_MODULES.values() for name in names}
    for name in sorted(declared - known):
        problems.append(
            f"_PLUGIN_MODULES declares type '{name}', but no plugin registered "
            f"it. The table and the @register_* decorator disagree."
        )
    for name in sorted(known - declared):
        problems.append(
            f"a plugin registered type '{name}', but _PLUGIN_MODULES does not "
            f"declare it. A failed import of its module could not name it."
        )

    examples = sorted(REPO_ROOT.glob("agents/*/config.example.json"))
    if not examples:
        problems.append(
            f"no agents/*/config.example.json found under {REPO_ROOT}; this "
            f"gate would pass by finding nothing to check."
        )

    for example in examples:
        relative = example.relative_to(REPO_ROOT)
        try:
            raw = json.loads(example.read_text())
        except json.JSONDecodeError as exc:
            problems.append(f"{relative}: invalid JSON at line {exc.lineno}: {exc.msg}")
            continue
        found: list[tuple[str, str]] = []
        walk_types(raw, "", found)
        if not found:
            problems.append(f"{relative}: no 'type' key anywhere; is this a config?")
        for path, kind in found:
            if kind not in known:
                problems.append(f"{relative}: type '{kind}' at {path} is not a known plugin")

    print(f"plugins registered: {len(known)}")
    print(f"example configs:    {len(examples)}")
    if problems:
        print()
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
