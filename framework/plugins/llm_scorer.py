"""LLM scorer: score a signal by asking a model, through the waterfall (issue #9).

The regex scorer is presence-based keyword matching, which is cheap and exact but
cannot tell "I hate our spreadsheet process" from "I hate how much I love my
spreadsheet". This scorer asks a model the question the agent actually cares
about — the `prompt` in config — and reads back a number on the same 0..100
scale the regex rules produce, so the Slack destination's `min_score` routes and
every other consumer keep working unchanged.

The model chain comes from `framework.llm`, exactly as the digest destination's
does: the agent's own `model:` block if it has one, otherwise OpenClaw's shared
`agents.defaults.model`. There is one waterfall in this repo, not two.

Three decisions worth knowing before configuring it:

  - **The model replies in JSON, and an unreadable reply fails the run.** The
    scorer asks for `{"score": <int>, "reasons": [...]}` and extracts the first
    JSON object from whatever comes back, tolerating a code fence or a sentence
    of preamble. If no object can be read, or `score` is not a number, it raises
    `LLMScoreError`. It does not quietly return 0: a 0 gets the signal marked
    seen and never looked at again, which is a silent drop wearing a score. A
    raised error leaves the run's dedupe unmarked, so the next run re-scores
    (issue #26's at-least-once bias). Loud beats lossy.
  - **Text is truncated to `max_chars` before it is sent.** Reddit posts run to
    thousands of words; scoring needs the first few hundred. The default keeps
    each call small and predictable on metered fallbacks.
  - **Temperature defaults to 0.** Scoring wants the same number for the same
    text; creativity is the digest's job.

Cost and latency: one model call per new signal. On the `codex/` subscription
transport that is a CLI process per signal, several seconds each, which is fine
at the fetch sizes and schedules the deployed agents run at (25 signals every
few hours) and would not be for a firehose. Nothing here batches signals into
one call; that is a deliberate non-feature until an agent needs it.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
import re

from framework.interfaces import ScoredSignal, Scorer, Signal
from framework.llm import LLMClient, build_client
from framework.registry import register_scorer

_DEFAULT_SYSTEM = (
    "You score short pieces of text for a specific purpose described by the "
    "user. Reply with a single JSON object and nothing else: "
    '{"score": <integer 0-100>, "reasons": [<one or two short strings>]}. '
    "0 means irrelevant, 100 means an unmistakable match. Be strict: most "
    "text is irrelevant and should score low."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class LLMScoreError(RuntimeError):
    """The model answered, but not with a readable score."""


def _parse_score(raw: str) -> tuple[int, list[str]]:
    """Read `{"score": n, "reasons": [...]}` out of a model reply.

    Tolerant of a ```json fence or a line of preamble, since not every model in
    a free-tier waterfall follows "JSON only". Not tolerant of a missing or
    non-numeric score, which is a failure the caller must hear about.
    """
    match = _JSON_OBJECT.search(raw)
    if match is None:
        raise LLMScoreError(f"no JSON object in model reply: {raw[:200]!r}")
    try:
        payload = json.loads(match.group(0))
    except ValueError as exc:
        raise LLMScoreError(f"model reply is not valid JSON ({exc}): {raw[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise LLMScoreError(f"model reply is not a JSON object: {raw[:200]!r}")

    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise LLMScoreError(f"model reply has no numeric 'score': {raw[:200]!r}")

    raw_reasons = payload.get("reasons") or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    reasons = [str(r) for r in raw_reasons if str(r).strip()]

    retval = (int(score), reasons)
    return retval


class LLMScorer:
    def __init__(
        self,
        llm: LLMClient,
        prompt: str,
        system: str = _DEFAULT_SYSTEM,
        max_chars: int = 4000,
        floor: int = 0,
        ceiling: int = 100,
    ):
        self.llm = llm
        self.prompt = prompt
        self.system = system
        self.max_chars = max_chars
        self.floor = floor
        self.ceiling = ceiling

    def _render(self, signal: Signal) -> str:
        title = (signal.meta or {}).get("title")
        body = signal.text.strip()
        if len(body) > self.max_chars:
            body = body[: self.max_chars] + " [truncated]"
        parts = [self.prompt, "", "---", ""]
        if title and title != body.splitlines()[0]:
            parts.append(f"Title: {title}")
        parts.append(body)
        retval = "\n".join(parts)
        return retval

    def score(self, signal: Signal) -> ScoredSignal:
        reply = self.llm.complete(self.system, self._render(signal))
        score, reasons = _parse_score(reply)
        score = max(self.floor, min(self.ceiling, score))
        reasons.append(f"model={self.llm.model_used}")
        retval = ScoredSignal(signal=signal, score=score, reasons=reasons)
        return retval


@register_scorer("llm")
def build(cfg: dict) -> Scorer:
    prompt = cfg.get("prompt")
    if not prompt:
        raise RuntimeError("llm scorer needs a 'prompt' saying what to score for")

    llm = build_client(
        cfg,
        default_max_tokens=200,
        default_temperature=0.0,
        what="llm scorer",
    )
    retval = LLMScorer(
        llm=llm,
        prompt=prompt,
        system=cfg.get("system", _DEFAULT_SYSTEM),
        max_chars=int(cfg.get("max_chars", 4000)),
        floor=int(cfg.get("floor", 0)),
        ceiling=int(cfg.get("ceiling", 100)),
    )
    return retval
