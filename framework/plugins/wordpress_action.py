"""WordPress.com publish action (issue #47).

An *action*, not a destination: nothing here runs inside `agent run`. A
destination records a proposal, a trusted human replies `do <id>`, and only
then does `agent approvals` call `execute()` on this step (issue #21). The
step turns a Markdown draft into a WordPress.com post and hands the post's
URL and id to the steps after it.

    {
      "type": "wordpress-publish",
      "site": "<site>.wordpress.com",
      "mode": "draft",
      "token_env": "WORDPRESS_ACCESS_TOKEN",
      "categories": ["releases"],
      "tags": []
    }

Config keys:

  - `site`        (required) blog domain or numeric blog id as WordPress.com
                  addresses it: `<site>.wordpress.com`, a mapped custom domain,
                  or the blog_id from `GET /rest/v1.1/me/sites`.
  - `mode`        `draft` or `publish`. `draft` is the operator's safety valve
                  for first runs: the post lands in the editor unpublished and
                  the returned `url` is its preview. `publish` makes it public
                  at once and the returned `url` is the public permalink.
  - `token_env`   env var holding the OAuth2 bearer token; default
                  `WORDPRESS_ACCESS_TOKEN`.
  - `categories`  list of category names; optional.
  - `tags`        list of tag names; optional.

`execute(payload, context)` reads `payload["title"]` and `payload["body_md"]`
and returns `{"url": ..., "post_id": ...}` for later steps to template.

The token, the conversion and the failure modes all follow the
`wordpress-com-publish` skill (voitta-ai/skillz), which is the reference for
publishing to `public-api.wordpress.com`. Its findings are the reason this
module is shaped the way it is:

  - **Token: environment first, then macOS Keychain.** The skill stores the
    token it mints under the Keychain service `wpcom_token`, so a token minted
    once by `/wordpress-com-publish --auth` serves every agent on that machine
    without copying it into a profile. The env var still wins when set, which
    is how a deployment that does not use Keychain supplies it. There is no
    third source and no minting here: the authorization-code flow needs a
    browser login, which an unattended action cannot do. A missing token
    raises at `execute`, naming the env var and the skill that mints one.
  - **A 401 is not a draft.** WordPress answers a revoked token with
    `authorization_required` and an unknown one with `invalid_token`. Either
    raises, naming re-minting as the fix. It
    never degrades `publish` to `draft`, and it never returns a fake result:
    a step that reports success without a post is the silent failure this
    repo's history is made of.
  - **Send Gutenberg block markup, not HTML.** WordPress runs `content` through
    `wpautop`, which reflows raw HTML: long headings acquire newlines and code
    blocks are dropped or garbled. Block markup (`<!-- wp:heading -->`,
    `<!-- wp:paragraph -->`, `<!-- wp:code -->`, `<!-- wp:list -->`) is stored
    verbatim and each element becomes its own block in the editor. The
    conversion below is the skill's `md2wp.py` rule set, ported: render
    Markdown to HTML, then wrap each top-level element in the block comment
    for its tag. The skill renders with pandoc (`--wrap=none`, because pandoc's
    default line wrapping is what put newlines in headings in the first place);
    this port renders with markdown-it-py, a CommonMark renderer that is a
    pip dependency rather than a system binary and does not wrap output, so
    the trap does not apply. CommonMark rather than Python-Markdown because
    the body will usually be LLM output, which nests list items by two
    spaces; Python-Markdown needs four and silently flattens two, while
    CommonMark nests a `-` item's children at two or four. Under a numbered
    item CommonMark wants children indented to the item's text (three spaces
    for `1. `), so two spaces there still comes out flat.
    Heading levels are kept as written: the payload author owns the body, and
    the skill's default of shifting `#` down a level exists for pasting a
    standalone document under a post title, not for a body authored as a post.
  - **The POST response is rendered, not stored.** Its `content` has the block
    comments stripped, so verifying what WordPress kept means
    `GET .../posts/<ID>?context=edit`, not reading the response.
  - **`posts/new` is not idempotent, and nothing here retries.** A timeout
    after WordPress accepted the request can leave a post that this action
    reports as failed, so check the site before re-running by hand.

Not in scope, per the issue: updating an existing post, media upload, and a
scheduled (`future`) publish date.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from typing import Any

import requests
from markdown_it import MarkdownIt

from framework.interfaces import Action
from framework.registry import register_action

_API = "https://public-api.wordpress.com/rest/v1.1"
_MODES = ("draft", "publish")
_DEFAULT_TOKEN_ENV = "WORDPRESS_ACCESS_TOKEN"
_KEYCHAIN_SERVICE = "wpcom_token"
_SKILL = "wordpress-com-publish"
# The "commonmark" preset passes raw HTML through, matching pandoc gfm in the
# skill; tables are a GFM extension the preset leaves off.
_MD = MarkdownIt("commonmark").enable("table")
# WordPress.com reports a revoked token as HTTP 401 `authorization_required`
# (the skill's finding) and a malformed or unknown one as HTTP 400
# `invalid_token` (observed while verifying this action). Both mean the same
# thing to an operator: mint a new token.
_AUTH_ERRORS = ("authorization_required", "invalid_token")


class WordPressPublishError(Exception):
    """The post could not be created; the message names the remedy."""


_VOID_TAGS = frozenset(
    ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
     "source", "track", "wbr")
)


class _BlockSplitter(HTMLParser):
    """Collect the top-level elements of an HTML fragment as (tag, raw) pairs.

    Same shape as the parser in the skill's `md2wp.py`: text between top-level
    elements is dropped, everything inside one is re-emitted verbatim,
    entities included (`convert_charrefs=False` keeps `&lt;` in code as-is).
    One addition: a void element written without the self-closing slash
    (`<br>`, `<img ...>` in raw HTML inside the Markdown) never gets an end
    tag, and counting it as an open element would leave the depth stuck above
    zero and silently drop every block after it. A void element at the top
    level (`---` renders as `<hr />`) is a block of its own.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.blocks: list[tuple[str, str]] = []
        self.cur = ""
        self.curtag = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.depth == 0:
            self.curtag = tag
            self.cur = ""
        self.cur += self.get_starttag_text() or ""
        if tag not in _VOID_TAGS:
            self.depth += 1
        elif self.depth == 0:
            self._emit()

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if self.depth == 0:
            self.curtag = tag
            self.cur = ""
        self.cur += self.get_starttag_text() or ""
        if self.depth == 0:
            self._emit()

    def _emit(self) -> None:
        self.blocks.append((self.curtag, self.cur))
        self.cur = ""
        self.curtag = ""

    def handle_endtag(self, tag: str) -> None:
        self.depth -= 1
        self.cur += f"</{tag}>"
        if self.depth == 0:
            self._emit()

    def handle_data(self, data: str) -> None:
        if self.depth > 0:
            self.cur += data

    def handle_entityref(self, name: str) -> None:
        if self.depth > 0:
            self.cur += f"&{name};"

    def handle_charref(self, name: str) -> None:
        if self.depth > 0:
            self.cur += f"&#{name};"


