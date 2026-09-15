"""Command action: run an existing tool as one step of an approved action (issue #48).

Some things an approved proposal should do already have a working tool outside
this repo: a posting script that knows a network's escaping rules, link cards
and token expiry. Reimplementing that as a plugin gives two code paths for one
job, so this step runs the tool instead:

    {"type": "command",
     "argv": ["/usr/local/bin/some-post-tool", "--text", "{blurb} {url}"],
     "result_format": "json"}

Config keys:

    argv           list of strings, required. Each element is rendered with
                   `framework.templating.render_template` against the payload
                   (the proposal's content plus earlier steps' results).
    cwd            working directory for the tool, optional. Not templated.
    timeout        seconds, default 120. A tool still running then is killed
                   and the step fails.
    result_pattern regex with named groups, searched (`re.search`) in stdout;
                   the groups become the step's results. Use `(?m)` for `^`/`$`
                   to anchor per line.
    result_format  "json": the last non-empty stdout line is parsed as a JSON
                   object and its keys become the step's results.

At most one of `result_pattern` / `result_format` may be set; with neither, the
step returns no results.

The tool is run from an argv list, never through a shell. Post text is exactly
the kind of value that carries quotes, `$(...)` and backticks, and every one of
those has to reach the tool as the literal characters of one argument. Templating
each element separately and handing the list to `subprocess.run` keeps an element
one argument no matter what it renders to; there is no command line to split or
quote.

The environment is the agent's own, passed explicitly (`env={**os.environ}`):
a tool reads its credentials and `HOME` from there, and a hand-built env dict
would silently drop them. Under launchd `PATH` is minimal, so give `argv[0]` as
an absolute path.

Failure is loud in every direction, because the approvals runner stops the
sequence at a failed step and posts its message to the approvals channel:

- A nonzero exit fails the step with the exit code and the last 20 lines of the
  tool's stderr, so a tool's own explanation ("re-mint needed: ...") reaches the
  human verbatim.
- A timeout, or a tool that cannot be started, fails the step.
- Exit 0 whose stdout does not parse as configured also fails: no match for
  `result_pattern`, a named group that did not take part in the match, or a last
  line that is not a JSON object. A later step templating `{url}` from a result
  that never arrived is the silent failure this avoids.

Only `argv[0]` ever appears in a message this step produces. The other elements
are rendered post text and may hold anything the proposal did; stdout is not
quoted either. `subprocess.TimeoutExpired` renders the whole argv in its
message, so it is replaced rather than chained. Nothing here logs.

Contract: see docs/PLUGIN-CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from framework.interfaces import Action
from framework.registry import register_action
from framework.templating import render_template

STDERR_TAIL_LINES = 20


def _stderr_tail(stderr: str) -> str:
    lines = stderr.rstrip().splitlines()[-STDERR_TAIL_LINES:]
    retval = "\n".join(lines)
    return retval


class CommandAction:
    def __init__(
        self,
        argv: list[str],
        cwd: str | None,
        timeout: int,
        pattern: re.Pattern | None,
        result_format: str | None,
    ):
        self._argv = argv
        self._cwd = cwd
        self._timeout = timeout
        self._pattern = pattern
        self._result_format = result_format

    def _results(self, exe: str, stdout: str) -> dict[str, Any]:
        retval: dict[str, Any] = {}
        if self._pattern is not None:
            match = self._pattern.search(stdout)
            if match is None:
                raise RuntimeError(
                    f"command {exe} exited 0 but its stdout does not match result_pattern "
                    f"{self._pattern.pattern!r}"
                )
            retval = match.groupdict()
            unset = sorted(name for name, value in retval.items() if value is None)
            if unset:
                raise RuntimeError(
                    f"command {exe} exited 0 but result_pattern group(s) {', '.join(unset)} "
                    "did not match anything in its stdout"
                )
        elif self._result_format == "json":
            lines = [line for line in stdout.splitlines() if line.strip()]
            if not lines:
                raise RuntimeError(
                    f"command {exe} exited 0 with no stdout; result_format json needs a JSON "
                    "object on the last line"
                )
            try:
                parsed = json.loads(lines[-1])
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"command {exe} exited 0 but the last line of its stdout is not JSON: "
                    f"{exc.msg} at column {exc.colno}"
                ) from None
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    f"command {exe} exited 0 but the last line of its stdout is a JSON "
                    f"{type(parsed).__name__}, not an object"
                )
            retval = parsed
        return retval

    def execute(self, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        argv = [render_template(arg, payload) for arg in self._argv]
        exe = argv[0]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"command {exe} did not finish within {self._timeout}s and was killed"
            ) from None
        except OSError as exc:
            raise RuntimeError(f"command {exe} could not be started: {exc}") from exc
        if proc.returncode != 0:
            tail = _stderr_tail(proc.stderr)
            detail = f"; last stderr lines:\n{tail}" if tail else " with no stderr"
            raise RuntimeError(f"command {exe} failed with exit code {proc.returncode}{detail}")
        retval = self._results(exe, proc.stdout)
        return retval


@register_action("command")
def build(cfg: dict) -> Action:
    argv = cfg.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError(
            "command action needs 'argv', a non-empty list of strings (it is never run "
            "through a shell)"
        )
    if not all(isinstance(arg, str) for arg in argv):
        raise ValueError("command action: every 'argv' element must be a string")
    cwd = cfg.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("command action: 'cwd' must be a string")
    pattern_src = cfg.get("result_pattern")
    result_format = cfg.get("result_format")
    if pattern_src is not None and result_format is not None:
        raise ValueError("command action: set at most one of 'result_pattern' and 'result_format'")
    if result_format is not None and result_format != "json":
        raise ValueError(
            f"command action: result_format {result_format!r} is not supported; use \"json\""
        )
    pattern = None
    if pattern_src is not None:
        try:
            pattern = re.compile(pattern_src)
        except (re.error, TypeError) as exc:
            raise ValueError(f"command action: result_pattern does not compile: {exc}") from exc
        if not pattern.groupindex:
            raise ValueError(
                "command action: result_pattern has no named groups (?P<name>...), so it "
                "would produce no results"
            )
    retval = CommandAction(
        argv=argv,
        cwd=cwd,
        timeout=int(cfg.get("timeout", 120)),
        pattern=pattern,
        result_format=result_format,
    )
    return retval
