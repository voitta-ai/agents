"""Multi source: several child sources behind one `source:` block (issue #57).

An agent whose subject matter is wider than any one feed needs more than one
source. The meetups agent is the case that forced it: chambers of commerce that
publish an iCal feed are reached with the `ics` source, and the ones that do not
are reached with `websearch` — the same agent, the same scorer, the same digest,
two ways in.

    "source": {
      "type": "multi",
      "sources": [
        {"name": "chambersmc", "type": "ics", "url_env": "...", "optional": true},
        {"name": "web", "type": "websearch", "queries": ["..."]}
      ]
    }

## Ids are prefixed, and that is the whole reason this is not a for-loop

Two children can mint the same `Signal.id`. Both the `ics` and the `websearch`
source can legitimately produce the id of the same chamber event — one from the
feed's UID, one from the event page's URL. Left alone, `SeenStore` would treat
the second as already seen and the run would silently drop it, which is this
repo's recurring failure shape. So every id becomes `f"{name}:{id}"`.

That makes the prefix part of the dedupe key, and therefore **renaming a child
re-delivers everything it produces**. `name` is a stable identifier, not a
label: it is documented as such here because the failure is silent and delayed
(one duplicated digest, once, long after the rename). A child without a `name`
is refused at build time rather than given a positional default, because a
positional default is exactly the thing that shifts when someone reorders the
list.

## One child's failure

`optional: true` is the whole of the policy. Without it, a child that raises
fails the run, naming the child — the default, because a feed that stops
answering is the failure this repo has been bitten by twice, and a source that
degrades quietly is worse than one that stops. With it, the child's error goes
to stderr and counts into a `degraded=` line, and the run continues on whatever
the other children produced.

`optional` belongs on a feed whose absence changes the digest's completeness but
not its point: a single chamber's calendar among a dozen search queries. It does
not belong on a child that is the only way the agent sees anything.

**Every child failing is not a quiet run**, even when all of them are optional:
`fetch()` raises, the same rule the reddit source applies to its feeds. A
wholly dead source must never render as "no events this week".

## Construction failures are never optional

`optional` covers `fetch()`, not `build()`. A child naming a plugin that does
not exist, or missing a required key, or missing the environment variable its
secret lives in, is a misconfiguration: it will fail identically on every run
until a human edits the config, so degrading past it would hide a broken agent
for as long as nobody reads stderr. Those raise at construction, before any
network call, naming the child.

## commit()

Defined unconditionally, and forwards to every child that has one, because the
runner discovers `commit` with getattr on the object it was handed — this one.
A child that tracks its own fetch window (`github-releases`) must still have it
advanced, and only the runner knows the run succeeded. A child without
`commit()` is skipped. An exception propagates: the runner's `commit_failed`
status exists for exactly this, and a window that silently failed to advance
would re-deliver forever.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import sys
from typing import Any

from framework.interfaces import Signal, Source
from framework.registry import build_source, register_source


class MultiSource:
    def __init__(self, children: list[tuple[str, bool, Source]]):
        # (name, optional, source) — order is the config's order, and the order
        # signals come out in, so a digest reads them in the order the operator
        # listed the feeds.
        self.children = children
        # Populated by fetch(), mirroring the reddit source's `feed_errors`, so
        # a caller (and a test) can see what degraded rather than only reading
        # stderr.
        self.child_errors: list[tuple[str, str]] = []

    def fetch(self) -> list[Signal]:
        self.child_errors = []
        signals: list[Signal] = []

        for name, optional, child in self.children:
            try:
                produced = child.fetch()
            except Exception as exc:
                detail = str(exc).replace("\n", " ")
                if not optional:
                    raise RuntimeError(
                        f"multi source: child {name!r} failed — {detail}. "
                        "Mark it 'optional': true if the agent should degrade "
                        "past it instead of failing."
                    ) from exc
                self.child_errors.append((name, detail))
                print(
                    f"multi source: degraded child={name} error={detail}",
                    file=sys.stderr,
                )
                continue
            for signal in produced:
                signals.append(self._prefixed(name, signal))

        # Every child failing is not a quiet run, however optional each one was.
        if self.children and len(self.child_errors) == len(self.children):
            detail = "; ".join(f"{name}: {why}" for name, why in self.child_errors[:3])
            raise RuntimeError(
                f"multi source: all {len(self.children)} child source(s) failed — {detail}"
            )

        if self.child_errors:
            print(
                f"multi source: degraded={len(self.child_errors)} "
                f"of={len(self.children)} child source(s)",
                file=sys.stderr,
            )

        retval = signals
        return retval

    def _prefixed(self, name: str, signal: Signal) -> Signal:
        """The child's signal with its id namespaced, and the child named in meta.

        A new Signal rather than a mutation: a child may hand back objects it
        also keeps, and `meta` is shared by reference until copied.
        """
        meta = dict(signal.meta or {})
        meta["multi_child"] = name
        retval = Signal(
            id=f"{name}:{signal.id}",
            text=signal.text,
            url=signal.url,
            source=signal.source,
            meta=meta,
        )
        return retval

    @property
    def dedupe(self) -> bool:
        """False when any child says so (issue #59).

        The runner's switch is per-run, not per-signal, so a `multi` holding one
        window-shaped child and one stream-shaped child cannot honour both. It
        resolves toward re-delivery: a duplicate in a digest is visible and
        recoverable, whereas a window silently emptied by the store is the
        failure `dedupe: false` exists to prevent, and it looks exactly like a
        quiet week. Mixing the two shapes under one `multi` is worth avoiding
        for that reason.
        """
        retval = all(bool(getattr(child, "dedupe", True)) for _n, _o, child in self.children)
        return retval

    def commit(self) -> None:
        for name, _optional, child in self.children:
            commit = getattr(child, "commit", None)
            if callable(commit):
                commit()


def _child_cfg(parent: dict, declared: dict) -> dict[str, Any]:
    """A child's config block, with the parent's injected run-level keys carried in.

    `agent run` injects `state_dir` into the *top-level* source block only
    (`framework/cli.py`), because the registry hands a factory nothing but its
    own block. A child built from a nested block would therefore not see it, and
    a `github-releases` child would have nowhere to keep its fetch window. So
    the injected keys are copied down, and a child that sets one itself keeps
    its own value.
    """
    retval = dict(declared)
    for key in ("state_dir", "_workspace", "_root"):
        if key in parent and key not in retval:
            retval[key] = parent[key]
    return retval


@register_source("multi")
def build(cfg: dict) -> Source:
    declared = cfg.get("sources")
    if not isinstance(declared, list) or not declared:
        raise RuntimeError(
            "multi source needs a non-empty 'sources' list, each entry a source "
            "block with its own 'type' and a 'name'"
        )

    children: list[tuple[str, bool, Source]] = []
    names: set[str] = set()
    for index, child_declared in enumerate(declared):
        if not isinstance(child_declared, dict):
            raise RuntimeError(
                f"multi source: sources[{index}] is {type(child_declared).__name__}, "
                "not a source block"
            )
        name = str(child_declared.get("name") or "").strip()
        if not name:
            raise RuntimeError(
                f"multi source: sources[{index}] (type "
                f"{child_declared.get('type')!r}) needs a 'name'. It prefixes every "
                "id this child produces, so it is part of the dedupe key — a "
                "positional default would shift if the list were reordered and "
                "silently re-deliver everything."
            )
        if ":" in name:
            raise RuntimeError(
                f"multi source: child name {name!r} must not contain ':' — the "
                "prefix is joined to the child's id with one, and a name that "
                "contains it makes the boundary ambiguous"
            )
        if name in names:
            raise RuntimeError(
                f"multi source: duplicate child name {name!r}. Names namespace "
                "ids, so two children sharing one would dedupe against each other"
            )
        names.add(name)

        optional = bool(child_declared.get("optional", False))
        # A construction failure is a misconfiguration, never degradable: it
        # would fail the same way on every run until a human fixes the config.
        try:
            child = build_source(_child_cfg(cfg, child_declared))
        except Exception as exc:
            raise RuntimeError(
                f"multi source: child {name!r} (type {child_declared.get('type')!r}) "
                f"could not be built — {exc}. 'optional' covers a failed fetch, not "
                "a config that cannot be built at all."
            ) from exc
        children.append((name, optional, child))

    retval = MultiSource(children=children)
    return retval
