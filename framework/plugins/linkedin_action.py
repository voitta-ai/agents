"""LinkedIn post action (issue #48).

One step of an approval-gated sequence: after a post has been published, share
it on the operator's LinkedIn profile. Configured as a `steps` entry:

    steps:
      - type: linkedin-post
        text: "{blurb_linkedin} {url}"

`text` is a template rendered with `str.format_map` over the step's payload, the
same way `twitter-post` does it: earlier steps' results and the proposal's
fields are available by name, and a key the payload lacks is an error naming
that key rather than an empty substitution.

The share goes through the LinkedIn Posts API: `POST https://api.linkedin.com/rest/posts`
with a text-only body (`author`, `commentary`, `visibility: PUBLIC`,
`distribution.feedDistribution: MAIN_FEED`, `lifecycleState: PUBLISHED`). The
versioned REST base path requires two headers on every call: `LinkedIn-Version`
in `YYYYMM` form and `X-Restli-Protocol-Version: 2.0.0`. LinkedIn publishes a
version monthly and supports each for at least a year; the default here is
`202608`, the latest documented when this module was written, and `version` in
the config overrides it when that one sunsets.

`author` is `urn:li:person:<id>`, where the id is the `sub` claim from
`GET https://api.linkedin.com/v2/userinfo` (OpenID Connect). It is resolved once
per action instance and reused. `userinfo` needs the `openid` scope alongside
`w_member_social`; both come from the same "Share on LinkedIn" + "Sign In with
LinkedIn using OpenID Connect" products on the developer app.

Ships working but dormant. No LinkedIn token exists yet, so the token variable
(config `access_token_env`, default `LINKEDIN_ACCESS_TOKEN`) is read in
`execute`, not at construction: earlier steps of the sequence run, and when this
one is reached with the variable unset it raises a "not configured" error naming
the variable and the scope. The approval runner reports that step as failed and
stops, so the operator sees exactly what is waiting on credentials, and nothing
before it is silently skipped. LinkedIn member tokens live 60 days and do not
refresh on their own; the expiry is the operator's to track.

Nothing in this module logs a header, a request body or the token at any level.
An API failure surfaces as the status code plus the short `message` LinkedIn
puts in its error body, which is what says *why* (expired token, missing scope,
sunset version).

`execute` returns `{"post_urn": "urn:li:share:<id>"}`, taken from the
`x-restli-id` response header, which is where the Posts API reports the id of
a created post.
"""
from __future__ import annotations

import os
from typing import Any

import requests

from framework.interfaces import Action
from framework.registry import register_action

USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
DEFAULT_LINKEDIN_VERSION = "202608"
DEFAULT_TOKEN_ENV = "LINKEDIN_ACCESS_TOKEN"
REQUIRED_SCOPE = "w_member_social"


class _Payload(dict):
    """Payload view for `str.format_map` that names a missing key."""

    def __missing__(self, key: str):
        raise ValueError(
            f"linkedin-post: text template references {key!r}, which is not in the payload"
        )


def _render(template: str, payload: dict[str, Any]) -> str:
    retval = template.format_map(_Payload(payload))
    return retval


def _error_detail(resp: requests.Response) -> str:
    """The `message` LinkedIn puts in an error body, or nothing."""
    retval = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("message"):
        retval = f": {body['message']}"
    return retval


class LinkedInPostAction:
    def __init__(self, text: str, token_env: str, version: str, timeout: int = 30):
        self._text = text
        self._token_env = token_env
        self._version = version
        self._timeout = timeout
        self._author: str | None = None

    def _token(self) -> str:
        value = os.environ.get(self._token_env)
        if not value:
            raise RuntimeError(
                f"linkedin-post is not configured: set ${self._token_env} to a LinkedIn "
                f"member access token with the {REQUIRED_SCOPE} scope"
            )
        return value

    def _headers(self, token: str) -> dict[str, str]:
        retval = {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": self._version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
        return retval

    def _resolve_author(self, token: str) -> str:
        if self._author is None:
            resp = requests.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"linkedin-post: GET /v2/userinfo returned {resp.status_code}"
                    f"{_error_detail(resp)}"
                )
            sub = resp.json().get("sub")
            if not sub:
                raise RuntimeError(
                    "linkedin-post: GET /v2/userinfo returned no 'sub'; "
                    "the token needs the openid scope"
                )
            self._author = f"urn:li:person:{sub}"
        retval = self._author
        return retval

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        token = self._token()
        text = _render(self._text, payload)
        author = self._resolve_author(token)
        body = {
            "author": author,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        resp = requests.post(
            POSTS_URL,
            json=body,
            headers=self._headers(token),
            timeout=self._timeout,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"linkedin-post: POST /rest/posts returned {resp.status_code}{_error_detail(resp)}"
            )
        post_urn = resp.headers.get("x-restli-id")
        if not post_urn:
            raise RuntimeError("linkedin-post: POST /rest/posts returned 201 without x-restli-id")
        retval = {"post_urn": post_urn}
        return retval


@register_action("linkedin-post")
def build(cfg: dict) -> Action:
    text = cfg.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("linkedin-post: 'text' (a template string) is required")
    retval = LinkedInPostAction(
        text=text,
        token_env=cfg.get("access_token_env", DEFAULT_TOKEN_ENV),
        version=str(cfg.get("version", DEFAULT_LINKEDIN_VERSION)),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
