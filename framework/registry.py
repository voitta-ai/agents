"""Plugin registry: resolve config `type:` strings to constructed plugins.

This module freezes the plugin-construction contract that Wave 1 builds
against (Reddit source #2, Slack destination #3, regex scorer #4). See
docs/PLUGIN-CONTRACT.md for the prose version.

The contract, in one sentence: a plugin registers a factory under a `type`
name; the factory receives that component's whole config block (the mapping
under `source:` / `scorer:` / `destination:`, including its own `type` key)
and returns an instance implementing the matching Protocol.

    @register_source("reddit")
    def build(cfg: dict) -> Source:
        return RedditSource(subreddits=cfg["subreddits"])

Factories are populated by importing plugin modules; `load_plugins()` imports
them to trigger those registrations.

Import of a plugin module can fail on a missing optional dependency, and that
must not take down plugins an agent actually uses (issue #39). `load_plugins()`
therefore imports each module in isolation, keeps the ones that worked and
remembers the ones that did not. The tolerance stops here: `build_source` /
`build_scorer` / `build_destination` raise `PluginImportError` the moment a
config names a plugin whose module failed, quoting the original `ImportError`.

Loud where it matters, tolerant where it does not - an agent on `reddit` +
`regex` + `slack` runs on a venv without `icalendar`, and an agent configured
for `ics` on that same venv fails at startup naming the missing package,
rather than mid-run at first use.
"""
from __future__ import annotations

from typing import Callable

from framework.interfaces import Destination, Scorer, Source
from framework.plugins import PluginImportFailure, import_all

SourceFactory = Callable[[dict], Source]
ScorerFactory = Callable[[dict], Scorer]
DestinationFactory = Callable[[dict], Destination]

_sources: dict[str, SourceFactory] = {}
_scorers: dict[str, ScorerFactory] = {}
_destinations: dict[str, DestinationFactory] = {}

# `type:` name -> the plugin module whose import failed, populated by
# load_plugins(). Empty until it runs, and empty afterwards on a complete venv.
_failed_plugins: dict[str, PluginImportFailure] = {}


class UnknownPluginError(Exception):
    """Config referenced a `type:` with no registered factory.

    Deliberately not a KeyError subclass: KeyError.__str__ wraps its message
    in repr() quotes, which would garble this user-facing CLI error (the exact
    message every Wave-1 dev sees before their plugin is registered).
    """


class PluginImportError(Exception):
    """Config named a plugin whose module could not be imported.

    Distinct from `UnknownPluginError` because the two call for different
    fixes: an unknown type is a typo or an unregistered plugin, while this is
    an installed-dependency problem with a known remedy, and the message says
    which package and which plugin.
    """


def register_source(type_name: str) -> Callable[[SourceFactory], SourceFactory]:
    def decorator(factory: SourceFactory) -> SourceFactory:
        _sources[type_name] = factory
        return factory

    return decorator


def register_scorer(type_name: str) -> Callable[[ScorerFactory], ScorerFactory]:
    def decorator(factory: ScorerFactory) -> ScorerFactory:
        _scorers[type_name] = factory
        return factory

    return decorator


def register_destination(type_name: str) -> Callable[[DestinationFactory], DestinationFactory]:
    def decorator(factory: DestinationFactory) -> DestinationFactory:
        _destinations[type_name] = factory
        return factory

    return decorator


def _build(kind: str, registry: dict, cfg: dict):
    type_name = cfg.get("type")
    factory = registry.get(type_name)
    if factory is None:
        failure = _failed_plugins.get(type_name)
        if failure is not None:
            raise PluginImportError(
                f"{kind} type {type_name!r} is provided by plugin module "
                f"{failure.qualified_module!r}, which failed to import: "
                f"{failure.reason()}"
            )
        known = ", ".join(sorted(registry)) or "(none registered)"
        raise UnknownPluginError(
            f"unknown {kind} type {type_name!r}; registered: {known}"
        )
    retval = factory(cfg)
    return retval


def build_source(cfg: dict) -> Source:
    retval = _build("source", _sources, cfg)
    return retval


def build_scorer(cfg: dict) -> Scorer:
    retval = _build("scorer", _scorers, cfg)
    return retval


def build_destination(cfg: dict) -> Destination:
    retval = _build("destination", _destinations, cfg)
    return retval


def load_plugins() -> dict[str, PluginImportFailure]:
    """Import the plugin modules so they self-register.

    Returns the plugins that could not be imported, keyed by the `type:` name
    each would have registered, so the caller can report them. The same map is
    kept here, so a later `build_*` for one of those types can explain itself.

    The import is done through `framework.plugins.import_all()` rather than a
    plain package import, so one plugin's missing optional dependency costs
    only that plugin (issue #39).
    """
    _failed_plugins.clear()
    _failed_plugins.update(import_all())
    retval = dict(_failed_plugins)
    return retval
