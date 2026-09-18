"""Slack-post action: post a templated line to a webhook (issue #21).

The first action plugin, and the one that closes the loop on an approved
proposal: `Published: {url}` into the team channel once the publish step has
returned a `url`. One step in an action's `steps` list:

    {"type": "slack-post", "webhook_env": "SOME_WEBHOOK", "text": "Published: {url}"}

`text` is rendered against the merged payload (the proposal's content plus
every earlier step's result) with `framework.templating.render_template`, so a
placeholder with no value fails the step rather than posting a blank. The
webhook is bound to a channel when Slack issues it, so there is no channel
key here.

The line goes out through `framework.slack.post_message` like every other
Slack post here, so a `text` holding a Markdown link arrives clickable rather
than as literal brackets (issue #73). A plain line is posted unchanged.

Returns an empty dict: an incoming webhook answers `ok` and nothing a later
step could use.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
from typing import Any

from framework.interfaces import Action
from framework.registry import register_action
from framework.slack import post_message
from framework.templating import render_template


class SlackPostAction:
    def __init__(self, webhook_url: str, text: str, timeout: int = 30):
        self._webhook_url = webhook_url
        self._text = text
        self._timeout = timeout

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        text = render_template(self._text, payload)
        post_message(self._webhook_url, text, timeout=self._timeout)
        retval: dict[str, Any] = {}
        return retval


@register_action("slack-post")
def build(cfg: dict) -> Action:
    env_name = cfg.get("webhook_env")
    if not env_name:
        raise RuntimeError("slack-post action needs a 'webhook_env'")
    webhook_url = os.environ.get(env_name)
    if not webhook_url:
        raise RuntimeError(f"slack-post action needs the webhook URL in ${env_name}")
    text = cfg.get("text")
    if not text:
        raise RuntimeError("slack-post action needs a 'text'")
    retval = SlackPostAction(
        webhook_url=webhook_url,
        text=str(text),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
