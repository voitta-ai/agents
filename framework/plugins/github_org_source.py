"""GitHub org source: what shipped across an organisation's repos (issue #13).

Enumerates repos under an org and yields merged pull requests and closed issues
as Signals, for the weekly release-notes agent to summarize.

Auth comes from the `gh` CLI rather than a token in config. `gh` is already
authenticated on any machine where this runs, it refreshes itself, and it keeps
one more credential out of the environment. The cost is a hard dependency on the
binary, which is checked up front with a clear error rather than a confusing
JSON parse failure later.

**Merged PRs and closed issues, not commits.** The issue asks to look at
"commits/PRs/issues", but commits are the wrong altitude for a feature summary —
a week of `fix typo` and `bump deps` drowns the one thing worth writing about,
and squash-merged repos duplicate every PR as a commit anyway. PR titles and
bodies are what humans wrote to describe their own change, which is exactly the
input a summarizer wants. Set `include_commits: true` to add them for repos that
merge without PRs.

**No time window is applied here.** The runner's SeenStore already guarantees
each item is summarized once, so the source over-fetches a generous window and
lets dedupe do the filtering. That is also what makes a missed run harmless: the
next one picks up everything not yet seen, rather than silently skipping a week
the way a strict since-last-run timestamp would.

Private repos are excluded by default — this feeds a public blog post.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from framework.interfaces import Signal, Source
from framework.registry import register_source


class GitHubCLIError(RuntimeError):
    pass


def _gh(args: list[str]) -> list[dict]:
    if not shutil.which("gh"):
        raise GitHubCLIError(
            "github-org source needs the `gh` CLI on PATH (brew install gh; gh auth login)"
        )
    # Extend the parent environment rather than replacing it: gh reads its auth
    # from GH_TOKEN/GITHUB_TOKEN or from ~/.config/gh (which needs HOME) when
    # neither is set. A bare {"GH_PAGER": ..., "PATH": ...} env drops all of
    # that and gh reports "not logged into any GitHub hosts" even when it is.
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GH_PAGER": "cat"},
    )
    if proc.returncode != 0:
        raise GitHubCLIError(f"gh {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    retval = json.loads(proc.stdout or "[]")
    return retval


class GitHubOrgSource:
    def __init__(
        self,
        org: str,
        days: int = 14,
        include_private: bool = False,
        include_commits: bool = False,
        include_issues: bool = True,
        max_repos: int = 50,
        per_repo_limit: int = 50,
    ):
        self.org = org
        self.days = days
        self.include_private = include_private
        self.include_commits = include_commits
        self.include_issues = include_issues
        self.max_repos = max_repos
        self.per_repo_limit = per_repo_limit
        self.skipped: list[tuple[str, str]] = []

    def _repos(self) -> list[dict]:
        visibility = [] if self.include_private else ["--visibility", "public"]
        rows = _gh(
            [
                "repo", "list", self.org,
                "--limit", str(self.max_repos),
                *visibility,
                "--json", "name,isArchived,pushedAt",
            ]
        )
        retval = [r for r in rows if not r.get("isArchived")]
        return retval

    def _pulls(self, repo: str) -> list[Signal]:
        rows = _gh(
            [
                "pr", "list", "-R", f"{self.org}/{repo}",
                "--state", "merged", "--limit", str(self.per_repo_limit),
                "--json", "number,title,body,url,mergedAt,author",
            ]
        )
        signals = []
        for pr in rows:
            signals.append(
                Signal(
                    id=f"{self.org}/{repo}#pr{pr['number']}",
                    text=f"{pr['title']}\n\n{(pr.get('body') or '').strip()}",
                    url=pr.get("url", ""),
                    source="github",
                    meta={
                        "title": f"{repo}: {pr['title']}",
                        "repo": repo,
                        "kind": "pull",
                        "merged_at": pr.get("mergedAt", ""),
                        "author": (pr.get("author") or {}).get("login", ""),
                    },
                )
            )
        return signals

    def _issues(self, repo: str) -> list[Signal]:
        rows = _gh(
            [
                "issue", "list", "-R", f"{self.org}/{repo}",
                "--state", "closed", "--limit", str(self.per_repo_limit),
                "--json", "number,title,body,url,closedAt",
            ]
        )
        signals = []
        for it in rows:
            signals.append(
                Signal(
                    id=f"{self.org}/{repo}#i{it['number']}",
                    text=f"{it['title']}\n\n{(it.get('body') or '').strip()}",
                    url=it.get("url", ""),
                    source="github",
                    meta={
                        "title": f"{repo}: {it['title']}",
                        "repo": repo,
                        "kind": "issue",
                        "closed_at": it.get("closedAt", ""),
                    },
                )
            )
        return signals

    def _commits(self, repo: str) -> list[Signal]:
        rows = _gh(
            [
                "api", f"repos/{self.org}/{repo}/commits",
                "--jq", "[.[] | {sha: .sha, message: .commit.message, url: .html_url}]",
            ]
        )
        signals = []
        for c in rows[: self.per_repo_limit]:
            head = (c.get("message") or "").splitlines()[0]
            signals.append(
                Signal(
                    id=f"{self.org}/{repo}@{c['sha'][:12]}",
                    text=c.get("message", ""),
                    url=c.get("url", ""),
                    source="github",
                    meta={"title": f"{repo}: {head}", "repo": repo, "kind": "commit"},
                )
            )
        return signals

    def fetch(self) -> list[Signal]:
        self.skipped = []
        signals: list[Signal] = []
        repos = self._repos()

        attempted = 0
        succeeded = 0
        for repo in repos:
            name = repo["name"]
            for label, fn, enabled in (
                ("pulls", self._pulls, True),
                ("issues", self._issues, self.include_issues),
                ("commits", self._commits, self.include_commits),
            ):
                if not enabled:
                    continue
                attempted += 1
                try:
                    signals.extend(fn(name))
                    succeeded += 1
                except GitHubCLIError as exc:
                    # One inaccessible repo must not sink the whole run, but the
                    # gap has to be visible rather than looking like a quiet week.
                    self.skipped.append((f"{name}:{label}", str(exc)[:120]))

        # Compare feed calls against feed calls. Counting failures against the
        # *repo* count meant one failing feed per repo (say issues disabled
        # org-wide) tripped the all-failed guard even though pulls and commits
        # had succeeded everywhere -- turning a good run into a hard error.
        if attempted and not succeeded:
            raise GitHubCLIError(
                f"github-org: all {attempted} feed call(s) failed — "
                f"{self.skipped[0][1] if self.skipped else ''}"
            )

        retval = signals
        return retval


@register_source("github-org")
def build(cfg: dict) -> Source:
    org = cfg.get("org")
    if not org:
        raise RuntimeError("github-org source needs an 'org'")
    retval = GitHubOrgSource(
        org=org,
        days=int(cfg.get("days", 14)),
        include_private=bool(cfg.get("include_private", False)),
        include_commits=bool(cfg.get("include_commits", False)),
        include_issues=bool(cfg.get("include_issues", True)),
        max_repos=int(cfg.get("max_repos", 50)),
        per_repo_limit=int(cfg.get("per_repo_limit", 50)),
    )
    return retval
