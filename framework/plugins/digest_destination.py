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

`render()` (issue #21) is the same digest handed back as parts instead of
posted, so the `approval` destination can wrap this one and turn the digest
into a proposal. `flush()` is `render()` plus the post, and behaves exactly as
before when this destination is used on its own.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os

import requests

from framework.interfaces import Destination, ScoredSignal
from framework.llm import LLMClient, load_openclaw_chain, LLMError
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

    def render(self) -> dict:
        """Write the digest without posting it (issue #21).

        This is the hook the `approval` destination uses to turn a digest into
        a proposal: the same LLM call `flush()` makes, returned as parts rather
        than sent. `title` and `body_md` are the contract; `footer` carries the
        item count and the model that answered; `empty` is True when nothing
        came through this run, so a wrapper can tell a quiet run from a digest
        worth proposing. An LLM failure raises `LLMError`; the caller decides
        where to surface it. Does not clear the buffer, so it can be called more
        than once per run.
        """
        if not self._buffered:
            retval = {
                "title": self.title,
                "body_md": self.empty_message,
                "footer": "",
                "empty": True,
                "items": 0,
            }
            return retval

        dropped = max(0, len(self._buffered) - self.max_signals)
        user = f"{self.prompt}\n\n---\n\n{self._render_items()}"
        summary = self.llm.complete(self.system, user)

        footer = f"_{len(self._buffered)} item(s) · {self.llm.model_used}_"
        if dropped:
            footer += f" · _{dropped} item(s) omitted past max_signals_"
        retval = {
            "title": self.title,
            "body_md": summary,
            "footer": footer,
            "empty": False,
            "items": len(self._buffered),
            "model": self.llm.model_used,
        }
        return retval

    def flush(self) -> None:
        header = f"*{self.title}*"
        if self.gate:
            header += "  _(draft — review before publishing)_"

        try:
            rendered = self.render()
        except LLMError as exc:
            # Surface the failure into the channel. Staying silent here would
            # look exactly like a quiet week.
            self._post(f"{header}\n\n:warning: digest not generated — {exc}")
            raise

        if rendered["empty"]:
            self._post(f"{header}\n\n{rendered['body_md']}")
            return

        self._post(f"{header}\n\n{rendered['body_md']}\n\n{rendered['footer']}")
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

    # An explicit model chain in the agent's own config wins. Absent one, fall
    # back to OpenClaw's shared chain so there is a single waterfall to
    # maintain rather than one copy per agent.
    model_cfg = cfg.get("model") or {}
    primary = model_cfg.get("primary")
    fallbacks = model_cfg.get("fallbacks")
    if not primary:
        shared = load_openclaw_chain()
        if shared is None:
            raise RuntimeError(
                "digest destination needs 'model.primary', or an OpenClaw config "
                "at ~/.openclaw/openclaw.json providing agents.defaults.model"
            )
        primary, fallbacks = shared

    # Only OpenRouter entries need a key. A chain whose entries are all
    # `codex/...` runs on the ChatGPT subscription with no API key at all, which
    # is the main reason that transport exists -- so requiring the key up front
    # would defeat it. A missing key becomes a per-model failure at the point an
    # OpenRouter entry is actually attempted.
    key_env = cfg.get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(key_env)

    llm = LLMClient(
        api_key=api_key,
        primary=primary,
        fallbacks=fallbacks,
        timeout=int(cfg.get("llm_timeout", 120)),
        max_tokens=int(cfg.get("max_tokens", 2000)),
        temperature=float(cfg.get("temperature", 0.3)),
    )

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
