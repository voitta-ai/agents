"""Twitter (X) post action (issue #48).

One step of an approval-gated sequence: after a post has been published, announce
it with a tweet. Configured as a `steps` entry of an action:

    {"type": "twitter-post", "text": "{blurb_twitter} {url}"}

`text` is rendered with `framework.templating.render_template` against the
step's payload, so values produced by earlier steps (`{url}` from the publishing
step, say) and the proposal's own fields are available by name. Only plain
names are accepted: a field that reaches into a value (`{url.__class__}`,
`{result[0]}`) is refused before anything is sent, so a template cannot pull an
object's internals into a public tweet. A template that names a key the
payload does not carry is an error naming that key, not an empty substitution:
a tweet announcing "None" is worse than no tweet.

The rendered text is checked against the 280-character limit *before* any
request goes out, and a longer text is an error, not a truncation. Truncating
would post something the human did not approve. The check is a plain `len()`;
X's own weighted count (URLs count as 23, some scripts as 2 per character) is
not reproduced here. A text that passes `len()` and still exceeds X's count is
rejected by the API with a 403, which is just as loud.

Posting goes through the X API v2 (`POST /2/tweets`) with OAuth 1.0a user
context: the tweet appears on the account that owns the access token. The
signing is done by `requests-oauthlib`, an optional dependency (`pip install
'.[twitter]'`). It is imported at module scope on purpose: the plugin loader
(issue #40) tolerates a missing external package here, keeps every other plugin
working, and turns a config that names `twitter-post` on a venv without the
package into a startup error quoting the missing module.

Credentials are read from the environment only. The config names the variables;
the defaults are the four an X developer app hands out:

    api_key_env:        TWITTER_API_KEY        (consumer key)
    api_secret_env:     TWITTER_API_SECRET     (consumer secret)
    access_token_env:   TWITTER_ACCESS_TOKEN
    access_secret_env:  TWITTER_ACCESS_SECRET

They are read in `execute`, not at construction, so a sequence whose earlier
steps do not need Twitter is built and started regardless, and the approval
runner reports *this* step as the one waiting on credentials. A missing variable
raises naming it. Nothing in this module logs a header, a request body or a
token at any level. `timeout` (seconds, default 30) bounds the request.

`execute` returns `{"tweet_url": "https://x.com/i/status/<id>"}`. The `/i/status/`
form resolves without knowing the account's handle, which the create response
does not include.
"""
from __future__ import annotations

import os
from typing import Any

import requests
from requests_oauthlib import OAuth1

from framework.interfaces import Action
from framework.registry import register_action
from framework.templating import render_template

TWEETS_URL = "https://api.x.com/2/tweets"
TWEET_MAX_CHARS = 280

_DEFAULT_ENV = {
    "api_key_env": "TWITTER_API_KEY",
    "api_secret_env": "TWITTER_API_SECRET",
    "access_token_env": "TWITTER_ACCESS_TOKEN",
    "access_secret_env": "TWITTER_ACCESS_SECRET",
}


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"twitter-post needs a credential in ${name}")
    return value


def _error_detail(resp: requests.Response) -> str:
    """The `title` / `detail` pair X puts in an error body, or nothing.

    Only those two keys are surfaced, never the body as a whole: the message is
    for the operator reading a failed step, and two short strings are what X
    uses to say *why* (duplicate content, over the limit, wrong credentials).
    """
    retval = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("title", "detail") if body.get(k)]
        retval = ": " + " - ".join(parts) if parts else ""
    return retval


class TwitterPostAction:
    def __init__(self, text: str, env_names: dict[str, str], timeout: int = 30):
        self._text = text
        self._env_names = env_names
        self._timeout = timeout

    def _auth(self) -> OAuth1:
        retval = OAuth1(
            _require_env(self._env_names["api_key_env"]),
            _require_env(self._env_names["api_secret_env"]),
            _require_env(self._env_names["access_token_env"]),
            _require_env(self._env_names["access_secret_env"]),
        )
        return retval

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        text = render_template(self._text, payload)
        if len(text) > TWEET_MAX_CHARS:
            raise ValueError(
                f"twitter-post: rendered text is {len(text)} characters, "
                f"over the {TWEET_MAX_CHARS} limit; nothing was posted"
            )
        resp = requests.post(
            TWEETS_URL,
            json={"text": text},
            auth=self._auth(),
            timeout=self._timeout,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"twitter-post: POST /2/tweets returned {resp.status_code}{_error_detail(resp)}"
            )
        tweet_id = resp.json().get("data", {}).get("id")
        if not tweet_id:
            raise RuntimeError("twitter-post: POST /2/tweets returned 201 without a tweet id")
        retval = {"tweet_url": f"https://x.com/i/status/{tweet_id}"}
        return retval


@register_action("twitter-post")
def build(cfg: dict) -> Action:
    text = cfg.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("twitter-post: 'text' (a template string) is required")
    env_names = {key: cfg.get(key, default) for key, default in _DEFAULT_ENV.items()}
    retval = TwitterPostAction(
        text=text,
        env_names=env_names,
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
