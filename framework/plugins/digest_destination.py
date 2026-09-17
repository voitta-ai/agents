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

`fields` (issue #63) is what an approved *multi-step* action needs. A digest
that only ever produces one blob of prose gives an action sequence exactly one
template name, `{body_md}`, so a release-notes action that publishes the post
and then posts a blurb to a social account has nowhere to get the blurb from
and fails at that step, after the post is already published. With `fields`, the
model is asked for one JSON object with those keys instead of prose, and every
key becomes a template name for the action's steps:

    "fields": {
      "title":         "A post title, under 70 characters.",
      "body_md":       "The post itself, Markdown.",
      "blurb_twitter": "Under 240 characters, plain text, no hashtags."
    }

The values are descriptions written for the model, not defaults. `body_md` is
required among them, because it is what `render()` promises its caller and what
the proposal's Slack message shows. `empty`, `footer`, `items` and `model` are
refused: `render()` sets those for its caller, and a drafted string landing on
`empty` would tell the approval destination there was nothing to propose,
losing a digest that had already been written. Without a `fields` key nothing
changes: one prose summary, in `body_md`.

A reply that is not a JSON object carrying every configured key raises
`DigestFieldError` after one retry, and the run ends without recording a
proposal. Half a release announcement is worse than none: the next run asks
again with the same signals, because nothing was marked seen.

`meta_values` (issue #75) is the other half of that. A drafted field is the
model's prose, and prose is the wrong source for a tag list: a post's taxonomy
must not change because the model phrased its answer differently. So `render()`
also returns values the destination computed itself from the buffered signals,
not from the reply. The first is `repos`: the distinct `meta["repo"]` values of
the items the model was shown, comma-separated, which for the release-notes
agent is exactly the repos that released this run. They appear twice in the
rendered result — once collected under `meta_values`, for a caller that wants to
know which values were computed rather than drafted, and once as plain top-level
keys, because an action's steps template against the proposal payload by bare
name (`framework/templating.py` refuses `{meta_values[repos]}` for reaching into
a value) and `{repos}` has to resolve. A field name that collides with one of
them is refused at build time rather than shadowed, so a config cannot end up
with a taxonomy the model wrote.

`min_score` (issue #63) drops low-scoring signals before the model ever sees
them, for agents whose scorer is a filter rather than a ranking. Dropped items
are not in the item count either, so a run that gates everything out reads as
the quiet run it was. Signals are still marked seen: the gate says "not worth
summarizing", not "look at this again next run". A gated run also shows the
model each item's score and the scorer's reasons; an ungated one does not, so
a digest that was running before this change reads exactly the prompt it
always did.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
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

_DECODER = json.JSONDecoder()

# Keys `render()` sets for its caller, which a drafted field must never
# overwrite. `empty` is the dangerous one: a truthy `empty` tells the approval
# destination there is nothing to propose, so a real digest would be posted
# and dropped, its signals marked seen, and the run would report `ok` having
# silently lost drafted content. `title` and `body_md` are absent on purpose:
# drafting them is the point, and `title`'s override is handled.
_RESERVED_FIELDS = frozenset({"empty", "footer", "items", "model"})

# Keys `render()` computes from the signals themselves (issue #75). Reserved for
# the same reason as the ones above, and one more: these exist precisely so a
# taxonomy can come from something the model never wrote, which a drafted field
# of the same name would quietly undo.
_COMPUTED_FIELDS = frozenset({"meta_values", "repos"})


class DigestFieldError(RuntimeError):
    """The model answered, but not with a JSON object carrying every field.

    Not an `LLMError`: every rung of the waterfall answered, so the chain is
    healthy and reporting it as a dead chain would send an operator after the
    wrong thing. `flush()` still surfaces it to the channel the same way.
    """


def _fields_instruction(fields: dict[str, str]) -> str:
    """The JSON-only contract appended to the system prompt."""
    lines = [f'  "{name}": {description}' for name, description in fields.items()]
    body = "\n".join(lines)
    retval = (
        "Reply with a single JSON object and nothing else -- no code fence, no "
        "commentary before or after it. The object has exactly these keys, each "
        "a string:\n"
        f"{body}\n"
        "Every key must be present and non-empty."
    )
    return retval


_RETRY_REMINDER = (
    "\n\n---\n\nYour previous reply could not be read. Reply with one JSON object "
    "and nothing else: no code fence, no text before or after it, every key "
    "present, every value a non-empty string."
)


def _first_object_with(raw: str, names: list[str]) -> dict | None:
    """First JSON object in `raw` that carries every name in `names`, or None.

    Tries a decode at each `{` rather than spanning first-`{` to last-`}`: a
    model that appends a second object (a correction, an example) must not
    turn one readable answer into unparseable "extra data". Same reading the
    llm scorer does for its `score` object.
    """
    retval = None
    pos = raw.find("{")
    while pos != -1 and retval is None:
        try:
            candidate, _end = _DECODER.raw_decode(raw, pos)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and all(name in candidate for name in names):
            retval = candidate
        pos = raw.find("{", pos + 1)
    return retval


def _parse_fields(raw: str, fields: dict[str, str]) -> dict[str, str]:
    """Read the configured fields out of a model reply, or raise.

    Every key has to be there and has to be a non-empty string. A field that
    came back as an empty string is the failure this is here to catch: it
    templates into a step as nothing at all, and an approved action would post
    a blank blurb rather than fail.
    """
    names = list(fields)
    payload = _first_object_with(raw, names)
    if payload is None:
        raise DigestFieldError(
            f"no JSON object with the keys {', '.join(names)} in model reply: {raw[:300]!r}"
        )
    retval = {}
    for name in names:
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise DigestFieldError(
                f"field {name!r} came back as {type(value).__name__} "
                f"{str(value)[:80]!r}, not a non-empty string"
            )
        retval[name] = value.strip()
    return retval


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
        fields: dict[str, str] | None = None,
        min_score: int = 0,
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
        self.fields = fields or {}
        self.min_score = min_score
        self._buffered: list[ScoredSignal] = []

    def send(self, scored: ScoredSignal) -> None:
        if scored.score < self.min_score:
            return
        self._buffered.append(scored)

    def _render_items(self) -> str:
        """The item list the model reads.

        Scores and the scorer's reasons appear only when `min_score` gates the
        run. A config with a gate is demonstrably using scores for something,
        and showing them lets the model say which items cleared the bar; a
        config without one gets the list it has always got, rather than a
        prompt newly seasoned with whatever its scorer writes into `reasons`
        ("matched keyword X", for the regex scorer).
        """
        show_scores = self.min_score > 0
        lines = []
        for scored in self._buffered[: self.max_signals]:
            sig = scored.signal
            head = (sig.meta or {}).get("title") or sig.text.splitlines()[0]
            line = f"- [{scored.score}] {head}" if show_scores else f"- {head}"
            if sig.url:
                line += f" ({sig.url})"
            if show_scores and scored.reasons:
                line += f"\n  why: {'; '.join(scored.reasons)}"
            body = sig.text.strip()
            if body and body != head:
                line += f"\n  {body[:800]}"
            lines.append(line)
        retval = "\n".join(lines)
        return retval

    def _meta_values(self) -> dict[str, str]:
        """Values computed from the buffered signals, for taxonomy and the like.

        `repos`: the distinct `meta["repo"]` values, in first-seen order,
        comma-separated. Taken from the same `max_signals` slice the model is
        shown, so the tags on a post always describe the items in it; an item
        omitted past `max_signals` is not in the prose either. A source whose
        signals carry no `repo` yields an empty string, which the WordPress
        action drops rather than turning into an empty tag.
        """
        repos: list[str] = []
        for scored in self._buffered[: self.max_signals]:
            repo = str((scored.signal.meta or {}).get("repo") or "").strip()
            if repo and repo not in repos:
                repos.append(repo)
        retval = {"repos": ", ".join(repos)}
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
        meta_values = self._meta_values()
        if not self._buffered:
            retval = {
                "title": self.title,
                "body_md": self.empty_message,
                "footer": "",
                "empty": True,
                "items": 0,
                "meta_values": meta_values,
                **meta_values,
            }
            return retval

        dropped = max(0, len(self._buffered) - self.max_signals)
        user = f"{self.prompt}\n\n---\n\n{self._render_items()}"
        if self.fields:
            drafted = self._complete_fields(user)
        else:
            drafted = {"body_md": self.llm.complete(self.system, user)}

        footer = self._extras_block(drafted)
        footer += f"_{len(self._buffered)} item(s) · {self.llm.model_used}_"
        if dropped:
            footer += f" · _{dropped} item(s) omitted past max_signals_"
        retval = {
            "title": self.title,
            "body_md": drafted["body_md"],
            "footer": footer,
            "empty": False,
            "items": len(self._buffered),
            "model": self.llm.model_used,
            "meta_values": meta_values,
        }
        # The configured fields are the template names an approved action's
        # steps render against, so they go in whole, after the keys above:
        # a field named `title` is the model's post title and replaces the
        # static config one, which is a label for the Slack message.
        retval.update(drafted)
        # After the drafted fields, so the order states the precedence: a
        # computed value is never overwritten by a model-written one. Build-time
        # validation refuses that collision outright, and this is the belt.
        retval.update(meta_values)
        return retval

    def _complete_fields(self, user: str) -> dict[str, str]:
        """Ask for the configured fields as one JSON object; one retry, then raise."""
        system = f"{self.system}\n\n{_fields_instruction(self.fields)}"
        reply = self.llm.complete(system, user)
        try:
            retval = _parse_fields(reply, self.fields)
        except DigestFieldError as first:
            # Same one-retry rule the llm scorer uses: a model that drifted
            # into prose once usually complies when told so, and a second miss
            # is a real failure rather than a bad roll.
            reply = self.llm.complete(system, user + _RETRY_REMINDER)
            try:
                retval = _parse_fields(reply, self.fields)
            except DigestFieldError as second:
                raise DigestFieldError(
                    f"unreadable reply after one retry; first: {first}; retry: {second}"
                ) from second
        return retval

    def _extras_block(self, drafted: dict[str, str]) -> str:
        """The drafted fields other than `body_md`, shown under the digest.

        A human approving a proposal has to see everything it will publish, not
        just the part that happens to be the post. Named first, then quoted in
        full, and the whole block sits in `footer` so that `body_md` stays
        exactly what an action's publish step will send.
        """
        extras = [name for name in self.fields if name not in ("body_md", "title")]
        if not extras:
            retval = ""
            return retval
        parts = [f"*also drafted:* {', '.join(extras)}"]
        for name in extras:
            parts.append(f"*{name}*\n{drafted[name]}")
        retval = "\n\n".join(parts) + "\n\n"
        return retval

    def flush(self) -> None:
        header = f"*{self.title}*"
        if self.gate:
            header += "  _(draft — review before publishing)_"

        try:
            rendered = self.render()
        except (LLMError, DigestFieldError) as exc:
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

    fields = cfg.get("fields")
    if fields is not None:
        if not isinstance(fields, dict) or not fields:
            raise RuntimeError(
                "digest destination's 'fields' must be a non-empty object of "
                "name -> description (the description is written for the model)"
            )
        bad = [name for name, desc in fields.items() if not isinstance(desc, str) or not desc]
        if bad:
            raise RuntimeError(
                f"digest destination's fields {', '.join(sorted(bad))} need a "
                "non-empty description string saying what the model should write"
            )
        if "body_md" not in fields:
            raise RuntimeError(
                "digest destination's 'fields' must include 'body_md': it is what "
                "render() promises its caller and what the digest message shows"
            )
        clashes = sorted(name for name in fields if name in _RESERVED_FIELDS)
        if clashes:
            raise RuntimeError(
                f"digest destination's fields {', '.join(clashes)} would overwrite "
                "render()'s own keys; rename them. Reserved: "
                f"{', '.join(sorted(_RESERVED_FIELDS))} "
                "('title' and 'body_md' are yours to draft)"
            )
        computed = sorted(name for name in fields if name in _COMPUTED_FIELDS)
        if computed:
            raise RuntimeError(
                f"digest destination's fields {', '.join(computed)} are computed "
                "from the signals themselves, so asking the model for them would "
                "shadow a value it cannot get wrong; rename them. Computed: "
                f"{', '.join(sorted(_COMPUTED_FIELDS))}"
            )

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
        fields=fields,
        min_score=int(cfg.get("min_score", 0)),
    )
    return retval
