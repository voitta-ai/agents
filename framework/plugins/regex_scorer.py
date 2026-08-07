"""Regex/keyword scorer — the v1 reference Scorer implementation (issue #4).

Config-driven: a list of {pattern, points} rules; every rule whose regex matches
the signal text contributes its points. Negative points express spam/vendor
penalties. Multiplier rules apply a factor when they match, which is how the
industry-relevance boost is expressed.

Behaviour is deliberately equivalent to hq/leads/signals/signal_processor.py's
`score_signal()`, which is the reference for v1:

  - matching is **presence-based**, not occurrence-counted: a keyword appearing
    five times scores the same as appearing once (`if keyword in text`);
  - spam indicators carry negative points and are summed with the rest;
  - a single industry match multiplies the running total by 1.5, truncated to
    int, and matching several industry terms still multiplies only once;
  - the final score is floored at 0.

`match: count` is offered for agents that genuinely want "more mentions means a
stronger signal", but it is not the reference behaviour and is not the default.

An LLM-based scorer is the tracked future replacement (issue #9).

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import re

from framework.interfaces import ScoredSignal, Scorer, Signal
from framework.registry import register_scorer


class RegexScorer:
    def __init__(
        self,
        rules: list[dict],
        ignore_case: bool = True,
        match: str = "presence",
        multipliers: list[dict] | None = None,
        floor: int | None = 0,
    ):
        if match not in ("presence", "count"):
            raise ValueError(f"regex scorer: match must be 'presence' or 'count', got {match!r}")
        flags = re.IGNORECASE if ignore_case else 0
        self._match = match
        self._floor = floor
        self._rules = [
            (re.compile(r["pattern"], flags), int(r.get("points", 1))) for r in rules
        ]
        self._multipliers = [
            (re.compile(m["pattern"], flags), float(m.get("factor", 1.0)))
            for m in (multipliers or [])
        ]

    def score(self, signal: Signal) -> ScoredSignal:
        total = 0
        reasons: list[str] = []

        for pattern, points in self._rules:
            if self._match == "presence":
                if pattern.search(signal.text):
                    total += points
                    reasons.append(f"{pattern.pattern} (+{points})")
            else:
                hits = len(pattern.findall(signal.text))
                if hits:
                    total += hits * points
                    reasons.append(f"{pattern.pattern} x{hits} (+{hits * points})")

        # Multipliers apply once each, after the additive pass — matching the
        # reference, where any industry hit boosts the whole score by 1.5x.
        for pattern, factor in self._multipliers:
            if pattern.search(signal.text):
                total = int(total * factor)
                reasons.append(f"{pattern.pattern} (x{factor})")

        if self._floor is not None:
            total = max(self._floor, total)

        retval = ScoredSignal(signal=signal, score=total, reasons=reasons)
        return retval


@register_scorer("regex")
def build(cfg: dict) -> Scorer:
    retval = RegexScorer(
        rules=cfg.get("rules", []),
        ignore_case=cfg.get("ignore_case", True),
        match=cfg.get("match", "presence"),
        multipliers=cfg.get("multipliers"),
        floor=cfg.get("floor", 0),
    )
    return retval
