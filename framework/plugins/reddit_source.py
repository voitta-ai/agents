"""Reddit source — the v1 reference Source implementation (issue #2).

Ported from hq/leads/signals/reddit_monitor.py. RSS only: no API key, no OAuth,
no PRAW. Each configured subreddit contributes its newest posts, optionally
narrowed by per-subreddit search terms.

Two things carried over from the reference because both were learned the hard
way:

  - **Reddit 403s browser-spoofing User-Agents.** Their API policy wants a
    descriptive bot UA, so send one. A Chrome-lookalike UA gets blocked.
  - **Search RSS feeds have been 403ing since roughly March 2026.** A subreddit
    with no `searches` (plain `/new.rss`) is the reliable configuration.

Measured while porting, and worse than the reference implies: **`/new.rss`
itself now rate-limits hard**, returning 429 after only a couple of requests
from one IP. Two consequences shape this implementation:

  - Retry with backoff, honouring `Retry-After` when Reddit sends it.
  - A feed that still fails is recorded in `feed_errors` and the run continues,
    but the failure is *not* swallowed. The reference wrapped everything in
    `except Exception: return []`, so a fully rate-limited run looked exactly
    like a quiet day on Reddit. If every feed fails, `fetch()` raises rather
    than reporting a cheerful zero.

If the rate limiting proves permanent, v1's no-API-key premise needs revisiting
in favour of authenticated API access.

Scoring deliberately does **not** live here, unlike the reference script, which
scored inline. Under the plugin contract a Source only produces Signals; the
Scorer decides what they are worth.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import re
import time
from urllib.parse import quote

import feedparser
import requests

from framework.interfaces import Signal, Source
from framework.registry import register_source

_DEFAULT_USER_AGENT = "voitta-agents:reddit-source/1.0 (+https://github.com/voitta-ai/agents)"
_SUBREDDIT_RE = re.compile(r"/r/(\w+)/")


def _new_url(subreddit: str) -> str:
    retval = f"https://www.reddit.com/r/{subreddit}/new.rss"
    return retval


def _search_url(subreddit: str, search: str) -> str:
    retval = (
        f"https://old.reddit.com/r/{subreddit}/search.rss"
        f"?q={quote(search)}&restrict_sr=1&sort=new"
    )
    return retval


def _extract_subreddit(url: str) -> str:
    match = _SUBREDDIT_RE.search(url or "")
    retval = match.group(1) if match else ""
    return retval


class RedditSource:
    def __init__(
        self,
        subreddits: dict[str, dict],
        user_agent: str = _DEFAULT_USER_AGENT,
        delay_seconds: float = 2.0,
        timeout: int = 15,
        limit_per_feed: int = 25,
        max_retries: int = 2,
    ):
        self.subreddits = subreddits
        self.user_agent = user_agent
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.limit_per_feed = limit_per_feed
        self.max_retries = max_retries
        self.feed_errors: list[tuple[str, str]] = []

    def _get(self, url: str) -> requests.Response | None:
        """GET with backoff on 429. Returns None if the feed stays unavailable."""
        delay = self.delay_seconds
        for attempt in range(self.max_retries + 1):
            time.sleep(delay)
            resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
            if resp.status_code == 429:
                if attempt == self.max_retries:
                    self.feed_errors.append((url, "429 rate limited"))
                    return None
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else delay * 4
                continue
            if resp.status_code == 403:
                self.feed_errors.append((url, "403 blocked (search feeds are blocked)"))
                return None
            if not resp.ok:
                self.feed_errors.append((url, f"{resp.status_code} {resp.reason}"))
                return None
            return resp
        return None

    def _fetch_feed(self, url: str) -> list[dict]:
        resp = self._get(url)
        if resp is None:
            return []

        feed = feedparser.parse(resp.content)
        entries = []
        for entry in feed.entries[: self.limit_per_feed]:
            entries.append(
                {
                    "id": entry.get("id", ""),
                    "title": entry.get("title", ""),
                    "content": entry.get("summary", ""),
                    "author": entry.get("author", ""),
                    "url": entry.get("link", ""),
                    "published": entry.get("published", ""),
                }
            )
        return entries

    def _to_signal(self, post: dict, subreddit: str) -> Signal:
        title = post.get("title", "")
        content = post.get("content", "")
        retval = Signal(
            # Reddit's RSS id is already stable and globally unique; fall back to
            # the permalink so a feed without ids still dedupes sanely.
            id=post.get("id") or post.get("url", ""),
            text=f"{title}\n\n{content}".strip(),
            url=post.get("url", ""),
            source="reddit",
            meta={
                "title": title,
                "author": post.get("author", ""),
                "published": post.get("published", ""),
                "subreddit": _extract_subreddit(post.get("url", "")) or subreddit,
            },
        )
        return retval

    def fetch(self) -> list[Signal]:
        self.feed_errors = []
        signals: list[Signal] = []
        seen_ids: set[str] = set()
        attempted = 0

        for subreddit, cfg in self.subreddits.items():
            searches = (cfg or {}).get("searches") or []
            urls = [_search_url(subreddit, s) for s in searches] or [_new_url(subreddit)]
            for url in urls:
                attempted += 1
                for post in self._fetch_feed(url):
                    signal = self._to_signal(post, subreddit)
                    # The same post can surface under several search terms.
                    if not signal.id or signal.id in seen_ids:
                        continue
                    seen_ids.add(signal.id)
                    signals.append(signal)

        # A totally failed run must not masquerade as a quiet one.
        if attempted and len(self.feed_errors) == attempted:
            detail = "; ".join(f"{url}: {why}" for url, why in self.feed_errors[:3])
            raise RuntimeError(f"reddit source: all {attempted} feed(s) failed — {detail}")

        retval = signals
        return retval


def _normalize_subreddits(declared: object) -> dict[str, dict]:
    """Accept either the plain list form or the mapping-with-searches form.

        subreddits: [construction, smallbusiness]

        subreddits:
          construction:
            searches: [paperwork, invoicing]

    The list form fetches /new.rss, which is the reliable path now that search
    feeds 403.
    """
    if isinstance(declared, dict):
        retval = {name: (cfg or {}) for name, cfg in declared.items()}
    elif isinstance(declared, list):
        retval = {name: {} for name in declared}
    else:
        retval = {}
    return retval


@register_source("reddit")
def build(cfg: dict) -> Source:
    subreddits = _normalize_subreddits(cfg.get("subreddits"))
    if not subreddits:
        raise RuntimeError("reddit source needs a non-empty 'subreddits' list or mapping")
    retval = RedditSource(
        subreddits=subreddits,
        user_agent=cfg.get("user_agent", _DEFAULT_USER_AGENT),
        delay_seconds=float(cfg.get("delay_seconds", 2.0)),
        timeout=int(cfg.get("timeout", 15)),
        limit_per_feed=int(cfg.get("limit_per_feed", 25)),
        max_retries=int(cfg.get("max_retries", 2)),
    )
    return retval
