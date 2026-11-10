"""The action page: one link, one lead, acted on without Slack (issue #124).

A third way to resolve a proposal, beside the `do <id>` reply
(`framework/approvals.py`) and the Block Kit button
(`framework/approval_buttons.py`). The reason it exists is not that a third
mechanism is desirable: it is that the two existing ones both require a Slack
account in the agent's workspace, and the people who decide on leads are not
all in Slack and do not want to be. A Signal group gets a blurb and a link;
the link opens this page; the verdict happens here.

What that buys, beyond reaching people Slack cannot:

  - **There is nothing to reconcile across surfaces.** The same lead announced
    to a Slack channel and a Signal group is one proposal with one link, so
    resolving it resolves it everywhere by construction. Cross-surface state,
    convergence after an outage and the double-execution window between two
    surfaces are not solved here, they are absent.
  - **A new surface costs nothing.** Email, SMS, a web digest: each needs only
    to render a blurb and a URL. No surface needs a way to send a decision
    back, which is what made approving from Signal expensive (#121: a receive
    loop on an account only one process can hold).

Everything else is deliberately shared with the button path: the same
`ProposalStore`, the same compare-and-set claim, the same step runner. A tap
here, a click in Slack and a `do <id>` reply cannot both execute, because all
three take the proposal through `transition(pending -> running)` and only one
can win. No new concurrency reasoning was introduced and none should be.

Config, in the workspace's `config.json`:

    "approvals": {
      "channel_id": "C0123456789",
      "bot_token_env": "SOME_BOT_TOKEN",
      "trusted_user_ids": ["U0123456789"],
      "action_page": {
        "base_url": "https://example.invalid/leads",
        "bind": "127.0.0.1",
        "port": 8731,
        "post_label": "Draft outreach"
      }
    }

`base_url` is how the link reads to whoever receives it, which is not
necessarily how this process is reached: a tunnel or a reverse proxy sits in
between, so the two are configured separately and nothing here guesses. It is
the only key a *posting* run needs; `bind` and `port` are read only by the
listener that serves the page. `post_label` renames the first verdict, for the
same reason the Slack card can (#76): "Post" is wrong for an action that
drafts something a human will send.

## The URL is the credential, and that is the whole security model

The reply path trusts a Slack user id; the button path trusts a Slack user id.
A link has no identity behind it. Whoever holds the URL can act, and in a
Signal group that means every member, anyone who joins later and scrolls back,
and any device backup. This is a real reduction in authorization strength and
should be chosen deliberately, for a group whose members would all have been
in `trusted_user_ids` anyway.

It is not, however, unprecedented in this codebase: a Slack `response_url` is
described in `framework/approval_buttons.py` as "a bearer capability (30
minutes, five uses)", and the same discipline is applied here.

  - **The token is not the proposal id.** Ids are short by design -- a base-36
    counter plus three random base-36 characters, about 47 thousand
    possibilities for a given counter value -- which is ample against someone
    typing `do p10` into a channel and nowhere near enough for something
    reachable over HTTP. The link carries its own `secrets.token_urlsafe(32)`,
    compared with `secrets.compare_digest`.
  - **A token resolves exactly one proposal**, never the store. It is useless
    against any other lead.
  - **Single use falls out of the claim.** The first verdict moves the
    proposal off `pending`; every later request fails the compare-and-set and
    is told what the proposal already is. No separate "token spent" state
    exists to get out of step with the proposal's.
  - **The token must not leak outward.** The page sends
    `Referrer-Policy: no-referrer` and marks links `rel="noreferrer noopener"`,
    because the blurb is about somebody else's web page and a plain anchor
    would hand our token to that site in the `Referer` header. It is still in
    the reader's browser history and in the logs of anything the request
    passes through; `base_url` should therefore point at something private.

## A GET must never act

This is the correctness condition for the whole design, not a preference.
Signal, Slack and iMessage all build link previews by fetching the URL **from
the recipient's device**, so a link whose GET resolves the proposal is
resolved by the message rendering on somebody's phone, before a human has read
it. The same mechanism made "unsubscribe" links unusable for a decade, courtesy
of corporate mail scanners.

So: `GET` renders and is idempotent; only `POST` resolves. The POST carries a
form token the GET minted (an HMAC over the link token under a per-process
secret), which also means a third-party page cannot forge the request on a
reader's behalf -- it cannot read the GET's response to learn the field. A
restart between the GET and the POST invalidates the form token and the page
says to reload; that is the cost of keeping no server-side session.

## Nothing untrusted becomes HTML

The blurb is a Reddit post, a LinkedIn post or whatever else a source
produced, which is to say it is attacker-controlled text that a human asked to
see. It is HTML-escaped and rendered as preformatted text, and the only anchor
on the page is the lead's own URL, which is checked for an `http`/`https`
scheme first. No Markdown is turned into markup here and none should be: a
stored cross-site scripting bug on a page that resolves proposals would hand
the token and the verdict to whoever wrote the post.
"""
from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import sys
import threading
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from framework.approvals import (
    ApprovalsConfig,
    VARIANT_DRAFT,
    VARIANT_POST,
    _now_iso,
    _run_steps,
    _slack_call,
    has_draftable_step,
    steps_detail,
)
from framework.config import ConfigError
from framework.slack import to_mrkdwn
from framework.proposals import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SKIPPED,
)

