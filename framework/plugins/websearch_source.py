"""Websearch source: a search engine as a source (issue #57).

Runs a list of queries against the Brave Search API and emits one Signal per
result. This is the generic primitive issue #30 anticipated while rescoping the
LinkedIn work: the missing piece was never a LinkedIn connector, it was
search-engine-as-source. A `site:linkedin.com/posts` query and a
`"<city> chamber mixer"` query are the same plugin.

    "source": {
      "type": "websearch",
      "backend": "firecrawl",
      "firecrawl_api_key_env": "FIRECRAWL_API_KEY",
      "api_key_env": "BRAVE_API_KEY",
      "queries": ["<city> small business networking", "site:eventbrite.com <city>"],
      "count": 10,
      "freshness": "pw",
      "fetch_page": true
    }

Why a search API rather than a scraped SERP: it is documented and takes a key,
so nothing here depends on a page's markup holding still. The retired LinkedIn
scraper broke silently against a changed DOM while still reporting successful
runs, and that failure mode is what this plugin exists not to repeat.

## Two backends, and when the second one matters

`backend` is `brave` (the default, unchanged) or `firecrawl`.

The difference that justifies the second one is **quoted phrases**. A query that
finds a business owner identifying themselves is recognisable by its
construction rather than its topic -- `"I own a"`, `"I run a small"` -- so the
engine has to honour the quotes. Firecrawl does. Brave mostly does (measured
2026-11-12: 72-100% of on-site snippets contained the quoted phrase) and honours
`site:`, but returns nothing at all for `site:reddit.com/r/<sub>`.

With `backend: "firecrawl"` each query goes to Firecrawl, and once Firecrawl
answers 402 (credits exhausted) the rest of the run uses Brave -- including the
query that hit the 402, which is retried there immediately. Exhaustion is sticky
for the run and announced on stderr once, not once per query. If there is no
Brave key to fall back to, the 402 is recorded as that query's error and the
run carries on under the whole-run guard below.

**Only a 402 falls back.** A Firecrawl 429 or 500 is recorded as a query error
like any other and is *not* answered by Brave. Credits running out is a
permanent condition for the rest of the run, and continuing is strictly better
than stopping; a transient error is not, and quietly swapping the engine
mid-run would change what a quoted phrase means with nothing in the log saying
so. `meta["backend"]` on every signal names the engine that returned it, so a
run that straddles the switch can still be read.

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

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/search"
_BACKENDS = ("brave", "firecrawl")
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
        backend: str = "brave",
        firecrawl_api_key: str = "",
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
        self.backend = backend
        self.firecrawl_api_key = firecrawl_api_key
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
        # Sticky for the run: once Firecrawl says the credits are gone, asking
        # it again on every remaining query only wastes the run's time.
        self._firecrawl_exhausted = False

    def _search(self, query: str) -> list[dict]:
        """The configured backend's results, falling back to Brave on a 402."""
        if self.backend != "firecrawl" or self._firecrawl_exhausted:
            return self._search_brave(query)

        results, exhausted = self._search_firecrawl(query)
        if not exhausted:
            return results

        self._firecrawl_exhausted = True
        print(
            "websearch source: firecrawl credits exhausted; the rest of this run "
            f"{'uses brave' if self.api_key else 'has no backend left'}",
            file=sys.stderr,
        )
        return self._search_brave(query)

    def _search_firecrawl(self, query: str) -> tuple[list[dict], bool]:
        """(results, credits_exhausted).

        The flag is separate from an empty list because the two mean opposite
        things: no results for this query, versus no results for any query from
        here on. Only the second is worth switching engines over.
        """
        body = {"query": query, "limit": self.count, "sources": ["web"]}
        headers = {"Authorization": f"Bearer {self.firecrawl_api_key}"}
        resp = requests.post(
            _FIRECRAWL_ENDPOINT, json=body, headers=headers, timeout=self.timeout
        )
        if resp.status_code == 402:
            return ([], True)
        if not resp.ok:
            self.query_errors.append((query, f"firecrawl {resp.status_code} {resp.reason}"))
            return ([], False)
        try:
            payload = resp.json()
        except ValueError as exc:
            self.query_errors.append((query, f"firecrawl reply was not JSON: {exc}"))
            return ([], False)
        if not payload.get("success", True):
            self.query_errors.append((query, f"firecrawl: {payload.get('error')}"))
            return ([], False)
        data = payload.get("data") or {}
        rows = data.get("web") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            self.query_errors.append((query, "firecrawl data.web was not a list"))
            return ([], False)
        # Normalised to the shape `_to_signal` already reads, so one code path
        # builds the Signal whichever engine answered. Firecrawl reports no age
        # for a result; the keys stay absent rather than being faked.
        results = [
            {
                "url": row.get("url", ""),
                "title": row.get("title", ""),
                "description": row.get("description", ""),
                "backend": "firecrawl",
            }
            for row in rows
            if isinstance(row, dict)
        ]
        return (results, False)

    def _search_brave(self, query: str) -> list[dict]:
        # Reachable with no key only after a Firecrawl 402, since `build()`
        # requires the key when Brave is the configured backend. Checked here
        # rather than at the fallback because it has to hold for *every*
        # remaining query, not just the one that hit the 402: sending them with
        # an empty token would spend the rest of the run collecting 401s.
        if not self.api_key:
            self.query_errors.append(
                (query, "402 firecrawl credits exhausted, and no Brave key to fall back to")
            )
            return []

        params: dict[str, object] = {"q": query, "count": self.count}
        if self.freshness:
            params["freshness"] = self.freshness
        if self.country:
            params["country"] = self.country
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        resp = requests.get(
            _BRAVE_ENDPOINT, params=params, headers=headers, timeout=self.timeout
        )
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
        for result in results:
            if isinstance(result, dict):
                result["backend"] = "brave"
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
        self._firecrawl_exhausted = False
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
            "backend": str(result.get("backend") or ""),
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
    backend = str(cfg.get("backend", "brave") or "brave").strip().lower()
    if backend not in _BACKENDS:
        raise RuntimeError(
            f"websearch source: 'backend' must be one of {', '.join(_BACKENDS)}, "
            f"not {backend!r}"
        )

    api_key_env = cfg.get("api_key_env", "BRAVE_API_KEY")
    api_key = os.environ.get(api_key_env) or ""
    firecrawl_api_key_env = cfg.get("firecrawl_api_key_env", "FIRECRAWL_API_KEY")
    firecrawl_api_key = ""

    if backend == "firecrawl":
        firecrawl_api_key = os.environ.get(firecrawl_api_key_env) or ""
        if not firecrawl_api_key:
            raise RuntimeError(
                f"websearch source needs the Firecrawl API key in "
                f"${firecrawl_api_key_env} — it is a secret, so it is never "
                "accepted from config"
            )
        # The Brave key stays optional here: it is only the 402 fallback, and
        # refusing to start without it would make the backend that needs no
        # fallback impossible to configure.
    elif not api_key:
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
        backend=backend,
        firecrawl_api_key=firecrawl_api_key,
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
