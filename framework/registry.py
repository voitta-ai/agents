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
the `framework.plugins` package to trigger those registrations.
"""
from __future__ import annotations

from typing import Callable

from framework.interfaces import Destination, Scorer, Source

SourceFactory = Callable[[dict], Source]
ScorerFactory = Callable[[dict], Scorer]
DestinationFactory = Callable[[dict], Destination]

_sources: dict[str, SourceFactory] = {}
_scorers: dict[str, ScorerFactory] = {}
_destinations: dict[str, DestinationFactory] = {}


class UnknownPluginError(Exception):
    """Config referenced a `type:` with no registered factory.

    Deliberately not a KeyError subclass: KeyError.__str__ wraps its message
    in repr() quotes, which would garble this user-facing CLI error (the exact
    message every Wave-1 dev sees before their plugin is registered).
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


def load_plugins() -> None:
    """Import the plugins package so its modules self-register.

    Import is deferred (not top-of-file) so the registry module itself stays
    import-cheap and free of plugin dependencies.
    """
    import framework.plugins  # noqa: F401  (import for side effect: registration)
