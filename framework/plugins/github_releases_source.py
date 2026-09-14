"""GitHub releases source: what an organisation *released* since the last run (issue #46).

`github-org` (#13) yields merged PRs and closed issues. Summarising those gives
a changelog. The weekly release-notes post wants release notes, and the repos
that cut releases already wrote them: the release body is the text the project
chose to describe what shipped. This source yields one Signal per release,
body intact, and nothing else.

Auth and transport are the `gh` CLI, as in `github_org_source`, including the
lesson recorded there: the subprocess environment *extends* `os.environ`
(`{**os.environ, "GH_PAGER": "cat"}`). Replacing it drops HOME and the token
and gh reports "not logged into any GitHub hosts" while it plainly is.

**The window is explicit, and it is state.** The other sources over-fetch and
let SeenStore dedupe. That works for a rolling feed; it does not work for
releases, where "everything a repo ever released" is unbounded and the question
is precisely "what is new since we last looked". So `since` is either an ISO
date used literally (hand re-runs: `since: "2026-09-01"`) or the string
`"state"`, meaning: read `<state_dir>/github-releases.json`, use its `last_run`,
and on the first run (no file, no `last_run`) fall back to `first_run_since`,
which is required in that mode. A release is kept if `published_at >=` the
window start, it is not a draft, and it is not a prerelease unless
`include_prereleases` is set. `>=` rather than `>`: a release published in the
same second the window starts costs one duplicate that SeenStore drops, whereas
`>` would lose it.

**How `last_run` moves, and why not at fetch time.** If `fetch()` wrote
`last_run = now` the window would advance even when the run then failed to
deliver, and last week's releases would be gone from the next run's window
while never having been seen: exactly the silent skip this source exists to
prevent. So `fetch()` records the time it *started* as `pending` (started, not
finished, so a release published mid-fetch lands in the next window rather than
between windows), and `pending` is promoted to `last_run` only by `commit()`,
which the runner calls after `seen.save()` has succeeded (see
`framework/runner.py`). The consequences:

  - A run that fails anywhere before `commit()` leaves `last_run` where it
    was. The next run re-fetches the same window and SeenStore drops whatever
    was in fact delivered. At-least-once, never at-most-once.
  - A runner that does not call `commit()` never advances the window. The
    source still works, it just re-fetches from `first_run_since` every time
    and relies on SeenStore. Correct, and increasingly slow; the summary line's
    `fetched=` count is the tell.
  - `since` as a literal date never touches the state file, in either
    direction. A hand re-run must not move the scheduled agent's window.

The issue sketched a runner-free variant: promote `pending` at the *next*
fetch if that run's ids are all in SeenStore. The source cannot see SeenStore,
and promoting unconditionally at the next fetch is wrong (the next fetch is the
next run, whether or not the previous one delivered). The smallest honest
mechanism is the optional `commit()` hook, discovered by getattr exactly as
`Destination.flush()` is, so no existing source changes.

**Where the state file lives.** `state_dir` in this block. Issue #43 introduces
workspaces and will have the CLI inject the workspace's `state/` directory into
the source block; until it lands, set `state_dir` explicitly (default:
`<cwd>/state`). The file is `github-releases.json`:

    {"last_run": "<ISO, UTC>", "pending": "<ISO, UTC>"}

`pending` is present between a fetch and its commit; `last_run` is absent until
the first successful run.

**Failure policy.** The repo listing failing is fatal at once. A single repo's
releases call failing is recorded and the loop continues so that the error
names *every* failing repo, then `fetch()` raises after the loop: a partial
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
  state_dir            directory holding github-releases.json; see above.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from framework.interfaces import Signal, Source
from framework.registry import register_source

STATE_FILENAME = "github-releases.json"
_VISIBILITIES = ("public", "private", "all")


class GitHubCLIError(RuntimeError):
    pass


def _gh_api(path: str) -> list[dict]:
    """Run `gh api` over every page and return the rows as one list."""
    if not shutil.which("gh"):
        raise GitHubCLIError(
            "github-releases source needs the `gh` CLI on PATH (brew install gh; gh auth login)"
        )
    args = ["api", path, "--paginate", "--slurp"]
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_PAGER": "cat"},
    )
    if proc.returncode != 0:
        raise GitHubCLIError(f"gh api {path} failed: {proc.stderr.strip()[:300]}")
    # --slurp wraps the pages in an outer array: [[page1...], [page2...]].
    pages = json.loads(proc.stdout or "[]")
    retval = [row for page in pages for row in page]
    return retval


def _parse_iso(value: str, label: str) -> datetime:
    """ISO date or datetime -> aware UTC datetime. A bare date is midnight UTC."""
    try:
        parsed = datetime.fromisoformat(value)
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
        state_path: Path,
        visibility: str = "public",
        include_prereleases: bool = False,
        exclude_repos: list[str] | None = None,
        since: str = "state",
        first_run_since: str | None = None,
    ):
        self.org = org
        self.state_path = state_path
        self.visibility = visibility
        self.include_prereleases = include_prereleases
        self.exclude_repos = set(exclude_repos or [])
        self.since = since
        self.first_run_since = first_run_since
        self.failed: list[tuple[str, str]] = []

    # -- state --------------------------------------------------------------

    @property
    def tracks_state(self) -> bool:
        retval = self.since == "state"
        return retval

    def _read_state(self) -> dict:
        retval: dict = {}
        if self.state_path.exists():
            loaded = json.loads(self.state_path.read_text())
            if not isinstance(loaded, dict):
                raise RuntimeError(
                    f"github-releases: state file {self.state_path} is not a JSON object"
                )
            retval = loaded
        return retval

    def _write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, self.state_path)

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
        """Promote `pending` to `last_run`. The runner calls this after seen.save()."""
        if not self.tracks_state:
            return
        state = self._read_state()
        pending = state.pop("pending", None)
        if pending:
            state["last_run"] = pending
            self._write_state(state)

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

    def _releases(self, repo: str, start: datetime) -> list[Signal]:
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
            if _parse_iso(published_at, f"published_at of {repo}@{rel.get('tag_name')}") < start:
                continue
            tag = rel["tag_name"]
            name = (rel.get("name") or tag).strip()
            body = (rel.get("body") or "").strip()
            signals.append(
                Signal(
                    id=f"{repo}@{tag}",
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
        return signals

    def fetch(self) -> list[Signal]:
        self.failed = []
        start = self._window_start()
        # Taken before any API call: a release published while this loop runs
        # falls into the *next* window rather than between the two.
        fetch_started = _now_iso()

        signals: list[Signal] = []
        for repo in self._repos():
            try:
                signals.extend(self._releases(repo, start))
            except GitHubCLIError as exc:
                self.failed.append((repo, str(exc)[:200]))

        if self.failed:
            names = ", ".join(repo for repo, _ in self.failed)
            detail = "; ".join(f"{repo}: {why}" for repo, why in self.failed[:3])
            raise GitHubCLIError(
                f"github-releases: {len(self.failed)} repo(s) failed ({names}) — {detail}"
            )

        if self.tracks_state:
            state = self._read_state()
            state["pending"] = fetch_started
            self._write_state(state)

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
    if since == "state":
        if not first_run_since:
            raise RuntimeError(
                "github-releases: since is 'state', so 'first_run_since' (ISO date) is required"
            )
        _parse_iso(str(first_run_since), "first_run_since")
    else:
        _parse_iso(since, "since")
    exclude_repos = cfg.get("exclude_repos") or []
    if not isinstance(exclude_repos, list):
        raise RuntimeError("github-releases: 'exclude_repos' must be a list of repo names")
    state_dir = Path(cfg.get("state_dir") or (Path.cwd() / "state")).expanduser()
    retval = GitHubReleasesSource(
        org=str(org),
        state_path=state_dir / STATE_FILENAME,
        visibility=visibility,
        include_prereleases=bool(cfg.get("include_prereleases", False)),
        exclude_repos=[str(r) for r in exclude_repos],
        since=since,
        first_run_since=str(first_run_since) if first_run_since else None,
    )
    return retval
