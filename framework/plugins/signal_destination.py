"""Signal destination — one scored signal, one Signal message (issue #120).

```json
"destination": {
  "type": "signal-messenger",
  "account_env": "AGENTS_SIGNAL_ACCOUNT",
  "group_id_env": "AGENTS_SIGNAL_GROUP_LEADS",
  "min_score": 10
}
```

The type is `signal-messenger`, not `signal`. The `approval` destination
already has a `mode: "signal"` meaning one proposal per `Signal` — the
dataclass every source emits — and a destination called `signal` would read as
that one, in configs where both appear together.

Deliberately narrow next to the Slack destination:

- **One target, no routes.** Routes exist for the hot/firehose split, which is
  a Slack channel pattern. A Signal group is somebody's phone.
- **Streaming only.** No batch mode, because the digest destination is the
  thing that summarises a run, and sending Signal a digest is a separate piece
  of work — `digest` picks its transport at construction, in Slack's shape.
- **No drafter.** The drafted block belongs to the agent that needs it; this
  renders what it is given.

What it does carry over is `accepts()` and `render_signal()`, so the `approval`
destination's signal mode can wrap it and a lead looks the same whether it is
posted plainly or proposed for approval.
"""
from __future__ import annotations

from framework.interfaces import Destination, ScoredSignal
from framework.registry import register_destination
from framework.signal_messenger import (
    Transport,
    check_footer,
    post_message,
    transport_from_config,
)

_LABEL = "signal-messenger destination"


class SignalDestination:
    def __init__(
        self,
        transport: Transport,
        min_score: int = 0,
        footer: str = "",
        show_reasons: bool = True,
    ):
        self.transport = transport
        self.min_score = min_score
        self.footer = footer
        self.show_reasons = show_reasons

    def _format(self, scored: ScoredSignal) -> str:
        """One signal as Markdown, which the transport renders for Signal.

        The same shape the Slack destination streams, for the same reason: the
        score first because it is what a reader triages on, then the signal's
        own words, then its link, then — set apart and in italics — the
        scorer's reasons, which are ours about them rather than theirs.
        """
        sig = scored.signal
        text = f"**[{scored.score}]** {sig.text}"
        if sig.url:
            text += f"\n{sig.url}"
        if self.show_reasons and scored.reasons:
            text += "\n\n" + "\n".join(f"- _{reason}_" for reason in scored.reasons)
        retval = text
        return retval

    def accepts(self, scored: ScoredSignal) -> bool:
        """Whether this signal clears the gate. No send, no model call."""
        retval = scored.score >= self.min_score
        return retval

    def render_signal(self, scored: ScoredSignal) -> dict | None:
        """The message this would send, without sending it (issue #65).

        The footer is kept apart because the `approval` destination posts
        through its own transport and appends it itself.
        """
        if not self.accepts(scored):
            return None
        retval = {"text": self._format(scored), "footer": self.footer}
        return retval

    def send(self, scored: ScoredSignal) -> None:
        if not self.accepts(scored):
            return
        post_message(self.transport, self._format(scored), footer=self.footer)


@register_destination("signal-messenger")
def build(cfg: dict) -> Destination:
    timeout = int(cfg.get("timeout", 120))
    transport = transport_from_config(cfg, _LABEL, timeout)

    show_reasons = cfg.get("show_reasons", True)
    if not isinstance(show_reasons, bool):
        raise RuntimeError(f"{_LABEL}: 'show_reasons' must be true or false")

    footer = cfg.get("footer", "")
    if not isinstance(footer, str):
        raise RuntimeError(f"{_LABEL}: 'footer' must be a string")
    # Here rather than only at send time: a footer too long to leave room for a
    # message is knowable before the run fetches, scores or asks a model
    # anything, and left unchecked it turns one scored signal into thousands of
    # one-character Signal messages. The same guard `slack_destination` makes.
    # Against the transport's own limit, not Signal's: a gateway that puts a
    # label in front of every message has fewer bytes to give the footer, and
    # a check against the constant would pass a footer the run then cannot fit.
    check_footer(footer, f"{_LABEL}: 'footer'", limit=transport.limit)

    retval = SignalDestination(
        transport=transport,
        min_score=int(cfg.get("min_score", 0)),
        footer=footer,
        show_reasons=show_reasons,
    )
    return retval
