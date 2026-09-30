"""Fanout destination: one run, several destinations (issue #57).

The same digest to two Slack channels, because two audiences want it and neither
should have to join the other's channel.

    "destination": {
      "type": "fanout",
      "destinations": [
        {"type": "digest", "webhook_env": "HOOK_A", "channel": "#a", "prompt": "..."},
        {"type": "digest", "webhook_env": "HOOK_B", "channel": "#b", "prompt": "..."}
      ]
    }

## Every child sees every signal, and a digest child drafts once per child

`send()` forwards to each child; `flush()` calls each child's, in order. Two
`digest` children therefore make two LLM calls and write two summaries of the
same run, which will not be word-for-word identical.

That is accepted rather than worked around, and the reason is cost: at a weekly
cadence it is two calls a week. Sharing one draft between children would mean
either reaching into a child's buffer — the fanout does not know what kind of
child it holds, and must not — or a new protocol method for "render once, post
many", which is wider than the problem. **A config whose two channels must show
byte-identical text should not use fanout**; it should post once and let Slack
share the message.

## A failure in one child does not spare the others

`send()` and `flush()` both attempt **every** child before raising, then raise
naming all the failures. Stopping at the first would mean a dead webhook on the
first child silently costing the second child its digest, with the run reporting
one failure that looks like one problem.

The ordering matters for `flush()` in particular: that is where a digest child
does its real posting, so "attempt them all" is the difference between one
channel missing a digest and both missing it.

This is the same reasoning as issue #82's per-step `on_error` for approval
actions, reached independently: one dead channel must not cancel the others.

**A partial failure still fails the run.** The exception propagates after every
child has been attempted, so the runner reports `failed`, nothing is marked seen,
and the next run re-delivers — to every child, including the ones that already
got it. At-least-once, matching the framework's stated bias (`framework/runner.py`):
a duplicate digest is recoverable, a missing one is not noticed. A config that
would rather keep the successful children's delivery than re-send it has no
option here, deliberately — that is `on_error` semantics, and it belongs in one
place for actions and destinations alike rather than being invented here first.

## Children are built with the parent's injected keys

`agent run` injects `_workspace` and `_root` into the top-level destination
block, and anything built on `framework.llm` needs them — which every `digest`
child is. The registry hands a factory only its own block, so a nested child
would not see them and would fail to build its LLM client. They are copied down.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

from typing import Any

from framework.interfaces import Destination, ScoredSignal
from framework.registry import build_destination, register_destination


class FanoutDestination:
    def __init__(self, children: list[tuple[str, Destination]]):
        # (label, destination). The label is for error messages only — unlike a
        # multi source's child name it is not part of any dedupe key, because a
        # destination mints no ids.
        self.children = children

    def send(self, scored: ScoredSignal) -> None:
        self._each("send", lambda child: child.send(scored))

    def flush(self) -> None:
        def flush_one(child: Destination) -> None:
            child_flush = getattr(child, "flush", None)
            if callable(child_flush):
                child_flush()

        self._each("flush", flush_one)

    def _each(self, what: str, call) -> None:
        """Call `call` on every child, then raise if any of them failed.

        Attempting all of them before raising is the whole point: the first
        child's dead webhook must not cost the second child its digest.
        """
        failures: list[tuple[str, str]] = []
        for label, child in self.children:
            try:
                call(child)
            except Exception as exc:
                failures.append((label, f"{type(exc).__name__}: {exc}".replace("\n", " ")))
        if failures:
            detail = "; ".join(f"{label}: {why}" for label, why in failures)
            raise RuntimeError(
                f"fanout destination: {len(failures)} of {len(self.children)} "
                f"child destination(s) failed on {what}() — {detail}"
            )


def _child_cfg(parent: dict, declared: dict) -> dict[str, Any]:
    """A child's block with the parent's injected run-level keys carried in.

    See `framework/cli.py`: `_workspace` and `_root` are injected into the
    top-level destination block, and every `digest` child needs them to build
    its LLM client. A child that sets one itself keeps its own value.
    """
    retval = dict(declared)
    for key in ("_workspace", "_root"):
        if key in parent and key not in retval:
            retval[key] = parent[key]
    return retval


def _label(declared: dict, index: int) -> str:
    """How a child is named in an error message.

    `channel` first because it is what an operator recognises, then the
    destination type, then the position. No `name` key is required the way the
    multi source requires one: nothing here is a dedupe key, so there is no
    silent-redelivery trap to guard against.
    """
    channel = str(declared.get("channel") or "").strip()
    kind = str(declared.get("type") or "?")
    retval = f"{kind}[{index}] {channel}".strip() if channel else f"{kind}[{index}]"
    return retval


@register_destination("fanout")
def build(cfg: dict) -> Destination:
    declared = cfg.get("destinations")
    if not isinstance(declared, list) or not declared:
        raise RuntimeError(
            "fanout destination needs a non-empty 'destinations' list, each entry "
            "a destination block with its own 'type'"
        )

    children: list[tuple[str, Destination]] = []
    for index, child_declared in enumerate(declared):
        if not isinstance(child_declared, dict):
            raise RuntimeError(
                f"fanout destination: destinations[{index}] is "
                f"{type(child_declared).__name__}, not a destination block"
            )
        label = _label(child_declared, index)
        try:
            child = build_destination(_child_cfg(cfg, child_declared))
        except Exception as exc:
            raise RuntimeError(
                f"fanout destination: child {label} could not be built — {exc}"
            ) from exc
        children.append((label, child))

    retval = FanoutDestination(children=children)
    return retval
