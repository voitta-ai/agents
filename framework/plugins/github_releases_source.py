"""GitHub releases source: what an organisation *released* since the last run (issue #46).

`github-org` (#13) yields merged PRs and closed issues. Summarising those gives
a changelog. The weekly release-notes post wants release notes, and the repos
that cut releases already wrote them: the release body is the text the project
chose to describe what shipped. This source yields one Signal per release,
body intact, and nothing else.

Auth and transport are the `gh` CLI, as in `github_org_source`, including the
lesson recorded there: the subprocess environment *extends* `os.environ`
(`{**os.environ, "GH_PAGER": "cat"}`). Replacing it drops HOME and the token
and gh reports "not logged into any GitHub hosts" while it plainly is. Both
the repo listing and each repo's releases are read with `--paginate`, so an org
past 100 repos or a repo past 100 releases is read whole.

**The window is explicit, and it is state.** The other sources over-fetch and
let SeenStore dedupe. That works for a rolling feed; it does not work for
releases, where "everything a repo ever released" is unbounded and the question
is precisely "what is new since we last looked". So `since` is either an ISO
date used literally (hand re-runs: `since: "2026-09-01"`) or the string
`"state"`, meaning: read `<state_dir>/github-releases.json`, use its `last_run`,
and on the first run (no file, no `last_run`) fall back to `first_run_since`,
which is required in that mode. A release is kept if `published_at >=` the
window start, it is not a draft, it is not a prerelease unless
`include_prereleases` is set, and its tagged commit is recent enough (below).
`>=` rather than `>`: a release published in the same second the window starts
costs one duplicate that SeenStore drops, whereas `>` would lose it.

Timestamps are parsed with dateutil's `isoparse`, not `datetime.fromisoformat`:
GitHub writes a trailing `Z`, which `fromisoformat` rejects before Python 3.11,
and the project supports 3.10.

**Backfilled releases.** A release created today for an old tag has
`published_at` = today and would land in this week's window as though it had
just shipped. So a release is also skipped when its tagged commit is older than
the window start minus `max_commit_age_days` (default 30), and every such skip
is logged to stderr with `repo@tag` and the commit date, so a skipped release is
visible rather than silently absent. The slack is measured against the commit
on purpose: a release cut from a commit that sat for a week or two is ordinary;
a tag months old is a backfill.

The commit date is resolved per release, with `gh api
repos/<org>/<repo>/commits/refs/tags/<tag>` (the committer date; the endpoint
peels an annotated tag to its commit), and only for releases already inside the
`published_at` window, so a weekly run costs a handful of calls. The release's
own `created_at` would have been free, and GitHub describes it as the date of
the commit used for the release, so that was checked against the real org on
2026-09-14 before being ruled out. It is the *tag's* date. For a lightweight
tag that is the commit's date: `shmobster` v0.1.0 has `created_at`
2026-08-15T23:57:14Z, `published_at` four days later, and its commit is dated
2026-08-15T23:57:14Z. For an annotated tag it is the tagger date: `voitta-yolt`
v0.2.0, backfilled on 2026-09-14, has `created_at` 2026-07-27T23:06:34Z, which
is its tag object's tagger date, while the commit is dated 2026-07-27T22:22:37Z.
The gap there is 44 minutes, but nothing bounds it: an annotated tag created
today on a months-old commit, which is one way to backfill, would carry today's
`created_at` and pass as new.

**How `last_run` moves, and why not at fetch time.** If `fetch()` wrote
`last_run = now` the window would advance even when the run then failed to
deliver, and last week's releases would be gone from the next run's window
while never having been seen: exactly the silent skip this source exists to
prevent. So `fetch()` notes the time it *started* (started, not finished, so a
release published mid-fetch lands in the next window rather than between
windows), and `commit()` writes that time as `last_run`. The runner calls
`commit()` only after `seen.save()` has succeeded, and only on a `status=ok`
run (see `framework/runner.py`). The consequences:

  - A run that fails anywhere before `commit()` leaves `last_run` where it
    was. The next run re-fetches the same window and SeenStore drops whatever
    was in fact delivered. At-least-once, never at-most-once.
  - A `partial` run (some signal's scorer raised `ScoreError`) does not
    advance the window either: the unscored release is retried next run only
    if it is still inside the next run's window.
  - A runner that does not call `commit()` never advances the window. The
    source still works, it just re-fetches from `first_run_since` every time
    and relies on SeenStore. Correct, and increasingly slow; the summary line's
    `fetched=` count is the tell.
  - `since` as a literal date never touches the state file, in either
    direction. A hand re-run must not move the scheduled agent's window.

The fetch start time is held on the source instance, not written to the state
file for `commit()` to read back. One run's `fetch()` and `commit()` always act
on the same instance, whereas a value read from disk at commit time could be
an overlapping run's (a hand re-run while the scheduled one is in flight),
which would promote a window that other run never delivered. Two overlapping
runs can at worst move `last_run` backwards, which costs duplicates SeenStore
drops, never a skip.

The issue sketched a runner-free variant: promote a pending timestamp at the
*next* fetch if that run's ids are all in SeenStore. The source cannot see
SeenStore, and promoting unconditionally at the next fetch is wrong (the next
fetch is the next run, whether or not the previous one delivered). The smallest
honest mechanism is the optional `commit()` hook, discovered by getattr exactly
as `Destination.flush()` is, so no existing source changes.

**Where the state file lives.** `agent run` injects `state_dir =
<workspace>/state` into this block (an explicit `state_dir` wins), so the file
sits beside the dedupe state and configs need not name it. Building this source
any other way with `since: "state"` and no `state_dir` raises rather than guess
a directory. The file is `github-releases.json`:

    {"last_run": "<ISO, UTC>"}

`last_run` is absent until the first successful run. It is written to a
temporary file and renamed into place, so a crash mid-write leaves the previous
value; a file that is not a JSON object raises rather than restart the window.

**Failure policy.** The repo listing failing is fatal at once. A single repo's
releases call, or the commit lookup for one of its tags, failing is recorded
and the loop continues so that the error names *every* failing repo, then `fetch()` raises after the loop: a partial
window must not be committed as complete. Zero releases across the org is
`[]`, not an error.

Config keys:

  org                  required. The GitHub organisation.
  visibility           "public" (default), "private" or "all". Archived repos
                       are always excluded; this feeds a public post.
  include_prereleases  false by default.
  exclude_repos        list of repo names to skip (forks, mirrors).
  since                "state" (default) or an ISO date/datetime.
  first_run_since      ISO date/datetime; required when since is "state".
  max_commit_age_days  30 by default. Skip a release whose tagged commit is
                       older than the window start minus this many days.
  state_dir            directory holding github-releases.json; injected by
                       `agent run`, see above.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from dateutil.parser import isoparse

from framework.interfaces import Signal, Source
from framework.registry import register_source

STATE_FILENAME = "github-releases.json"
_VISIBILITIES = ("public", "private", "all")
DEFAULT_MAX_COMMIT_AGE_DAYS = 30


class GitHubCLIError(RuntimeError):
    pass


def _run_gh(path: str, *flags: str) -> str:
    """Run `gh api <path> <flags...>` and return its stdout."""
    if not shutil.which("gh"):
        raise GitHubCLIError(
            "github-releases source needs the `gh` CLI on PATH (brew install gh; gh auth login)"
        )
    proc = subprocess.run(
        ["gh", "api", path, *flags],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_PAGER": "cat"},
    )
    if proc.returncode != 0:
        raise GitHubCLIError(f"gh api {path} failed: {proc.stderr.strip()[:300]}")
    retval = proc.stdout
    return retval


def _gh_api(path: str) -> list[dict]:
    """Run `gh api` over every page and return the rows as one list."""
    # --slurp wraps the pages in an outer array: [[page1...], [page2...]].
    pages = json.loads(_run_gh(path, "--paginate", "--slurp") or "[]")
    retval = [row for page in pages for row in page]
    return retval


def _tag_commit_date(org: str, repo: str, tag: str) -> str:
    """Committer date of the commit a tag points at (annotated tags peeled)."""
    path = f"repos/{org}/{repo}/commits/refs/tags/{quote(tag, safe='/')}"
    retval = _run_gh(path, "--jq", ".commit.committer.date").strip()
    return retval


def _parse_iso(value: str, label: str) -> datetime:
    """ISO date or datetime -> aware UTC datetime. A bare date is midnight UTC."""
    try:
        parsed = isoparse(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"github-releases: {label} is not an ISO date: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    retval = parsed.astimezone(timezone.utc)
    return retval


def _now_iso() -> str:
    retval = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return retval


class GitHubReleasesSource:
    def __init__(
        self,
        org: str,
        state_path: Path | None,
        visibility: str = "public",
        include_prereleases: bool = False,
        exclude_repos: list[str] | None = None,
        since: str = "state",
        first_run_since: str | None = None,
        max_commit_age_days: int = DEFAULT_MAX_COMMIT_AGE_DAYS,
    ):
        self.org = org
        self.state_path = state_path
        self.visibility = visibility
        self.include_prereleases = include_prereleases
        self.exclude_repos = set(exclude_repos or [])
        self.since = since
        self.first_run_since = first_run_since
        self.max_commit_age_days = max_commit_age_days
        self.failed: list[tuple[str, str]] = []
        # This run's fetch start, promoted to last_run by commit().
        self._pending: str | None = None

    # -- state --------------------------------------------------------------

    @property
    def tracks_state(self) -> bool:
        retval = self.since == "state"
        return retval

    def _state_file(self) -> Path:
        if self.state_path is None:
            raise RuntimeError("github-releases: since is 'state' but no state_dir is set")
        retval = self.state_path
        return retval

    def _read_state(self) -> dict:
        path = self._state_file()
        retval: dict = {}
        if path.exists():
            loaded = json.loads(path.read_text())
            if not isinstance(loaded, dict):
                raise RuntimeError(f"github-releases: state file {path} is not a JSON object")
            retval = loaded
        return retval

    def _write_state(self, state: dict) -> None:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, path)

    def _window_start(self) -> datetime:
        if not self.tracks_state:
            retval = _parse_iso(self.since, "since")
            return retval
        last_run = self._read_state().get("last_run")
        if last_run:
            retval = _parse_iso(last_run, f"last_run in {self.state_path}")
        else:
            retval = _parse_iso(self.first_run_since or "", "first_run_since")
        return retval

    def commit(self) -> None:
        """Write this run's fetch start as `last_run`. The runner calls this after seen.save()."""
        if not self.tracks_state or self._pending is None:
            return
        state = self._read_state()
        state["last_run"] = self._pending
        self._write_state(state)
        self._pending = None

    # -- GitHub -------------------------------------------------------------

    def _repos(self) -> list[str]:
        type_param = "all" if self.visibility == "all" else self.visibility
        rows = _gh_api(f"orgs/{self.org}/repos?per_page=100&type={type_param}")
        names = []
        for repo in rows:
            if repo.get("archived"):
                continue
            if self.visibility != "all" and repo.get("visibility") != self.visibility:
                continue
            if repo["name"] in self.exclude_repos:
                continue
            names.append(repo["name"])
        retval = sorted(names)
        return retval

    def _releases(self, repo: str, start: datetime, oldest_commit: datetime) -> list[Signal]:
        rows = _gh_api(f"repos/{self.org}/{repo}/releases?per_page=100")
        signals = []
        for rel in rows:
            if rel.get("draft"):
                continue
            if rel.get("prerelease") and not self.include_prereleases:
                continue
            published_at = rel.get("published_at") or ""
            if not published_at:
                continue
            tag = rel["tag_name"]
            label = f"{repo}@{tag}"
            if _parse_iso(published_at, f"published_at of {label}") < start:
                continue
            # Not created_at: for an annotated tag that is the tag's date (module docstring).
            committed_at = _tag_commit_date(self.org, repo, tag)
            if _parse_iso(committed_at, f"commit date of {label}") < oldest_commit:
                print(
                    f"github-releases: skipped {label}: tag commit dated {committed_at}, "
                    f"before {oldest_commit.isoformat()} "
                    f"(max_commit_age_days={self.max_commit_age_days})",
                    file=sys.stderr,
                )
                continue
            name = (rel.get("name") or tag).strip()
            body = (rel.get("body") or "").strip()
            signals.append(
                Signal(
                    id=label,
                    text=f"{repo} {tag} — {name}\n\n{body}",
                    url=rel.get("html_url", ""),
                    source="github-releases",
                    meta={
                        "repo": repo,
                        "tag": tag,
                        "published_at": published_at,
                        "author": (rel.get("author") or {}).get("login", ""),
                    },
                )
            )
        retval = signals
        return retval

    def fetch(self) -> list[Signal]:
        self.failed = []
        self._pending = None
        start = self._window_start()
        oldest_commit = start - timedelta(days=self.max_commit_age_days)
        # Taken before any API call: a release published while this loop runs
        # falls into the *next* window rather than between the two.
        fetch_started = _now_iso()

        signals: list[Signal] = []
        for repo in self._repos():
            try:
                signals.extend(self._releases(repo, start, oldest_commit))
            except GitHubCLIError as exc:
                self.failed.append((repo, str(exc)[:200]))

        if self.failed:
            names = ", ".join(repo for repo, _ in self.failed)
            detail = "; ".join(f"{repo}: {why}" for repo, why in self.failed[:3])
            raise GitHubCLIError(
                f"github-releases: {len(self.failed)} repo(s) failed ({names}) — {detail}"
            )

        self._pending = fetch_started
        signals.sort(key=lambda s: s.meta["published_at"])
        retval = signals
        return retval


