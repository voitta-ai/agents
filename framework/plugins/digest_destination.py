"""Digest destination: summarize a whole run into one message (issues #13, #20).

Buffers every signal in `send()` and emits a single LLM-written summary in
`flush()`, using the hook added in #26. This is the destination for agents whose
output is a narrative rather than a stream of items: the weekly release notes,
the daily community digest.

The prompt lives in config, not in code. What makes a good digest is a matter of
taste that gets tuned by reading real output, and a prompt in YAML can be
iterated without a release.

Two defaults chosen deliberately:

  - **A quiet run says so, in one line, and stops.** `empty_message` is posted
    when nothing came through, so silence is always distinguishable from
    breakage. A digest that pads to look busy trains you to stop reading it.
  - **The model that answered is named in the footer.** With a waterfall the
    quality varies by which model picked up, and a thin digest is worth reading
    differently if the primary was down.

`gate: true` (the default) is a labelling concern only — the digest is prefixed
as a draft for review. Actually publishing anywhere other than Slack is a
separate destination; this one only ever posts to a webhook.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os

import requests

from framework.interfaces import Destination, ScoredSignal
from framework.llm import LLMClient, LLMError, build_client
from framework.registry import register_destination

_DEFAULT_SYSTEM = (
    "You write short, factual internal digests. Lead with what matters. "
    "Omit anything routine. If little happened, say so plainly and stop — "
    "never pad to fill space."
)


class DigestDestination:
    def __init__(
        self,
        webhook_url: str,
        llm: LLMClient,
        prompt: str,
        system: str = _DEFAULT_SYSTEM,
        title: str = "Digest",
        empty_message: str = "Nothing new this run.",
        max_signals: int = 200,
        gate: bool = True,
        channel_label: str = "",
        timeout: int = 30,
    ):
        self.webhook_url = webhook_url
        self.llm = llm
        self.prompt = prompt
        self.system = system
        self.title = title
        self.empty_message = empty_message
        self.max_signals = max_signals
        self.gate = gate
        self.channel_label = channel_label
        self.timeout = timeout
        self._buffered: list[ScoredSignal] = []

    def send(self, scored: ScoredSignal) -> None:
        self._buffered.append(scored)

    def _render_items(self) -> str:
        lines = []
        for scored in self._buffered[: self.max_signals]:
            sig = scored.signal
            head = (sig.meta or {}).get("title") or sig.text.splitlines()[0]
            line = f"- {head}"
            if sig.url:
                line += f" ({sig.url})"
            body = sig.text.strip()
            if body and body != head:
                line += f"\n  {body[:800]}"
            lines.append(line)
        retval = "\n".join(lines)
        return retval

    def _post(self, text: str) -> None:
        resp = requests.post(
            self.webhook_url,
            json={"text": text},
            headers={"Content-type": "application/json"},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def flush(self) -> None:
        header = f"*{self.title}*"
        if self.gate:
            header += "  _(draft — review before publishing)_"

        if not self._buffered:
            self._post(f"{header}\n\n{self.empty_message}")
            return

        dropped = max(0, len(self._buffered) - self.max_signals)
        user = f"{self.prompt}\n\n---\n\n{self._render_items()}"
        try:
            summary = self.llm.complete(self.system, user)
        except LLMError as exc:
            # Surface the failure into the channel. Staying silent here would
            # look exactly like a quiet week.
            self._post(f"{header}\n\n:warning: digest not generated — {exc}")
            raise

        footer = f"_{len(self._buffered)} item(s) · {self.llm.model_used}_"
        if dropped:
            footer += f" · _{dropped} item(s) omitted past max_signals_"
        self._post(f"{header}\n\n{summary}\n\n{footer}")
        self._buffered.clear()


@register_destination("digest")
def build(cfg: dict) -> Destination:
    env_name = cfg.get("webhook_env")
    if not env_name:
        raise RuntimeError("digest destination needs a 'webhook_env'")
    webhook_url = os.environ.get(env_name)
    if not webhook_url:
        raise RuntimeError(f"digest destination needs the webhook URL in ${env_name}")

    prompt = cfg.get("prompt")
    if not prompt:
        raise RuntimeError("digest destination needs a 'prompt'")

    llm = build_client(cfg, what="digest destination")

    retval = DigestDestination(
        webhook_url=webhook_url,
        llm=llm,
        prompt=prompt,
        system=cfg.get("system", _DEFAULT_SYSTEM),
        title=cfg.get("title", "Digest"),
        empty_message=cfg.get("empty_message", "Nothing new this run."),
        max_signals=int(cfg.get("max_signals", 200)),
        gate=bool(cfg.get("gate", True)),
        channel_label=cfg.get("channel", ""),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
