"""`agent run` — the entrypoint contract (issue #1).

Single invocation, single pass, exits 0 (success) / nonzero (failure). Safe to
double-run because the framework dedupes via SeenStore. No daemon, no loop:
cron/launchd owns the clock.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from framework.config import ConfigError, load_config
from framework.registry import (
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


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)

    load_plugins()
    source = build_source(cfg.source)
    scorer = build_scorer(cfg.scorer)
    destination = build_destination(cfg.destination)

    state_path = Path(args.state) if args.state else _default_state_path(cfg.dir, cfg.agent)
    seen = SeenStore(state_path)

    retval = run(source, scorer, destination, seen, cfg.agent)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        retval = args.func(args)
    except (ConfigError, UnknownPluginError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        return 2
    return retval


if __name__ == "__main__":
    sys.exit(main())
