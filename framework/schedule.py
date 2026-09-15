"""`agent install-schedule` / `uninstall-schedule` — the plist is the scheduler (issue #10).

The framework never runs a clock of its own: every entrypoint is run-and-exit,
and launchd decides when. What used to be a hand-written plist per agent is
generated from the workspace instead, so the cadence a config declares is the
cadence that actually runs, and a new agent is deployed by one command:

    agent install-schedule --workspace <dir> [--label <label>] [--venv <path>] [--dry-run]
    agent uninstall-schedule --workspace <dir> [--label <label>]

Config keys read from `<dir>/config.json`:

    "agent": "reddit-leads",           the default label is com.agents.<agent>
    "schedule": "every 4h",            when `agent run` fires (required)
    "approvals": {
      "schedule": "every 5m",          when `agent approvals` fires (default every 5m)
      ...
    }

A workspace gets one job, or two. `agent run` always, on `schedule`. When the
config has an `approvals` block (issue #21), `agent approvals` is its own job
on its own clock, labelled `<label>.approvals`: it has to answer a `do` within
minutes while the run may fire every few hours, so one plist cannot serve both.
Nothing else in the `approvals` block is checked here. Its bot token lives in
the login profile the job loads, not necessarily in the shell running the
install, so validating it now would fail installs that would run fine; the
approvals pass reports its own config errors in its log.

`schedule` has two forms, and anything else is a `ConfigError`:

- `every <N>m` / `every <N>h` becomes `StartInterval` (seconds). launchd counts
  from when the job was loaded, not from the top of the hour.
- A five-field cron string (`minute hour day-of-month month day-of-week`)
  becomes `StartCalendarInterval`, which is clock-aligned. `*` omits the key.
  A field may be a number, a range `A-B`, or a comma list of those; minute and
  hour also take `*/N`. Every combination of the listed values becomes one
  dict, so `0 9,17 * * 1-5` is ten dicts and `0 14 * * 3` is one. Day-of-week
  7 is Sunday, as 0 is. Not supported, and rejected rather than approximated:
  `*/N` in day, month or weekday, stepped ranges (`A-B/N`), names (`MON`,
  `JAN`), `@daily`-style macros, and restricting day-of-month and day-of-week
  together. Cron fires when *either* of those two matches; launchd's documented
  semantics for a dict carrying both are not that precise, and a schedule that
  quietly fires on different days is worse than one that refuses to install.

The generated plist, per job: `ProgramArguments` is
`/bin/bash -lc "exec <venv>/bin/agent <run|approvals> --workspace <dir>"`,
because the login profile is what supplies credentials (README, "Secrets live
in the environment"). `--venv` defaults to the venv the running `agent` lives
in (`sys.prefix`), so installing from the deployment checkout's venv points
the job at that venv. `WorkingDirectory` is the workspace; stdout and stderr go
to `<dir>/logs/run.log` / `run.error.log` (or `approvals.log` /
`approvals.error.log`); `RunAtLoad` is true and `KeepAlive` false, so an
install fires once immediately and then waits for its schedule.

Everything that can be wrong with the config, the label or the venv is checked
before any file is written. Then, per job: the plist is written to a temporary
file next to its destination, `plutil -lint`ed, the loaded job (if any) is
booted out, the temporary file is renamed over `~/Library/LaunchAgents/
<label>.plist`, and the job is bootstrapped. Re-running is therefore an update,
and a plist that fails lint never replaces a working one. Booting out a job
that is mid-pass terminates the pass: harmless for `agent run` (nothing
undelivered is marked seen), but an `agent approvals` pass killed mid-steps
leaves its proposal stuck, so re-install between passes. If the config no
longer has an `approvals` block, a previously installed `<label>.approvals`
job is removed, so an update cannot leave a stale job firing.

`uninstall-schedule` boots out and deletes both labels, whichever exist, and
does not care what the config says today. It reads the config only for the
default label; with `--label` it does not read it at all, so a workspace whose
config is broken can still be uninstalled. `--label` is needed whenever the
install used one. Finding nothing to remove is an error: a mistyped label
should not look like a successful uninstall.

`--dry-run` prints each plist, preceded by the path it would be written to,
and does nothing else: no directory, no lint, no launchctl. macOS only, by
`sys.platform`; there is no cron backend until something needs one.
"""
from __future__ import annotations

import itertools
import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.config import AgentConfig, ConfigError, load_workspace

DEFAULT_APPROVALS_SCHEDULE = "every 5m"
APPROVALS_LABEL_SUFFIX = ".approvals"
LOGS_DIRNAME = "logs"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([mh])$")
_UNIT_SECONDS = {"m": 60, "h": 3600}
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# (plist key, cron field name, lowest, highest, accepts */N)
_CRON_FIELDS = (
    ("Minute", "minute", 0, 59, True),
    ("Hour", "hour", 0, 23, True),
    ("Day", "day-of-month", 1, 31, False),
    ("Month", "month", 1, 12, False),
    ("Weekday", "day-of-week", 0, 7, False),
)

