"""Slack-draft action: draft from grounding on approval, post it for a human to use (issue #76).

The drafter (framework/drafter.py) normally runs at post time, under every
lead. Some drafts should cost a model call only when someone asks for one --
a job application, say, which is long and wanted for a few leads a week. This
step runs the same grounded drafter when the proposal is approved and posts
the result to Slack, where a person reads it, edits it and uses it by hand.
Nothing is submitted anywhere: the output of this step is a message.

    {"type": "slack-draft",
     "bot_token_env": "SOME_BOT_TOKEN",
     "channel_id": "C0123456789",
     "title": "Draft application: {title}",
     "drafter": {"prompt": "...", "fields": {...}, "grounding": ["..."]},
     "verbatim": [{"label": "Disclosure (include or omit: your call)",
                   "text": "..."}]}

Config keys:

    drafter   a drafter block, the same shape the slack destination takes:
              `prompt`, `fields`, `grounding` (read at build time, so a missing
              file fails `agent approvals` at startup rather than on a click),
              optional `max_grounding_chars`, `system`, `llm`.
    title     the message's first line, templated against the payload. Default
              "Draft: {title}". The payload's `url` follows it on its own line.
    verbatim  optional list of `{label, text}`, appended after the drafted
              fields exactly as written. For text that must never be
              paraphrased (a disclosure, a fixed sign-off): it does not go
              through the model, so the model cannot reword it, and showing it
              apart from the draft leaves including it a human decision.

The signal comes from the proposal's payload, as the approval destination's
signal mode records it (issue #65): `text`, `url`, `score`, `reasons`, `meta`.

A draft that fails fails the step, unlike at post time. There the lead is the
thing worth keeping, so a missing draft is a note under it; here the draft is
the whole job, and a click that produced nothing must say so. The approvals
pass reports the failure in the channel.

Returns `{"drafted": <field names, comma-separated>}`.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.drafter import drafter_inputs, drafter_with_llm
from framework.interfaces import Action, ScoredSignal, Signal
from framework.registry import register_action
from framework.slack import SlackTransport, post_message, transport_from_config
from framework.templating import render_template

_LABEL = "slack-draft action"


class SlackDraftAction:
    def __init__(
        self,
        transport: SlackTransport,
        block: dict,
        inputs: dict[str, Any],
        title: str,
        verbatim: list[dict[str, str]],
        timeout: int = 30,
    ):
        self._transport = transport
        self._block = block
        self._inputs = inputs
        self._title = title
        self._verbatim = verbatim
        self._timeout = timeout

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        workspace = context.get("workspace")
        root = Path(workspace).parent if workspace else None
        drafter = drafter_with_llm(self._block, self._inputs, _LABEL, workspace, root)
        meta = payload.get("meta")
        signal = Signal(
            id=str((meta or {}).get("id") or payload.get("url") or ""),
            text=str(payload.get("text") or ""),
            url=str(payload.get("url") or ""),
            source="proposal",
            meta=meta if isinstance(meta, dict) else {},
        )
        reasons = payload.get("reasons")
        scored = ScoredSignal(
            signal=signal,
            score=int(payload.get("score") or 0),
            reasons=list(reasons) if isinstance(reasons, list) else [],
        )
        drafted = drafter.draft(scored)

        parts = [f"**{render_template(self._title, payload)}**"]
        if signal.url:
            parts[0] += f"\n{signal.url}"
        for name, value in drafted.items():
            parts.append(f"*{name.replace('_', ' ')}*\n{value}")
        for item in self._verbatim:
            parts.append(f"*{item['label']}*\n{item['text']}")
        post_message(self._transport, "\n\n".join(parts), timeout=self._timeout)
        retval = {"drafted": ", ".join(drafted)}
        return retval


def _verbatim(cfg: dict) -> list[dict[str, str]]:
    items = cfg.get("verbatim") or []
    if not isinstance(items, list):
        raise RuntimeError(f"{_LABEL}: 'verbatim' must be a list of {{label, text}}")
    for item in items:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("label"), str) and item["label"].strip()
            and isinstance(item.get("text"), str) and item["text"].strip()
        ):
            raise RuntimeError(f"{_LABEL}: every 'verbatim' entry needs a non-empty 'label' and 'text'")
    retval = [{"label": i["label"], "text": i["text"]} for i in items]
    return retval


@register_action("slack-draft")
def build(cfg: dict) -> Action:
    timeout = int(cfg.get("timeout", 30))
    transport = transport_from_config(cfg, _LABEL, timeout)
    block = cfg.get("drafter")
    if not isinstance(block, dict):
        raise RuntimeError(f"{_LABEL} needs a 'drafter' block")
    retval = SlackDraftAction(
        transport=transport,
        block=block,
        inputs=drafter_inputs(block, _LABEL),
        title=str(cfg.get("title") or "Draft: {title}"),
        verbatim=_verbatim(cfg),
        timeout=timeout,
    )
    return retval
