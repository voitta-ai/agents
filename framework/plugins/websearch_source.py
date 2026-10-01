"""Websearch source: a search engine as a source (issue #57).

Runs a list of queries against the Brave Search API and emits one Signal per
result. This is the generic primitive issue #30 anticipated while rescoping the
LinkedIn work: the missing piece was never a LinkedIn connector, it was
search-engine-as-source. A `site:linkedin.com/posts` query and a
`"<city> chamber mixer"` query are the same plugin.

    "source": {
      "type": "websearch",
      "api_key_env": "BRAVE_API_KEY",
      "queries": ["<city> small business networking", "site:eventbrite.com <city>"],
      "count": 10,
      "freshness": "pw",
      "fetch_page": true
    }

Why Brave rather than a scraped SERP: it is a documented API with a key, so
nothing here depends on a page's markup holding still. The retired LinkedIn
scraper broke silently against a changed DOM while still reporting successful
runs, and that failure mode is what this plugin exists not to repeat.

## Snippets, `fetch_page`, and the honest limit

A search result is a title, a URL and a short description. For the meetups
agent that is usually not enough — a listing's date, venue and cost live on the
page, not in the snippet — so `fetch_page: true` does a plain GET of each result
and appends the page's visible text.

**It is a plain GET. No JavaScript runs.** An event page rendered entirely by a
client-side framework yields nothing useful, and that is recorded rather than
hidden: `meta["page_fetched"]` is False and `meta["page_error"]` says why, so a
digest built on thin text can be explained instead of merely being disappointing.
The signal is still emitted with its snippet, because a title and URL the
operator can click is worth more than a dropped row.

Text extraction is stdlib `html.parser`, not a DOM library: this needs "the
words on the page, roughly, minus script and style", and a dependency whose
absence would take the plugin out of `load_plugins()` (issue #39) is a poor
trade for tidier whitespace.

## The dedupe id is the normalised URL

Two queries legitimately return the same page — that is what overlapping queries
are for — and the same page arrives with a tracking parameter attached, or with
and without a trailing slash, often enough to matter. So the id is the URL with
its fragment dropped, its `utm_*`/`fbclid`-class parameters stripped, its host
lowercased and a bare trailing slash removed. Without that, one event is two
signals and the digest lists it twice.

Normalisation is deliberately conservative: query parameters that are not known
tracking junk are kept, because on plenty of event sites the id *is* a query
parameter (`?event_id=123`), and stripping it would collapse every event on the
site into one signal — a silent loss, which is worse than a duplicate.

## Failures

Per-query, mirroring the reddit source's `feed_errors`: a query that fails is
recorded in `query_errors` and the run continues on the others, but **if every
query fails, `fetch()` raises** rather than reporting a cheerful zero. A rate
limit that takes out the whole run must not read as "no events this week".

A failed *page* fetch under `fetch_page` is not a failed query — the result is
still a real result — so it degrades to the snippet and is recorded in meta.

Brave's free tier is one request per second, so queries are spaced by
`delay_seconds` (default 1.1). A 429 is recorded for that query rather than
retried: at a weekly cadence, and with the whole-run guard above, failing loudly
beats sitting in a retry loop inside a cron job.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import os
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from framework.interfaces import Signal, Source
from framework.registry import register_source

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_LABEL = "websearch"
_DEFAULT_PAGE_CHARS = 4000

# Query parameters that identify a campaign, not a document. Stripped so the
# same page reached from two places dedupes as one signal. Anything not on this
# list is kept: on many event sites the id is a query parameter, and dropping
# unknown ones would collapse a whole site into a single signal.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "_ga",
    }
)

# Elements whose contents are code or styling, never prose.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})


class _TextExtractor(HTMLParser):
    """Visible text of an HTML document, roughly. See the module docstring on
    why this is stdlib rather than a DOM library."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def text(self) -> str:
        retval = " ".join(self._parts)
        return retval


def _visible_text(html: str) -> str:
    parser = _TextExtractor()
    # A malformed page must not fail the run: whatever was parsed before the
    # error is still worth scoring.
    try:
        parser.feed(html)
    except Exception:
        pass
    retval = " ".join(parser.text().split())
    return retval