TOKEN_FIELD = "link_token"
DECIDED_VIA = "link"
PATH_PREFIX = "/a"

_TOKEN_BYTES = 32
# A whole proposal page is a few KiB of form fields at the very most; anything
# larger is not a verdict from this page's own form.
_MAX_BODY = 8192
_VERDICTS = {"post": VARIANT_POST, "draft": VARIANT_DRAFT, "discard": "discard"}
_DEFAULT_BIND = "127.0.0.1"
_DEFAULT_PORT = 8731
_DEFAULT_POST_LABEL = "Post"

# Minted once per process and never stored: it only has to outlive the gap
# between a GET and the POST that follows it.
_FORM_SECRET = secrets.token_bytes(32)


class ActionPageError(RuntimeError):
    """The action page cannot serve: bad config, or the port is unusable."""


class ActionPageConfig:
    def __init__(
        self,
        base_url: str,
        bind: str = _DEFAULT_BIND,
        port: int = _DEFAULT_PORT,
        post_label: str = _DEFAULT_POST_LABEL,
    ):
        self.base_url = base_url.rstrip("/")
        self.bind = bind
        self.port = port
        self.post_label = post_label


def load_action_page(block: dict[str, Any], config_path: Path) -> ActionPageConfig | None:
    """The `approvals.action_page` block, or None when there is none.

    Absent is the normal case and not an error: every deployment before #124
    approves in Slack only.
    """
    raw = block.get("action_page")
    if raw is None:
        retval = None
        return retval
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: 'approvals.action_page' must be an object")
    base_url = raw.get("base_url")
    if not base_url or not isinstance(base_url, str):
        raise ConfigError(
            f"{config_path}: 'approvals.action_page.base_url' is required -- how the link "
            "reads to whoever receives it, which is not necessarily how this process is "
            "reached (a tunnel or proxy may sit in between)"
        )
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"{config_path}: 'approvals.action_page.base_url' must be an absolute "
            f"http(s) URL; got {base_url!r}"
        )
    port = raw.get("port", _DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(
            f"{config_path}: 'approvals.action_page.port' must be a port number 1-65535"
        )
    retval = ActionPageConfig(
        base_url=base_url,
        bind=str(raw.get("bind", _DEFAULT_BIND)),
        port=port,
        post_label=str(raw.get("post_label", _DEFAULT_POST_LABEL)),
    )
    return retval


def mint_token() -> str:
    """A link token: 32 random bytes, URL-safe, not derived from the proposal."""
    retval = secrets.token_urlsafe(_TOKEN_BYTES)
    return retval


def action_path(workspace: str, token: str) -> str:
    retval = f"{PATH_PREFIX}/{workspace}/{token}"
    return retval


def action_url(base_url: str, workspace: str, token: str) -> str:
    retval = f"{base_url.rstrip('/')}{action_path(workspace, token)}"
    return retval


def base_url_for(workspace: str | os.PathLike) -> str | None:
    """`approvals.action_page.base_url` from a workspace, for a posting run.

    A destination needs the base URL to render a link, and reading it from the
    one place the listener reads it from is the point: a second config key for
    the same value is a second thing to get out of step. Returns None when the
    workspace does not configure an action page, which is how a destination
    knows to post no link rather than a broken one.
    """
    path = Path(workspace) / "config.json"
    raw = json.loads(path.read_text())
    block = raw.get("approvals") if isinstance(raw, dict) else None
    retval = None
    if not isinstance(block, dict):
        return retval
    # The same parser the listener uses, so the two cannot disagree. An earlier
    # version read the key directly and applied none of the validation, which
    # meant a relative `base_url` or an out-of-range `port` let the posting run
    # mint links happily while `--listen` refused to start -- links in somebody's
    # Signal group for a page no process serves, and nothing anywhere noticing.
    # A malformed config now fails the run that posts, in the same words the
    # listener would have used.
    page = load_action_page(block, path)
    if page is not None:
        retval = page.base_url
    return retval


def find_by_token(store: Any, token: str) -> dict[str, Any] | None:
    """The proposal whose `link_token` is `token`, compared in constant time.

    Reads the whole store rather than keeping a token index: the store is one
    small JSON file per workspace and an index is another thing that can
    disagree with the proposals it describes.
    """
    retval = None
    # `compare_digest` on `str` raises TypeError unless both sides are ASCII,
    # and the path arrives decoded as iso-8859-1, so any raw byte >= 0x80 in
    # the token segment would reach it. A real token is urlsafe base64 and so
    # always ASCII; anything else is a miss, not an error.
    if not token or not token.isascii():
        return retval
    with store._locked():  # noqa: SLF001 - the store's own read path, under its lock
        data = store._read()  # noqa: SLF001
    for proposal in data["proposals"]:
        held = proposal.get(TOKEN_FIELD)
        if isinstance(held, str) and secrets.compare_digest(held, token):
            retval = proposal
            break
    return retval


def _form_token(link_token: str) -> str:
    retval = hmac.new(_FORM_SECRET, link_token.encode(), sha256).hexdigest()
    return retval


def _form_token_ok(link_token: str, given: str) -> bool:
    retval = bool(given) and hmac.compare_digest(_form_token(link_token), given)
    return retval


def _esc(value: Any) -> str:
    retval = html.escape(str(value), quote=True)
    return retval


def _safe_link(url: Any) -> str:
    """The lead's own URL as an anchor, or empty when it is not http(s).

    A `javascript:` or `data:` URL in a scraped field would otherwise become a
    clickable script on a page that resolves proposals.
    """
    retval = ""
    text = str(url or "").strip()
    if not text:
        return retval
    if urlparse(text).scheme not in ("http", "https"):
        return retval
    retval = (
        f'<p class="src"><a href="{_esc(text)}" rel="noreferrer noopener" '
        f'target="_blank">{_esc(text)}</a></p>'
    )
    return retval


_STYLE = """
:root { color-scheme: light dark; --fg: #16181d; --bg: #fbfbfd; --mut: #5c6370;
        --line: #d9dce3; --card: #ffffff; --accent: #2b5fd9; }
@media (prefers-color-scheme: dark) { :root {
  --fg: #e7e9ee; --bg: #14161a; --mut: #9aa2b1; --line: #2b2f38; --card: #1b1e24;
  --accent: #7aa2ff; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 16px; background: var(--bg); color: var(--fg);
       font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 44rem; margin: 0 auto; }
h1 { font-size: 1.15rem; margin: 0 0 2px; }
.meta { color: var(--mut); font-size: .85rem; margin: 0 0 18px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
        padding: 16px 18px; margin: 0 0 20px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; font: inherit; }
.src { margin: 14px 0 0; overflow-wrap: anywhere; font-size: .9rem; }
form { display: flex; flex-wrap: wrap; gap: 10px; }
button { font: inherit; padding: 11px 20px; border-radius: 8px; border: 1px solid var(--line);
         background: var(--card); color: var(--fg); cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.note { color: var(--mut); font-size: .85rem; margin: 18px 0 0; }
.done { border-left: 3px solid var(--accent); padding-left: 14px; }
"""


def _page(title: str, body: str) -> bytes:
    retval = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        f"<title>{_esc(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    ).encode()
    return retval


def siblings(store: Any, proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """Other proposals for the same lead: one per configured action.

    A config with two actions announces one lead carrying two links. Both pages
    showed the same title and the same blurb, so a recipient who resolved one
    and then opened the other read it as "my tap did not register" and tapped
    again -- running a *different* action deliberately. The pages now name each
    other, which is cheaper than explaining it afterwards.
    """
    retval = []
    url = (proposal.get("payload") or {}).get("url")
    if not url:
        return retval
    try:
        with store._locked():  # noqa: SLF001
            data = store._read()  # noqa: SLF001
    except Exception:
        return retval
    for other in data["proposals"]:
        if other.get("id") == proposal.get("id"):
            continue
        if (other.get("payload") or {}).get("url") == url:
            retval.append(other)
    return retval


def render_proposal(
    cfg: ApprovalsConfig,
    page: ActionPageConfig,
    proposal: dict[str, Any],
    store: Any = None,
) -> bytes:
    """The GET page: the lead, then the verdicts. Reads nothing, changes nothing."""
    payload = proposal.get("payload") or {}
    blurb = str(payload.get("rendered") or payload.get("text") or "(no content recorded)")
    lead = str(payload.get("title") or proposal["id"])
    action = str(proposal.get("action") or "")
    # The action is in the heading, not only in the grey line, because with
    # more than one action per lead the heading was the only thing a recipient
    # read and it was identical on both pages.
    title = f"{action}: {lead}" if action else lead
    token = str(proposal.get(TOKEN_FIELD) or "")
    status = str(proposal.get("status") or "")

    kin = siblings(store, proposal) if store is not None else []
    kin_note = ""
    if kin:
        rows = ", ".join(
            f"{_esc(o.get('action', ''))} ({_esc(o.get('status', ''))})" for o in kin
        )
        kin_note = (
            f'<p class="note">This lead has {len(kin)} other proposal(s), each with its own '
            f"link and its own decision: {rows}. Resolving this one does not resolve those.</p>"
        )

    head = (
        f"<h1>{_esc(title)}</h1>"
        f'<p class="meta">{_esc(cfg.agent)} &middot; proposal '
        f"{_esc(proposal['id'])} &middot; {_esc(action)}</p>"
        f'<div class="card"><pre>{_esc(blurb)}</pre>{_safe_link(payload.get("url"))}</div>'
        + kin_note
    )

    if status != STATUS_PENDING:
        retval = _page(
            title,
            head + f'<p class="note done">This one is already <strong>{_esc(status)}</strong>'
            + (
                f" (via {_esc(proposal.get('decided_via'))})"
                if proposal.get("decided_via")
                else ""
            )
            + ". Nothing ran just now.</p>",
        )
        return retval

    buttons = [("post", page.post_label, "primary")]
    if has_draftable_step(cfg, str(proposal.get("action") or "")):
        buttons.append(("draft", "Draft", ""))
    buttons.append(("discard", "Discard", ""))
    fields = (
        f'<input type="hidden" name="form_token" value="{_esc(_form_token(token))}">'
        if token
        else ""
    )
    controls = "".join(
        f'<button class="{cls}" name="verdict" value="{verdict}">{_esc(label)}</button>'
        for verdict, label, cls in buttons
    )
    retval = _page(
        title,
        head
        + f'<form method="post" action="{_esc(action_path(cfg.workspace.name, token))}">'
        + fields
        + controls
        + "</form>"
        + '<p class="note">Acting here is the same as approving in Slack: it resolves this '
        "lead everywhere it was posted. Whoever holds this link can act on it, so treat it "
        "as the decision itself.</p>",
    )
    return retval


def _tell_slack(cfg: ApprovalsConfig, store: Any, proposal_id: str, text: str) -> str:
    """Mirror the verdict into the approval channel; never fail the page for it.

    The proposal is already resolved by the time this runs, exactly as on the
    reply path ("if posting the outcome fails, the status is already right").
    The difference is that a human is holding an open page, so the failure is
    reported to them and to stderr rather than raised.

    What it must not do is lose the failure. Three things record it: the page
    the human is holding, a stderr line carrying the proposal id and the real
    error (a `channel_not_found` and an `invalid_auth` need different repairs,
    and `type(exc).__name__` alone said neither), and `slack_mirrored: false`
    on the proposal -- without which a `done` lead could leave no trace in
    Slack at all and no durable evidence that it should have.
    """
    retval = ""
    if not cfg.bot_token or not cfg.channel_id:
        return retval
    try:
        _slack_call(
            "chat.postMessage", cfg.bot_token, cfg.timeout, channel=cfg.channel_id, text=text
        )
    except Exception as exc:
        retval = f"Slack was not told ({type(exc).__name__}); the verdict is recorded regardless."
        print(
            f"agent={cfg.agent} action-page could not mirror proposal={proposal_id} "
            f"to Slack: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        current = store.get(proposal_id) or {}
        now = current.get("status")
        if now:
            try:
                store.transition(proposal_id, now, now, slack_mirrored=False)
            except Exception:
                # The store is the thing that is broken; the stderr line above
                # is then the only record, which is why it carries the id.
                pass
    return retval


def _retract_card(route: Any, proposal: dict[str, Any], line: str) -> bool:
    """Edit the Block Kit card in place, buttons replaced by the outcome.

    Without this a link verdict left the Slack card untouched: live Post /
    Draft / Discard buttons on a lead that is already closed. Clicking one was
    always safe -- the claim refuses it -- but a reader scrolling the channel
    had no way to know the lead was decided, which is the failure that would
    have confused people daily.

    Returns whether the card was edited, so the caller can skip the separate
    outcome message rather than saying the same thing twice.
    """
    ts = str(proposal.get("card_ts") or "")
    cfg = route.cfg
    retval = False
    if not ts or not cfg.channel_id:
        return retval
    # Function-level: `framework/approval_buttons.py` imports this module at
    # module scope to serve the page, so importing it back at module scope
    # would be a cycle. This is the one thing needed from it.
    from framework.approval_buttons import outcome_blocks

    payload = proposal.get("payload") or {}
    content = str(payload.get("rendered") or payload.get("text") or "")
    try:
        route.responder.update(
            cfg.channel_id, ts, f"{content}\n\n{line}", outcome_blocks(to_mrkdwn(content), line)
        )
        retval = True
    except Exception as exc:
        print(
            f"agent={cfg.agent} action-page could not retract the card for "
            f"proposal={proposal['id']}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    return retval


def resolve(route: Any, proposal: dict[str, Any], verdict: str) -> tuple[bytes, str]:
    """Act on one POSTed verdict; return (page bytes, log summary).

    Ordering is the button path's and the reply path's, and for the same
    reason: claim, then steps, then the terminal status, then anyone is told.
    Every exit that does not claim leaves the proposal exactly as it was, so
    Slack can still resolve it.
    """
    cfg = route.cfg
    store = route.store
    proposal_id = proposal["id"]
    payload = proposal.get("payload") or {}
    title = str(payload.get("title") or proposal_id)
    # No `decided_by`: the link carries no identity, and a value here would
    # read as an actor in the store next to the Slack user ids the other two
    # paths record. `decided_via` is the whole truth about who acted.
    decided = {"decided_ts": _now_iso(), "decided_via": DECIDED_VIA}

    def already() -> tuple[bytes, str]:
        current = store.get(proposal_id) or {}
        status = current.get("status", "gone")
        body = (
            f"<h1>{_esc(title)}</h1>"
            f'<p class="note done">Proposal {_esc(proposal_id)} is already '
            f"<strong>{_esc(status)}</strong>"
            + (f" (via {_esc(current.get('decided_via'))})" if current.get("decided_via") else "")
            + ". Nothing ran.</p>"
        )
        return (_page(title, body), f"proposal={proposal_id} verdict=already-{status} via=link")

    if verdict == "discard":
        if store.transition(proposal_id, STATUS_PENDING, STATUS_SKIPPED, **decided) is None:
            retval = already()
            return retval
        line = f"Discarded from the action link at {_now_iso()}. Nothing ran."
        note = ""
        if not _retract_card(route, proposal, line):
            note = _tell_slack(
                cfg, store, proposal_id,
                f"Proposal {proposal_id} discarded from the action link.",
            )
        body = (
            f"<h1>{_esc(title)}</h1>"
            f'<p class="note done">Discarded. Nothing ran, and proposal '
            f"{_esc(proposal_id)} is closed.</p>"
            + (f'<p class="note">{_esc(note)}</p>' if note else "")
        )
        retval = (_page(title, body), f"proposal={proposal_id} verdict=discard via=link")
        return retval

    variant = _VERDICTS[verdict]
    if variant == VARIANT_DRAFT and not has_draftable_step(cfg, str(proposal.get("action") or "")):
        body = (
            f"<h1>{_esc(title)}</h1>"
            f'<p class="note">This action has no step a draft can draft, so there is nothing '
            f"to draft. Proposal {_esc(proposal_id)} is still open.</p>"
        )
        retval = (_page(title, body), f"proposal={proposal_id} verdict=draft-refused via=link")
        return retval

    claimed = store.transition(
        proposal_id,
        STATUS_PENDING,
        STATUS_RUNNING,
        claimed_at=_now_iso(),
        claimed_pid=os.getpid(),
        # The pid here is the long-lived listener's, not a pass that exits.
        # `framework/approvals.py` decides a `running` proposal is stuck partly
        # by the claiming pid being gone, which can never happen for a claim
        # made in this process -- so it is told which kind of claim this is.
        claimed_via=DECIDED_VIA,
        variant=variant,
        **decided,
    )
    if claimed is None:
        retval = already()
        return retval

    results, error = _run_steps(cfg, store, proposal, variant=variant)
    if error is None:
        store.transition(proposal_id, STATUS_RUNNING, STATUS_DONE, results=results)
        status, detail = STATUS_DONE, steps_detail(results)
    else:
        store.transition(proposal_id, STATUS_RUNNING, STATUS_FAILED, results=results, error=error)
        status, detail = STATUS_FAILED, f"{error} Steps: {steps_detail(results)}"
    line = f"{status} ({variant}) from the action link at {_now_iso()}. {detail}"
    note = ""
    if not _retract_card(route, proposal, line):
        note = _tell_slack(
            cfg, store, proposal_id,
            f"Proposal {proposal_id} {status} ({variant}) from the action link. {detail}",
        )
    body = (
        f"<h1>{_esc(title)}</h1>"
        f'<p class="note done"><strong>{_esc(status)}</strong> &middot; {_esc(detail)}</p>'
        + (f'<p class="note">{_esc(note)}</p>' if note else "")
    )
    retval = (_page(title, body), f"proposal={proposal_id} verdict={variant} status={status} via=link")
    return retval


def _not_found() -> bytes:
    """One body for every miss: no token, wrong token, unserved workspace.

    Saying which would turn the page into an oracle for guessing tokens.
    """
    retval = _page(
        "Nothing here",
        '<h1>Nothing here</h1><p class="note">This link does not open anything. It may have '
        "been mistyped, or it may belong to an agent this server does not serve.</p>",
    )
    return retval


class ActionPageHandler(BaseHTTPRequestHandler):
    """GET renders, POST resolves. `routes` is set on the server object.

    `protocol_version` stays at HTTP/1.0 deliberately, which means every
    response closes its connection. HTTP/1.1 keep-alive makes any handler that
    answers *without* consuming the request body a request-smuggling primitive:
    the unread bytes are parsed as the next request on the same connection, so
    a POST to a bogus token carrying a framed POST in its body executed the
    second one -- with no valid token on the outer request. Draining the body
    on every early return fixes the paths this class controls, and that is done
    below, but `BaseHTTPRequestHandler` also answers on its own (a malformed
    request line, an unimplemented method) where there is no hook to drain
    from. One connection per response removes the whole class instead of
    patching its members, and the cost is nothing: a human opens this page a
    handful of times.
    """

    protocol_version = "HTTP/1.0"
    # Without this a connection that opens and never finishes its request line
    # holds a thread forever; a few dozen of them are a denial of service from
    # one host with no credentials.
    timeout = 15

    def version_string(self) -> str:
        # The default advertises the Python version, which is free
        # reconnaissance for anything that finds the port.
        retval = "agents"
        return retval

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default writes the request line to stderr, and the request line
        # holds the link token. One line per verdict is printed by the handler
        # instead, with the proposal id and no token.
        return

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)
        # HTTP/1.0 already implies this; stated so that raising
        # `protocol_version` later cannot silently reopen the smuggling class.
        self.close_connection = True

    def _route(self) -> tuple[Any, dict[str, Any], str] | None:
        """(route, proposal, token) for this request's path, or None.

        Never raises. `_route` runs before the handlers' own `try`, so an
        exception here reaches `socketserver.handle_error` and prints a full
        traceback per request -- which an unauthenticated caller could trigger
        at will. A store that cannot be read is reported once per request as a
        single line and treated as a miss, because answering differently would
        also tell a caller which workspaces exist.
        """
        try:
            retval = self._route_inner()
        except Exception as exc:
            print(f"agent: action-page route failed: {type(exc).__name__}", file=sys.stderr)
            retval = None
        return retval

    def _route_inner(self) -> tuple[Any, dict[str, Any], str] | None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or f"/{parts[0]}" != PATH_PREFIX:
            retval = None
            return retval
        route = getattr(self.server, "routes", {}).get(parts[1])
        if route is None:
            retval = None
            return retval
        proposal = find_by_token(route.store, parts[2])
        retval = None if proposal is None else (route, proposal, parts[2])
        return retval

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        found = self._route()
        if found is None:
            self._send(404, _not_found())
            return
        route, proposal, _ = found
        page = getattr(self.server, "page")
        self._send(200, render_proposal(route.cfg, page, proposal, route.store))

    def do_HEAD(self) -> None:  # noqa: N802
        # Some previewers send HEAD first. Same safety as GET and no body.
        found = self._route()
        self._send(200 if found else 404, b"")

    def _body(self) -> bytes | None:
        """The whole request body, or None when the request was already refused.

        Read before anything else looks at the request, so that every path out
        of `do_POST` -- including a 404 for a token that does not exist -- has
        consumed exactly the bytes the request declared. See the class
        docstring for why an unread body is a smuggling primitive rather than
        an untidiness.
        """
        if self.headers.get("Transfer-Encoding"):
            # The chunked framing is the other half of the same smuggling
            # class, and nothing here needs it.
            self._send(400, _page("Not supported", "<h1>Not supported</h1>"))
            retval = None
            return retval
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= _MAX_BODY:
            self._send(400, _page("Too large", "<h1>Too large</h1>"))
            retval = None
            return retval
        try:
            retval = self.rfile.read(length)
        except OSError:
            # A client that promised more than it sent; nothing was decided.
            retval = None
        if retval is not None and len(retval) != length:
            self._send(400, _page("Incomplete", "<h1>Incomplete</h1>"))
            retval = None
        return retval

    @staticmethod
    def _fail_open_claim(route: Any, proposal_id: str, exc: Exception) -> None:
        """Close a claim this request made, after the request itself blew up."""
        try:
            if route.store.transition(
                proposal_id, STATUS_RUNNING, STATUS_FAILED,
                error=f"action page raised after claiming: {type(exc).__name__}: {exc}",
            ) is not None:
                print(
                    f"agent={route.cfg.agent} proposal={proposal_id} verdict=failed via=link "
                    f"(closed after an error; steps may have run) at={_now_iso()}",
                    flush=True,
                )
        except Exception as inner:
            # Nothing further is possible: the store is what is broken. The
            # stderr line above names the proposal, which is what a human
            # needs to go and look.
            print(
                f"agent={route.cfg.agent} action-page could not close "
                f"proposal={proposal_id}: {type(inner).__name__}",
                file=sys.stderr,
            )

    def do_POST(self) -> None:  # noqa: N802
        raw = self._body()
        if raw is None:
            return
        found = self._route()
        if found is None:
            self._send(404, _not_found())
            return
        route, proposal, token = found
        fields = parse_qs(raw.decode("utf-8", "replace"))
        if not _form_token_ok(token, (fields.get("form_token") or [""])[0]):
            self._send(
                400,
                _page(
                    "Reload this page",
                    '<h1>Reload this page</h1><p class="note">This form is from an older '
                    "session of the server, or did not come from this page. Nothing ran -- "
                    "reload and act again.</p>",
                ),
            )
            return
        verdict = (fields.get("verdict") or [""])[0]
        if verdict not in _VERDICTS:
            self._send(400, _page("Unknown verdict", "<h1>Unknown verdict</h1>"))
            return
        try:
            body, summary = resolve(route, proposal, verdict)
        except Exception as exc:
            # One bad request must not take the server down. But the proposal
            # may already be claimed, and a claim made here can never go stale
            # the way the polling pass's does -- its pid is the listener's and
            # stays alive -- so leaving it `running` hid it from `stuck=N` for
            # the whole `hours` window while its steps had really run. Close it
            # as failed, best effort, and say the error out loud.
            print(
                f"agent={route.cfg.agent} action-page failed on "
                f"proposal={proposal['id']}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            self._fail_open_claim(route, proposal["id"], exc)
            self._send(
                500,
                _page(
                    "That did not work",
                    '<h1>That did not work</h1><p class="note">The decision was recorded as '
                    "failed. Some of its steps may already have run, so check before trying "
                    "again rather than tapping twice.</p>",
                ),
            )
            return
        print(f"agent={route.cfg.agent} {summary} at={_now_iso()}", flush=True)
        self._send(200, body)


class _BoundedHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` with a ceiling on concurrent connections.

    The handler's `timeout` bounds how long any one connection can hold a
    thread, which is what stops a slow client holding one open forever. It does
    not bound how MANY arrive: the mixin spawns a thread per connection with no
    limit, so a flood from a single host exhausts the laptop's threads -- and
    with them the Socket Mode listener in the same process and anything else
    sharing the machine.

    The ceiling converts that into a bounded, visible refusal, which is a
    better failure but is still one: enough held connections make the page
    unavailable to the person it was sent to. That is accepted rather than
    solved, because it cannot be solved here. A per-client ceiling would be the
    usual answer and is the wrong one for this deployment -- behind a tunnel or
    a reverse proxy every connection arrives from the proxy, so a per-address
    limit would throttle the normal case and leave the abnormal one untouched.

    What actually bounds it is not being reachable by strangers: `bind`
    defaults to loopback, and a non-loopback bind is warned about at startup.
    This is a page for a handful of people behind a tailnet or a tunnel you
    control, not an internet-facing server, and an stdlib threaded server
    should not be asked to be one.
    """

    max_connections = 64

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            print(
                f"agent: action-page refused a connection; all {self.max_connections} "
                "slots are busy",
                file=sys.stderr,
            )
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve_action_page(routes: dict[str, Any], page: ActionPageConfig) -> ThreadingHTTPServer:
    """Start the action page on its own thread and return the server.

    A thread rather than a process, so the page and the Socket Mode client
    share one `ProposalStore` per workspace and therefore one claim. Binding
    failure is raised here, at start, rather than discovered when somebody taps
    a link.
    """
    try:
        server = _BoundedHTTPServer((page.bind, page.port), ActionPageHandler)
    except OSError as exc:
        raise ActionPageError(
            f"action page cannot bind {page.bind}:{page.port}: {exc}"
        ) from exc
    if page.bind not in ("127.0.0.1", "::1", "localhost"):
        # Not fatal: a tailnet address is a legitimate bind. But the page has
        # no authentication beyond the link token and no rate limiting worth
        # the name, so a bind reachable by strangers is nearly always a
        # mistake, and finding out from the logs beats finding out otherwise.
        print(
            f"agent: action page is bound to {page.bind}, not loopback. Anything that can "
            "reach it can try link tokens and can exhaust its connection ceiling; put it "
            "behind a tailnet or a tunnel you control rather than a public interface.",
            file=sys.stderr,
        )
    # `ThreadingHTTPServer.shutdown()` stops `serve_forever` and leaves the
    # listening socket open, so a second bind in the same process fails with
    # EADDRINUSE. In production every caller of shutdown() is on its way to
    # process exit and the OS reclaims the fd regardless, but a method that
    # does not finish its job is something a later caller will rely on.
    _close = server.shutdown

    def shutdown_and_close() -> None:
        _close()
        server.server_close()

    server.shutdown = shutdown_and_close  # type: ignore[method-assign]
    server.routes = routes  # type: ignore[attr-defined]
    server.page = page  # type: ignore[attr-defined]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="action-page", daemon=True).start()
    return server
