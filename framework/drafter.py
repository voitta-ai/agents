"""A second, grounded generation pass over an already-scored signal (issue #83).

The scorer answers "is this a lead". A drafter answers "what would we say", and
those are different jobs that must not share a prompt:

  - A score is a judgement about the signal. A draft is a **claim about us** --
    what we can do, what we have done, what we would offer. Claims need
    provenance; a judgement does not.
  - So a drafter is fed from named files and told to use nothing else. The
    scorer's prompt describes a posture; the drafter's inputs are documents.

That separation is the whole design, and it is why this is not another `fields`
block on the scorer. A model asked to score and pitch in one call will reach for
its own knowledge of the company to do the second half, and that is exactly the
failure a grounded drafter exists to prevent: a plausible sentence about a
capability nobody has.

## Grounding

`grounding` is a list of file paths, read at **build** time and concatenated
with a header naming each file. Build time rather than per-signal so a missing
or unreadable file fails the run at startup, where it is a misconfiguration,
rather than silently producing an ungrounded draft on signal seventeen.

The total is capped by `max_grounding_chars`; exceeding it raises rather than
truncating, because a silently truncated grounding set drops whichever document
sorts last and nothing says so.

## Failure is never silent, and never costs the lead

`draft()` raises `DraftError` after one retry. The caller is expected to keep
the signal and say the draft is missing -- the lead itself is the thing worth
having, and dropping a real lead because a second model call failed would be a
bad trade. What must not happen is a lead that *looks* complete while carrying
no draft, so the reason is rendered where the draft would have been.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from framework.interfaces import ScoredSignal
from framework.llm import LLMClient, build_client

_DECODER = json.JSONDecoder()

DEFAULT_SYSTEM = (
    "You draft outreach from supplied source documents only. Every capability, "
    "result or number you state must appear in those documents. If something is "
    "not in them, leave it out rather than inferring it -- an unsupported claim "
    "is worse than a shorter draft."
)

_RETRY_REMINDER = (
    "\n\n---\n\nYour previous reply could not be read. Reply with one JSON object "
    "and nothing else: no code fence, no text before or after it, every key "
    "present, every value a non-empty string."
)


class DraftError(RuntimeError):
    """The model answered, but not with a usable object.

    Not an `LLMError`: the chain is healthy. The caller surfaces it next to the
    signal rather than failing the run, because one unusable draft must not cost
    an otherwise good lead.
    """


def _first_object_with(raw: str, names: list[str]) -> dict | None:
    """First JSON object in `raw` carrying every name. Same reading the llm
    scorer and digest destination do: decode at each `{` rather than spanning
    first-to-last, so a model that appends a second object does not turn one
    readable answer into unparseable extra data."""
    retval = None
    pos = raw.find("{")
    while pos != -1 and retval is None:
        try:
            candidate, _end = _DECODER.raw_decode(raw, pos)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and all(n in candidate for n in names):
            retval = candidate
        pos = raw.find("{", pos + 1)
    return retval


def load_grounding(paths: list[str], max_chars: int, label: str) -> str:
    """Read every grounding file, or raise naming the one that failed."""
    if not paths:
        raise RuntimeError(f"{label}: 'grounding' must name at least one file")
    parts: list[str] = []
    total = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"{label}: cannot read grounding file {str(path)!r}: {exc}. "
                "Every claim in a draft comes from these files, so a missing one "
                "is a misconfiguration, not a degraded run."
            ) from exc
        if not text:
            raise RuntimeError(f"{label}: grounding file {str(path)!r} is empty")
        total += len(text)
        parts.append(f"--- SOURCE: {path.name} ---\n{text}")
    if total > max_chars:
        raise RuntimeError(
            f"{label}: grounding is {total} characters, over max_grounding_chars "
            f"({max_chars}). Raise the cap or name fewer files -- truncating would "
            "silently drop whichever document sorts last."
        )
    retval = "\n\n".join(parts)
    return retval


class Drafter:
    def __init__(
        self,
        llm: LLMClient,
        grounding: str,
        prompt: str,
        fields: dict[str, str],
        system: str = DEFAULT_SYSTEM,
        max_signal_chars: int = 4000,
    ):
        self.llm = llm
        self.grounding = grounding
        self.prompt = prompt
        self.fields = fields
        self.system = system
        self.max_signal_chars = max_signal_chars

    def _instruction(self) -> str:
        lines = [f'  "{name}": {desc}' for name, desc in self.fields.items()]
        retval = (
            "Reply with a single JSON object and nothing else -- no code fence, no "
            "commentary before or after it. The object has exactly these keys, each "
            "a string:\n" + "\n".join(lines) + "\nEvery key must be present and non-empty."
        )
        return retval

    def _user(self, scored: ScoredSignal) -> str:
        sig = scored.signal
        meta: dict[str, Any] = sig.meta or {}
        bits = [self.prompt, "", "--- SOURCE DOCUMENTS (the only permitted basis for claims) ---",
                self.grounding, "", "--- THE POST ---",
                f"url: {sig.url}", f"score: {scored.score}"]
        if scored.reasons:
            bits.append("why it scored: " + "; ".join(scored.reasons))
        rules = meta.get("subreddit_rules")
        if rules:
            # Shown so the draft can obey the community's own rule rather than a
            # generalisation about what communities usually allow.
            bits.append(f"subreddit rules that bear on self-promotion:\n{rules}")
        bits.append(sig.text[: self.max_signal_chars])
        retval = "\n".join(bits)
        return retval

    def draft(self, scored: ScoredSignal) -> dict[str, str]:
        system = f"{self.system}\n\n{self._instruction()}"
        user = self._user(scored)
        reply = self.llm.complete(system, user)
        try:
            retval = self._parse(reply)
        except DraftError as first:
            reply = self.llm.complete(system, user + _RETRY_REMINDER)
            try:
                retval = self._parse(reply)
            except DraftError as second:
                raise DraftError(
                    f"unreadable reply after one retry; first: {first}; retry: {second}"
                ) from second
        return retval

    def _parse(self, raw: str) -> dict[str, str]:
        names = list(self.fields)
        payload = _first_object_with(raw, names)
        if payload is None:
            raise DraftError(
                f"no JSON object with the keys {', '.join(names)} in model reply: {raw[:200]!r}"
            )
        retval = {}
        for name in names:
            value = payload[name]
            if not isinstance(value, str) or not value.strip():
                raise DraftError(
                    f"field {name!r} came back as {type(value).__name__} "
                    f"{str(value)[:60]!r}, not a non-empty string"
                )
            retval[name] = value.strip()
        return retval


def drafter_inputs(block: dict, label: str) -> dict[str, Any]:
    """A `drafter` block's validated inputs, grounding already read.

    Everything a `Drafter` needs except its model client, which wants the
    workspace and root that only the run (or, for an action, the approvals
    pass) knows. Split out so a caller that learns those later can still fail
    on a bad block, or an unreadable grounding file, when it is built.
    """
    prompt = block.get("prompt")
    if not prompt or not isinstance(prompt, str):
        raise RuntimeError(f"{label}: 'drafter' needs a 'prompt'")

    fields = block.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise RuntimeError(
            f"{label}: 'drafter' needs 'fields', an object of name -> "
            "description written for the model"
        )
    bad = sorted(n for n, d in fields.items() if not isinstance(d, str) or not d.strip())
    if bad:
        raise RuntimeError(f"{label}: drafter fields {', '.join(bad)} need a non-empty description")

    grounding_paths = block.get("grounding")
    if not isinstance(grounding_paths, list) or not all(
        isinstance(p, str) and p.strip() for p in grounding_paths
    ):
        raise RuntimeError(
            f"{label}: 'drafter' needs 'grounding', a non-empty list of file "
            "paths. A draft makes claims about us, and claims need provenance"
        )
    grounding = load_grounding(
        grounding_paths,
        int(block.get("max_grounding_chars", 60000)),
        f"{label} drafter",
    )
    retval = {
        "grounding": grounding,
        "prompt": prompt,
        "fields": fields,
        "system": block.get("system") or DEFAULT_SYSTEM,
        "max_signal_chars": int(block.get("max_signal_chars", 4000)),
    }
    return retval


def drafter_with_llm(
    block: dict, inputs: dict[str, Any], label: str, workspace: Any, root: Any
) -> Drafter:
    """A `Drafter` from `drafter_inputs()`, with its model client built now.

    The drafter's own chain if the block names one, else the deployment's
    waterfall (framework/llm.py).
    """
    llm_cfg = dict(block)
    if workspace and "_workspace" not in llm_cfg:
        llm_cfg["_workspace"] = str(workspace)
    if root and "_root" not in llm_cfg:
        llm_cfg["_root"] = str(root)
    llm = build_client(llm_cfg, what=f"{label} drafter")
    retval = Drafter(llm=llm, **inputs)
    return retval


def build_drafter(block: dict, label: str, workspace: Any, root: Any) -> Drafter:
    """Validate a `drafter` block, read its grounding and build its client."""
    retval = drafter_with_llm(block, drafter_inputs(block, label), label, workspace, root)
    return retval
