"""`agent run` — the entrypoint contract (issue #1).

Single invocation, single pass, exits 0 (success) / nonzero (failure). Safe to
double-run because the framework dedupes via SeenStore. No daemon, no loop:
cron/launchd owns the clock.

`agent run --workspace <dir>` is the whole interface (issue #43): the workspace
holds `config.json`, and dedupe state defaults to `<dir>/state/seen.json`.
`--config` is gone rather than deprecated. There is one caller per agent and it
is a plist line, so a deprecation shim would only hide the plist that was never
updated. Passing it exits 2 with a message that names `--workspace`.

`agent approvals --workspace <dir>` is the second entrypoint (issue #21): same
contract, its own clock, executes proposals a trusted human has replied to.

`agent install-schedule` / `uninstall-schedule` (issue #10) write and remove the
launchd jobs that invoke those two; see `framework/schedule.py`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from framework.approvals import load_approvals_config, run_approvals
from framework.config import ConfigError, load_workspace
from framework.plugins import PluginImportFailure
from framework.registry import (
    PluginImportError,
    UnknownPluginError,
    build_destination,
    build_scorer,
    build_source,
    load_plugins,
)
from framework.runner import run
from framework.schedule import ScheduleError, install_schedule, uninstall_schedule
from framework.state import SeenStore


class _RemovedConfigOption(argparse.Action):
    """Reject `--config` with a message that says what replaced it."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            "--config was removed; pass --workspace <dir> "
            "and put the agent's config.json in that directory"
        )


def _report_failed_plugins(failures: dict[str, PluginImportFailure]) -> None:
    """Say on stderr which plugins are unavailable, before anything uses them.

    A plugin that could not be imported is tolerated (issue #39) but never
    silent: if the run later succeeds, this line is the only evidence that the
    fleet is running on an incomplete venv, and if it dies at build time the
    line sits directly above the error that names the same plugin.

    Keyed by `type:` name, so one module maps to several entries; report each
    module once.
    """
    by_module = {failure.module: failure for failure in failures.values()}
    for module in sorted(by_module):
        failure = by_module[module]
        types = ", ".join(failure.types)
        print(
            f"agent: warning: plugin module {module!r} unavailable "
            f"(types: {types}): {failure.error}",
            file=sys.stderr,
        )


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_workspace(args.workspace)

    _report_failed_plugins(load_plugins())
    source = build_source(cfg.source)
    scorer = build_scorer(cfg.scorer)
    # The approval destination keeps its proposals store in the workspace, but
    # the registry hands a factory only its own block (docs/PLUGIN-CONTRACT.md),
    # so the path is injected here.
    cfg.destination["_workspace"] = str(cfg.workspace)
    destination = build_destination(cfg.destination)

    state_path = Path(args.state) if args.state else cfg.default_state_path
    seen = SeenStore(state_path)

    retval = run(source, scorer, destination, seen, cfg.agent)
    return retval


def _cmd_approvals(args: argparse.Namespace) -> int:
    cfg = load_approvals_config(args.workspace)
    _report_failed_plugins(load_plugins())
    retval = run_approvals(cfg)
    return retval


def _cmd_install_schedule(args: argparse.Namespace) -> int:
    retval = install_schedule(args.workspace, args.label, args.venv, args.dry_run)
    return retval


def _cmd_uninstall_schedule(args: argparse.Namespace) -> int:
    retval = uninstall_schedule(args.workspace, args.label)
    return retval


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="Run a single-purpose signal agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run one pass of an agent and exit.")
    run_parser.add_argument(
        "--workspace",
        required=True,
        help="The agent's workspace directory; <dir>/config.json is loaded.",
    )
    run_parser.add_argument("--config", action=_RemovedConfigOption, help=argparse.SUPPRESS)
    run_parser.add_argument(
        "--state",
        default=None,
        help="Path to the dedupe state file (default: <workspace>/state/seen.json).",
    )
    run_parser.set_defaults(func=_cmd_run)

    approvals_parser = sub.add_parser(
        "approvals",
        help="Execute proposals a trusted user has replied 'do <id>' to, and exit.",
    )
    approvals_parser.add_argument(
        "--workspace",
        required=True,
        help="The agent's workspace directory; <dir>/config.json and <dir>/state/proposals.json.",
    )
    approvals_parser.set_defaults(func=_cmd_approvals)

    install_parser = sub.add_parser(
        "install-schedule",
        help="Write and load the workspace's launchd plist(s) (macOS).",
    )
    install_parser.add_argument(
        "--workspace",
        required=True,
        help="The agent's workspace directory; its config.json supplies 'agent' and 'schedule'.",
    )
    install_parser.add_argument(
        "--label",
        default=None,
        help="launchd label (default: com.agents.<agent>); the approvals job is <label>.approvals.",
    )
    install_parser.add_argument(
        "--venv",
        default=None,
        help="Venv whose bin/agent the job runs (default: the venv running this command).",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plist(s) and do nothing else.",
    )
    install_parser.set_defaults(func=_cmd_install_schedule)

    uninstall_parser = sub.add_parser(
        "uninstall-schedule",
        help="Unload and delete the workspace's launchd plist(s) (macOS).",
    )
    uninstall_parser.add_argument(
        "--workspace",
        required=True,
        help="The agent's workspace directory; config.json is read only for the default label.",
    )
    uninstall_parser.add_argument(
        "--label",
        default=None,
        help="launchd label the install used (default: com.agents.<agent>).",
    )
    uninstall_parser.set_defaults(func=_cmd_uninstall_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        retval = args.func(args)
    except (ConfigError, UnknownPluginError, PluginImportError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        return 2
    except ScheduleError as exc:
        print(f"agent: {exc}", file=sys.stderr)
        retval = 1
    return retval


if __name__ == "__main__":
    sys.exit(main())