def _markdown_to_blocks(body_md: str) -> str:
    """Markdown -> Gutenberg block markup, per the skill's `md2wp.py` rules."""
    html = _MD.render(body_md)
    splitter = _BlockSplitter()
    splitter.feed(html)
    splitter.close()
    out: list[str] = []
    for tag, raw in splitter.blocks:
        if re.fullmatch(r"h[1-6]", tag):
            lvl = int(tag[1])
            raw = re.sub(rf"^<h{lvl}\b", f'<h{lvl} class="wp-block-heading"', raw, count=1)
            attr = "" if lvl == 2 else f' {{"level":{lvl}}}'
            out.append(f"<!-- wp:heading{attr} -->\n{raw}\n<!-- /wp:heading -->")
        elif tag == "p":
            out.append(f"<!-- wp:paragraph -->\n{raw}\n<!-- /wp:paragraph -->")
        elif tag == "pre":
            raw = re.sub(r"^<pre\b[^>]*>", '<pre class="wp-block-code">', raw, count=1)
            out.append(f"<!-- wp:code -->\n{raw}\n<!-- /wp:code -->")
        elif tag == "ul":
            raw = re.sub(r"^<ul\b[^>]*>", '<ul class="wp-block-list">', raw, count=1)
            out.append(f"<!-- wp:list -->\n{raw}\n<!-- /wp:list -->")
        elif tag == "ol":
            raw = re.sub(r"^<ol\b[^>]*>", '<ol class="wp-block-list">', raw, count=1)
            out.append(f'<!-- wp:list {{"ordered":true}} -->\n{raw}\n<!-- /wp:list -->')
        elif tag == "blockquote":
            out.append(f"<!-- wp:quote -->\n{raw}\n<!-- /wp:quote -->")
        else:
            out.append(f"<!-- wp:html -->\n{raw}\n<!-- /wp:html -->")
    retval = "\n\n".join(out)
    return retval