# How long to wait for a booted-out job to leave the domain before
# bootstrapping its replacement (bootout returns before launchd is done).
_BOOTOUT_WAIT_SECONDS = 5.0
_BOOTOUT_POLL_SECONDS = 0.1


class ScheduleError(RuntimeError):
    """Raised when launchd or plutil refuses what the config asked for."""


@dataclass
class Job:
    """One scheduled entrypoint of a workspace, rendered to a plist."""

    label: str
    plist: dict[str, Any]

    @property
    def path(self) -> Path:
        retval = LAUNCH_AGENTS_DIR / f"{self.label}.plist"
        return retval


def _parse_cron_field(value: str, key: str, name: str, low: int, high: int, steps: bool, where: str) -> list[int] | None:
    """Expand one cron field to its sorted values, or None for `*`."""

    def bad(reason: str) -> ConfigError:
        retval = ConfigError(f"{where}: {name} field {value!r} {reason}")
        return retval

    values: set[int] = set()
    step_match = re.fullmatch(r"\*/(\d+)", value)
    if value == "*":
        pass
    elif step_match:
        if not steps:
            raise bad("uses */N, which is only supported in minute and hour")
        step = int(step_match.group(1))
        if step < 1:
            raise bad("has a step of 0")
        values.update(range(low, high + 1, step))
    else:
        for part in value.split(","):
            range_match = re.fullmatch(r"(\d+)-(\d+)", part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start > end:
                    raise bad(f"has a backwards range {part!r}")
            elif re.fullmatch(r"\d+", part):
                start = end = int(part)
            else:
                raise bad("is not a number, a range A-B, or a comma list of those")
            if start < low or end > high:
                raise bad(f"is outside {low}-{high}")
            values.update(range(start, end + 1))

    if key == "Weekday":
        values = {0 if v == 7 else v for v in values}
    retval = None if value == "*" else sorted(values)
    return retval


def _parse_cron(text: str, where: str) -> dict[str, Any] | list[dict[str, Any]]:
    expanded: list[tuple[str, list[int]]] = []
    for raw, (key, name, low, high, steps) in zip(text.split(), _CRON_FIELDS):
        values = _parse_cron_field(raw, key, name, low, high, steps, where)
        if values is not None:
            expanded.append((key, values))

    keys = [key for key, _ in expanded]
    if "Day" in keys and "Weekday" in keys:
        raise ConfigError(
            f"{where}: restricting both day-of-month and day-of-week is not supported "
            "(cron fires when either matches); restrict one of them"
        )

    dicts = [dict(zip(keys, combo)) for combo in itertools.product(*(values for _, values in expanded))]
    retval = dicts[0] if len(dicts) == 1 else dicts
    return retval


def parse_schedule(spec: Any, where: str) -> dict[str, Any]:
    """Map a config `schedule` to the plist keys that implement it.

    `where` names the config file and key (`<path>: 'schedule'`) in errors.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError(f"{where} must be a string like 'every 4h' or '0 */4 * * *'")
    text = spec.strip()
    where = f"{where} {spec!r}"

    every = _EVERY_RE.match(text)
    if every:
        seconds = int(every.group(1)) * _UNIT_SECONDS[every.group(2)]
        if seconds < 1:
            raise ConfigError(f"{where}: the interval must be positive")
        retval = {"StartInterval": seconds}
    elif len(text.split()) == len(_CRON_FIELDS):
        retval = {"StartCalendarInterval": _parse_cron(text, where)}
    else:
        raise ConfigError(f"{where} is neither 'every <N>m' / 'every <N>h' nor a five-field cron string")
    return retval


def default_label(agent: str) -> str:
    retval = f"com.agents.{agent}"
    return retval


def _check_label(label: str) -> None:
    # The label becomes a file name under ~/Library/LaunchAgents.
    if not _LABEL_RE.match(label):
        raise ConfigError(
            f"label {label!r} must be letters, digits, '.', '_' or '-', starting with a letter or digit"
        )


def _agent_binary(venv: str | Path) -> Path:
    binary = Path(venv).expanduser().absolute() / "bin" / "agent"
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        raise ConfigError(f"no executable 'agent' in the venv: {binary} (pass --venv)")
    return binary


def _plist(label: str, binary: Path, command: str, workspace: Path, log_stem: str, schedule: dict[str, Any]) -> dict[str, Any]:
    logs = workspace / LOGS_DIRNAME
    shell = f"exec {shlex.quote(str(binary))} {command} --workspace {shlex.quote(str(workspace))}"
    retval = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "-lc", shell],
        "WorkingDirectory": str(workspace),
        "StandardOutPath": str(logs / f"{log_stem}.log"),
        "StandardErrorPath": str(logs / f"{log_stem}.error.log"),
        "RunAtLoad": True,
        "KeepAlive": False,
        **schedule,
    }
    return retval


def build_jobs(cfg: AgentConfig, label: str | None, venv: str | Path | None) -> list[Job]:
    """Everything install needs, validated, with nothing written yet."""
    job_label = label or default_label(cfg.agent)
    _check_label(job_label)
    binary = _agent_binary(venv if venv is not None else sys.prefix)

    run_schedule = parse_schedule(cfg.raw.get("schedule"), f"{cfg.path}: 'schedule'")
    jobs = [Job(job_label, _plist(job_label, binary, "run", cfg.workspace, "run", run_schedule))]

    approvals = cfg.raw.get("approvals")
    if approvals is not None:
        if not isinstance(approvals, dict):
            raise ConfigError(f"{cfg.path}: 'approvals' must be an object")
        approvals_schedule = parse_schedule(
            approvals.get("schedule", DEFAULT_APPROVALS_SCHEDULE), f"{cfg.path}: 'approvals.schedule'"
        )
        approvals_label = job_label + APPROVALS_LABEL_SUFFIX
        jobs.append(
            Job(
                approvals_label,
                _plist(approvals_label, binary, "approvals", cfg.workspace, "approvals", approvals_schedule),
            )
        )
    retval = jobs
    return retval


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ScheduleError(
            f"install-schedule writes launchd plists and only works on macOS (this is {sys.platform}); "
            "schedule 'agent run --workspace <dir>' with your platform's scheduler instead"
        )


def _domain() -> str:
    retval = f"gui/{os.getuid()}"
    return retval


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    retval = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return retval


def _is_loaded(label: str) -> bool:
    retval = _launchctl("print", f"{_domain()}/{label}").returncode == 0
    return retval


def _bootout(label: str) -> bool:
    """Unload `label` if it is loaded; True if it was. Waits until it is gone."""
    was_loaded = _is_loaded(label)
    if was_loaded:
        result = _launchctl("bootout", f"{_domain()}/{label}")
        if result.returncode != 0 and _is_loaded(label):
            raise ScheduleError(f"launchctl bootout {_domain()}/{label} failed: {result.stderr.strip()}")
        deadline = time.monotonic() + _BOOTOUT_WAIT_SECONDS
        while _is_loaded(label):
            if time.monotonic() > deadline:
                raise ScheduleError(f"{_domain()}/{label} is still loaded {_BOOTOUT_WAIT_SECONDS}s after bootout")
            time.sleep(_BOOTOUT_POLL_SECONDS)
    retval = was_loaded
    return retval


def _write_linted(job: Job) -> Path:
    """Write the plist to a temporary file beside its destination and lint it."""
    job.path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=job.path.parent, prefix=f".{job.label}.", suffix=".plist.tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(job.plist, handle, sort_keys=True)
        # mkstemp creates 0600; a plist holds no secret and is 0644 by convention.
        tmp.chmod(0o644)
        lint = subprocess.run(["plutil", "-lint", str(tmp)], capture_output=True, text=True)
        if lint.returncode != 0:
            raise ScheduleError(f"plutil -lint rejected the plist for {job.label}: {lint.stdout.strip()} {lint.stderr.strip()}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _install_job(job: Job) -> None:
    tmp = _write_linted(job)
    try:
        _bootout(job.label)
        os.replace(tmp, job.path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    result = _launchctl("bootstrap", _domain(), str(job.path))
    if result.returncode != 0:
        raise ScheduleError(
            f"launchctl bootstrap {_domain()} {job.path} failed: {result.stderr.strip()} "
            f"(the plist is in place; fix the cause and re-run install-schedule)"
        )
    print(f"installed {job.label}: {job.path}")
    print(f"  check: launchctl print {_domain()}/{job.label}")


def _remove_job(label: str) -> bool:
    """Boot out and delete `label`'s job; True if there was anything to remove."""
    path = LAUNCH_AGENTS_DIR / f"{label}.plist"
    was_loaded = _bootout(label)
    had_file = path.exists()
    if had_file:
        path.unlink()
    if was_loaded or had_file:
        print(f"removed {label}: {path}")
    retval = was_loaded or had_file
    return retval


def render(job: Job) -> str:
    retval = plistlib.dumps(job.plist, sort_keys=True).decode()
    return retval


def install_schedule(workspace: str | Path, label: str | None, venv: str | Path | None, dry_run: bool) -> int:
    cfg = load_workspace(workspace)
    jobs = build_jobs(cfg, label, venv)

    if dry_run:
        for job in jobs:
            print(f"# {job.path}")
            print(render(job), end="")
        retval = 0
        return retval

    _require_macos()
    (cfg.workspace / LOGS_DIRNAME).mkdir(parents=True, exist_ok=True)
    for job in jobs:
        _install_job(job)
    if len(jobs) == 1:
        # No approvals block: remove the approvals job an earlier install made.
        _remove_job(jobs[0].label + APPROVALS_LABEL_SUFFIX)
    retval = 0
    return retval


def uninstall_schedule(workspace: str | Path, label: str | None) -> int:
    if label is None:
        label = default_label(load_workspace(workspace).agent)
    _check_label(label)
    _require_macos()

    removed = [_remove_job(name) for name in (label, label + APPROVALS_LABEL_SUFFIX)]
    if not any(removed):
        raise ScheduleError(
            f"nothing to uninstall: neither {label} nor {label}{APPROVALS_LABEL_SUFFIX} "
            f"is loaded or has a plist in {LAUNCH_AGENTS_DIR} (pass --label if the install used one)"
        )
    retval = 0
    return retval