@register_source("github-releases")
def build(cfg: dict) -> Source:
    org = cfg.get("org")
    if not org:
        raise RuntimeError("github-releases source needs an 'org'")
    visibility = str(cfg.get("visibility", "public"))
    if visibility not in _VISIBILITIES:
        raise RuntimeError(
            f"github-releases: visibility must be one of {', '.join(_VISIBILITIES)}, "
            f"got {visibility!r}"
        )
    since = str(cfg.get("since", "state"))
    first_run_since = cfg.get("first_run_since")
    state_dir = cfg.get("state_dir")
    if since == "state":
        if not first_run_since:
            raise RuntimeError(
                "github-releases: since is 'state', so 'first_run_since' (ISO date) is required"
            )
        _parse_iso(str(first_run_since), "first_run_since")
        if not state_dir:
            raise RuntimeError(
                "github-releases: since is 'state', so 'state_dir' is required "
                "(`agent run` injects <workspace>/state)"
            )
    else:
        _parse_iso(since, "since")
    max_commit_age_days = cfg.get("max_commit_age_days", DEFAULT_MAX_COMMIT_AGE_DAYS)
    if (
        isinstance(max_commit_age_days, bool)
        or not isinstance(max_commit_age_days, int)
        or max_commit_age_days < 0
    ):
        raise RuntimeError(
            "github-releases: 'max_commit_age_days' must be a non-negative integer, "
            f"got {max_commit_age_days!r}"
        )
    exclude_repos = cfg.get("exclude_repos") or []
    if not isinstance(exclude_repos, list):
        raise RuntimeError("github-releases: 'exclude_repos' must be a list of repo names")
    state_path = Path(state_dir).expanduser() / STATE_FILENAME if state_dir else None
    retval = GitHubReleasesSource(
        org=str(org),
        state_path=state_path,
        visibility=visibility,
        include_prereleases=bool(cfg.get("include_prereleases", False)),
        exclude_repos=[str(r) for r in exclude_repos],
        since=since,
        first_run_since=str(first_run_since) if first_run_since else None,
        max_commit_age_days=max_commit_age_days,
    )
    return retval
