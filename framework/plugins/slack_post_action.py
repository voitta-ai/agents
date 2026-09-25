"""Slack-post action: post a templated line to Slack (issue #21).

The first action plugin, and the one that closes the loop on an approved
proposal: `Published: {url}` into the team channel once the publish step has
returned a `url`. One step in an action's `steps` list:

    {"type": "slack-post", "webhook_env": "SOME_WEBHOOK", "text": "Published: {url}"}

or, to post under the agent's own name through the Web API (issue #86):

    {"type": "slack-post", "bot_token_env": "SOME_BOT_TOKEN",
     "channel_id": "C0123456789", "text": "Published: {url}"}

`text` is rendered against the merged payload (the proposal's content plus
every earlier step's result) with `framework.templating.render_template`, so a
placeholder with no value fails the step rather than posting a blank. An
incoming webhook is bound to a channel when Slack issues it, which is why
`channel_id` belongs to the bot-token form only.

The line goes out through `framework.slack.post_message` like every other
Slack post here, so a `text` holding a Markdown link arrives clickable rather
than as literal brackets (issue #73). A plain line is posted unchanged.

Returns an empty dict. A webhook answers `ok` and nothing else; the Web API
answers with a message `ts`, but a line announcing a published post is not
something a later step edits, so nothing is published by name.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

from typing import Any

from framework.interfaces import Action
from framework.registry import register_action
from framework.slack import SlackTransport, post_message, transport_from_config
from framework.templating import render_template


class SlackPostAction:
    def __init__(self, transport: SlackTransport, text: str, timeout: int = 30):
        self._transport = transport
        self._text = text
        self._timeout = timeout

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        text = render_template(self._text, payload)
        post_message(self._transport, text, timeout=self._timeout)
        retval: dict[str, Any] = {}
        return retval


@register_action("slack-post")
def build(cfg: dict) -> Action:
    timeout = int(cfg.get("timeout", 30))
    transport = transport_from_config(cfg, "slack-post action", timeout)
    text = cfg.get("text")
    if not text:
        raise RuntimeError("slack-post action needs a 'text'")
    retval = SlackPostAction(
        transport=transport,
        text=str(text),
        timeout=timeout,
    )
    return retval