def _keychain_token() -> str:
    """The skill's Keychain fallback; empty when absent or not on macOS."""
    retval = ""
    security = shutil.which("security")
    if security is not None:
        proc = subprocess.run(
            [security, "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            retval = proc.stdout.strip()
    return retval


def _resolve_token(token_env: str) -> str:
    token = os.environ.get(token_env, "").strip()
    if not token:
        token = _keychain_token()
    if not token:
        raise WordPressPublishError(
            f"wordpress-publish: no token; set {token_env} or store one in the "
            f"macOS Keychain under service {_KEYCHAIN_SERVICE!r}. Mint one with "
            f"the {_SKILL} skill (`/{_SKILL} --auth`); it needs a browser login, "
            "so this cannot run unattended."
        )
    retval = token
    return retval


class WordPressPublishAction:
    def __init__(
        self,
        site: str,
        mode: str,
        token_env: str = _DEFAULT_TOKEN_ENV,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        timeout: int = 30,
    ):
        if not site:
            raise ValueError("wordpress-publish: `site` is required")
        if mode not in _MODES:
            raise ValueError(
                f"wordpress-publish: mode must be one of {list(_MODES)}, got {mode!r}"
            )
        self.site = site
        self.mode = mode
        self.token_env = token_env
        self.categories = list(categories or [])
        self.tags = list(tags or [])
        self._timeout = timeout

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        for key in ("title", "body_md"):
            if not payload.get(key):
                raise WordPressPublishError(
                    f"wordpress-publish: payload is missing {key!r}; the proposal "
                    "must supply `title` and `body_md` (Markdown)"
                )
        token = _resolve_token(self.token_env)
        form: dict[str, str] = {
            "title": payload["title"],
            "content": _markdown_to_blocks(payload["body_md"]),
            "status": self.mode,
        }
        if self.categories:
            form["categories"] = ",".join(self.categories)
        if self.tags:
            form["tags"] = ",".join(self.tags)
        resp = requests.post(
            f"{_API}/sites/{self.site}/posts/new",
            headers={"Authorization": f"Bearer {token}"},
            data=form,
            timeout=self._timeout,
        )
        body = self._json(resp)
        if resp.status_code in (401, 403) or body.get("error") in _AUTH_ERRORS:
            raise WordPressPublishError(
                f"wordpress-publish: HTTP {resp.status_code} "
                f"{body.get('error', '')}: the token in {self.token_env} (or the "
                f"Keychain item {_KEYCHAIN_SERVICE!r}) was rejected; re-mint it with "
                f"the {_SKILL} skill (`/{_SKILL} --auth`). Nothing was posted."
            )
        if not resp.ok or "ID" not in body:
            raise WordPressPublishError(
                f"wordpress-publish: HTTP {resp.status_code} from {self.site}: "
                f"{body.get('error', '')} {body.get('message', '')}".rstrip()
            )
        url = body["URL"]
        if self.mode == "draft":
            # The API's URL for a draft is the `?p=<id>` form, which a logged-in
            # editor can open; `preview=true` makes it the same link the editor's
            # own Preview button produces, so the approver lands on the preview.
            url = f"{url}{'&' if '?' in url else '?'}preview=true"
        retval = {"url": url, "post_id": body["ID"]}
        return retval

    @staticmethod
    def _json(resp: requests.Response) -> dict[str, Any]:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {}
        retval = parsed if isinstance(parsed, dict) else {}
        return retval


@register_action("wordpress-publish")
def build(cfg: dict) -> Action:
    retval = WordPressPublishAction(
        site=cfg.get("site", ""),
        mode=cfg.get("mode", ""),
        token_env=cfg.get("token_env", _DEFAULT_TOKEN_ENV),
        categories=cfg.get("categories"),
        tags=cfg.get("tags"),
        timeout=int(cfg.get("timeout", 30)),
    )
    return retval