def normalise_url(url: str) -> str:
    """The dedupe id for a result. See the module docstring for what is and is
    not stripped, and why the conservative choice is the safe one."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    # Trailing slashes are dropped, the bare root included: `https://host/` and
    # `https://host` are one page, and urlsplit gives them different paths ("/"
    # and ""), so leaving the root alone would make them two ids.
    path = parts.path.rstrip("/")
    retval = urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )
    return retval


class WebSearchSource:
    def __init__(
        self,
        api_key: str,
        queries: list[str],
        count: int = 10,
        freshness: str = "",
        fetch_page: bool = False,
        page_chars: int = _DEFAULT_PAGE_CHARS,
        label: str = _DEFAULT_LABEL,
        country: str = "",
        delay_seconds: float = 1.1,
        timeout: int = 30,
        page_timeout: int = 20,
        user_agent: str = "",
    ):
        self.api_key = api_key
        self.queries = queries
        self.count = count
        self.freshness = freshness
        self.fetch_page = fetch_page
        self.page_chars = page_chars
        self.label = label
        self.country = country
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.page_timeout = page_timeout
        self.user_agent = user_agent
        self.query_errors: list[tuple[str, str]] = []

    def _search(self, query: str) -> list[dict]:
        params: dict[str, object] = {"q": query, "count": self.count}
        if self.freshness:
            params["freshness"] = self.freshness
        if self.country:
            params["country"] = self.country
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        resp = requests.get(_ENDPOINT, params=params, headers=headers, timeout=self.timeout)
        if resp.status_code == 429:
            self.query_errors.append((query, "429 rate limited"))
            return []
        if not resp.ok:
            self.query_errors.append((query, f"{resp.status_code} {resp.reason}"))
            return []
        try:
            payload = resp.json()
        except ValueError as exc:
            self.query_errors.append((query, f"reply was not JSON: {exc}"))
            return []
        results = ((payload.get("web") or {}).get("results")) or []
        if not isinstance(results, list):
            self.query_errors.append((query, "web.results was not a list"))
            return []
        return results

    def _page_text(self, url: str) -> tuple[str, str]:
        """(text, error). A failed page fetch is not a failed query — the result
        is still real — so this reports rather than raises."""
        headers = {"User-Agent": self.user_agent} if self.user_agent else {}
        try:
            resp = requests.get(url, headers=headers, timeout=self.page_timeout)
        except requests.RequestException as exc:
            return ("", f"{type(exc).__name__}: {exc}")
        if not resp.ok:
            return ("", f"{resp.status_code} {resp.reason}")
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type.lower() and content_type:
            return ("", f"not HTML (Content-Type: {content_type})")
        text = _visible_text(resp.text)
        if not text:
            return ("", "no visible text (page is probably rendered by JavaScript)")
        return (text[: self.page_chars], "")

    def fetch(self) -> list[Signal]:
        self.query_errors = []
        signals: list[Signal] = []
        seen_ids: set[str] = set()

        for index, query in enumerate(self.queries):
            if index and self.delay_seconds:
                time.sleep(self.delay_seconds)
            for result in self._search(query):
                url = str(result.get("url") or "").strip()
                if not url:
                    continue
                signal_id = normalise_url(url)
                # Overlapping queries returning the same page is expected, and
                # the point of normalising: one event, one signal.
                if signal_id in seen_ids:
                    continue
                seen_ids.add(signal_id)
                signals.append(self._to_signal(result, url, signal_id, query))

        if self.queries and len(self.query_errors) == len(self.queries):
            detail = "; ".join(f"{q}: {why}" for q, why in self.query_errors[:3])
            raise RuntimeError(
                f"websearch source: all {len(self.queries)} query(ies) failed — {detail}"
            )
        if self.query_errors:
            print(
                f"websearch source: {len(self.query_errors)} of {len(self.queries)} "
                f"query(ies) failed — {'; '.join(q for q, _ in self.query_errors)}",
                file=sys.stderr,
            )

        retval = signals
        return retval

    def _to_signal(self, result: dict, url: str, signal_id: str, query: str) -> Signal:
        title = str(result.get("title") or "").strip()
        snippet = str(result.get("description") or "").strip()
        meta = {
            "title": title,
            "snippet": snippet,
            "query": query,
            "age": str(result.get("age") or ""),
            "page_age": str(result.get("page_age") or ""),
            "page_fetched": False,
            "page_error": "",
        }
        text = f"{title}. {snippet}".strip()
        if self.fetch_page:
            page_text, page_error = self._page_text(url)
            if page_text:
                meta["page_fetched"] = True
                text = f"{text}\n\n{page_text}".strip()
            else:
                meta["page_error"] = page_error
        retval = Signal(id=signal_id, text=text, url=url, source=self.label, meta=meta)
        return retval


@register_source("websearch")
def build(cfg: dict) -> Source:
    api_key_env = cfg.get("api_key_env", "BRAVE_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"websearch source needs the Brave Search API key in ${api_key_env} — "
            "it is a secret, so it is never accepted from config"
        )

    queries = cfg.get("queries")
    if not isinstance(queries, list) or not queries:
        raise RuntimeError("websearch source needs a non-empty 'queries' list")
    bad = [q for q in queries if not isinstance(q, str) or not q.strip()]
    if bad:
        raise RuntimeError("websearch source: every entry in 'queries' must be a non-empty string")

    retval = WebSearchSource(
        api_key=api_key,
        queries=[q.strip() for q in queries],
        count=int(cfg.get("count", 10)),
        freshness=str(cfg.get("freshness", "") or ""),
        fetch_page=bool(cfg.get("fetch_page", False)),
        page_chars=int(cfg.get("page_chars", _DEFAULT_PAGE_CHARS)),
        label=cfg.get("label", _DEFAULT_LABEL),
        country=str(cfg.get("country", "") or ""),
        delay_seconds=float(cfg.get("delay_seconds", 1.1)),
        timeout=int(cfg.get("timeout", 30)),
        page_timeout=int(cfg.get("page_timeout", 20)),
        user_agent=cfg.get("user_agent", ""),
    )
    return retval
