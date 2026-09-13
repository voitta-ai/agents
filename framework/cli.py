"""`agent run` — the entrypoint contract (issue #1).

Single invocation, single pass, exits 0 (success) / nonzero (failure). Safe to
double-run because the framework dedupes via SeenStore. No daemon, no loop:
cron/launchd owns the clock.

`agent approvals --workspace <dir>` is the second entrypoint (issue #21): same
contract, its own clock, executes proposals a trusted human has replied to.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from framework.approvals import load_approvals_config, run_approvals
from framework.config import ConfigError, load_config
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
from framework.state import SeenStore


def _default_state_path(config_dir: Path, agent: str) -> Path:
    retval = config_dir / f".seen-{agent}.json"
    return retval


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
    cfg = load_config(args.config)

    _report_failed_plugins(load_plugins())
    source = build_source(cfg.source)
    scorer = build_scorer(cfg.scorer)
    # The approval destination keeps its proposals store in the workspace, but
    # the registry hands a factory only its own block (docs/PLUGIN-CONTRACT.md),
    # so the path is injected here. `workspace` is the attribute #43 introduces;
    # `dir` is the pre-workspace config directory.
    workspace = getattr(cfg, "workspace", None) or cfg.dir
    cfg.destination["_workspace"] = str(workspace)
    destination = build_destination(cfg.destination)

    state_path = Path(args.state) if args.state else _default_state_path(cfg.dir, cfg.agent)
    seen = SeenStore(state_path)

    retval = run(source, scorer, destination, seen, cfg.agent)
    return retval


def _cmd_approvals(args: argparse.Namespace) -> int:
    cfg = load_approvals_config(args.workspace)
    _report_failed_plugins(load_plugins())
    retval = run_approvals(cfg)
    return retval


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="Run a single-purpose signal agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run one pass of an agent and exit.")
    run_parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the agent's config.yaml (default: ./config.yaml).",
    )
    run_parser.add_argument(
        "--state",
        default=None,
        help="Path to the dedupe state file (default: <config-dir>/.seen-<agent>.json).",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        retval = args.func(args)
    except (ConfigError, UnknownPluginError, PluginImportError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        return 2
    return retval


if __name__ == "__main__":
    sys.exit(main())
