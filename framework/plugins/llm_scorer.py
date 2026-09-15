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

  - **The model replies in JSON, and an unreadable reply skips that signal,
    loudly.** The scorer asks for `{"score": <int>, "reasons": [...]}` and
    takes the first JSON object carrying a `score` key from whatever comes
    back, tolerating a code fence, a sentence of preamble, or a second object
    after the answer. If no such object can be read, or `score` is not a finite
    number, it asks once more with the format restated, and if that reply is
    unreadable too it raises `LLMScoreError`. It does not quietly return 0: a 0
    gets the signal marked seen and never looked at again, which is a silent
    drop wearing a score.

    `LLMScoreError` is a `ScoreError`, which the runner handles per signal:
    that signal is neither sent nor marked seen, everything else in the run is
    delivered and deduped, the summary line counts it under `unscored=`, and
    the run exits 1 with `status=partial`. So one signal the model will not
    answer in JSON cannot silence the agent, and it is retried every run until
    it scores or leaves the feed. A dead chain is different: `LLMError` (every
    model failed) is not a `ScoreError` and still aborts the run, since it
    would fail every signal the same way.
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
import math

from framework.interfaces import ScoredSignal, ScoreError, Scorer, Signal
from framework.llm import LLMClient, build_client
from framework.registry import register_scorer

_DEFAULT_SYSTEM = (
    "You score short pieces of text for a specific purpose described by the "
    "user. Reply with a single JSON object and nothing else: "
    '{"score": <integer 0-100>, "reasons": [<one or two short strings>]}. '
    "0 means irrelevant, 100 means an unmistakable match. Be strict: most "
    "text is irrelevant and should score low."
)

_DECODER = json.JSONDecoder()


_RETRY_REMINDER = (
    "\n\n---\n\nYour previous reply could not be read. Reply with a single JSON "
    'object and nothing else: {"score": <integer 0-100>, "reasons": [<one or '
    "two short strings>]}."
)


class LLMScoreError(ScoreError):
    """The model answered, but not with a readable score.

    A `ScoreError`, so the runner skips this signal and carries on. `LLMError`
    from `framework.llm` (every model in the chain failed) deliberately is not:
    a dead chain fails every signal alike and must still abort the run.
    """


def _first_score_object(raw: str) -> dict | None:
    """Return the first JSON object in `raw` that has a `score` key, or None.

    Tries a decode at every `{` in turn and keeps the first one that parses to
    a dict carrying `score`. A regex spanning first-`{` to last-`}` would glue a
    valid answer to whatever object the model appended after it (a correction,
    an example, debug metadata) and fail the lot as "extra data".
    """
    retval = None
    pos = raw.find("{")
    while pos != -1 and retval is None:
        try:
            candidate, _end = _DECODER.raw_decode(raw, pos)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and "score" in candidate:
            retval = candidate
        pos = raw.find("{", pos + 1)
    return retval


def _parse_score(raw: str) -> tuple[int, list[str]]:
    """Read `{"score": n, "reasons": [...]}` out of a model reply.

    Takes the first JSON object in the reply that carries a `score` key, so a
    ```json fence, a line of preamble, or a second object after the answer are
    all tolerated: not every model in a free-tier waterfall follows "JSON only".
    Not tolerant of a missing, non-numeric or non-finite score, or of `reasons`
    that is neither a list nor a string; each raises `LLMScoreError`.
    """
    payload = _first_score_object(raw)
    if payload is None:
        raise LLMScoreError(f"no JSON object with a 'score' in model reply: {raw[:200]!r}")

    score = payload["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise LLMScoreError(f"model reply has no numeric 'score': {raw[:200]!r}")
    if isinstance(score, float) and not math.isfinite(score):
        raise LLMScoreError(f"model reply has a non-finite 'score': {raw[:200]!r}")

    raw_reasons = payload.get("reasons")
    if raw_reasons is None:
        raw_reasons = []
    elif isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    elif not isinstance(raw_reasons, list):
        raise LLMScoreError(f"model reply has non-list 'reasons': {raw[:200]!r}")
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
        # split, not splitlines()[0]: an empty body (a commit with no message)
        # has no first line, and that must not raise before the model is asked.
        first_line = body.split("\n", 1)[0]
        parts = [self.prompt, "", "---", ""]
        if title and title != first_line:
            parts.append(f"Title: {title}")
        parts.append(body)
        retval = "\n".join(parts)
        return retval

    def score(self, signal: Signal) -> ScoredSignal:
        user = self._render(signal)
        reply = self.llm.complete(self.system, user)
        try:
            score, reasons = _parse_score(reply)
        except LLMScoreError as first:
            # One retry, with the format restated. A model that drifted into
            # prose once often answers cleanly when told so; a second miss is
            # this signal's problem and goes to the runner as a ScoreError.
            reply = self.llm.complete(self.system, user + _RETRY_REMINDER)
            try:
                score, reasons = _parse_score(reply)
            except LLMScoreError as second:
                raise LLMScoreError(
                    f"unreadable reply after one retry; first: {first}; retry: {second}"
                ) from second
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
