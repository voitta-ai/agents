"""Config loading for the `agent run` entrypoint.

An agent is declared by a *workspace*: one directory per deployed agent, outside
any git checkout, holding everything the agent owns at runtime (issue #43):

    <root>/
      _shared/            cross-agent files; llm.json is the LLM waterfall (framework/llm.py)
      <agent-name>/       the workspace
        config.json       the agent's declaration; the only file a human edits
        state/            SeenStore and any other per-agent state
        logs/             stdout/stderr when scheduled

`config.json` is a single agent's declaration: which source/scorer/destination
to build, plus each plugin's own settings. The framework validates only the
shape it needs to resolve plugins; everything else in a component block is
opaque and handed to that plugin's factory verbatim (see docs/PLUGIN-CONTRACT.md).

Config is JSON, not YAML. YAML was the original format, but the only thing it
bought was comments, and those comments were where decisions went to be lost
(nothing could read them back). JSON is in the standard library, so the
framework has one fewer dependency to break a fresh venv, and a comment worth
keeping is written as a `"_comment"` key the framework ignores.

The workspaces root (the parent of a workspace, where `_shared/` lives) is only
resolved by name for helpers that take an agent *name* rather than a path;
`agent run` always takes the explicit `--workspace` path and derives `root`
from it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"
STATE_DIRNAME = "state"
SEEN_FILENAME = "seen.json"


class ConfigError(ValueError):
    """Raised when a config file is missing required framework-level shape."""


@dataclass
class AgentConfig:
    """Parsed, framework-validated view of one agent's workspace config."""

    agent: str
    source: dict[str, Any]
    scorer: dict[str, Any]
    destination: dict[str, Any]
    path: Path
    workspace: Path
    root: Path
    raw: dict[str, Any]

    @property
    def dir(self) -> Path:
        """Alias of `workspace`, kept so pre-workspace callers still resolve."""
        retval = self.workspace
        return retval

    @property
    def default_state_path(self) -> Path:
        retval = self.workspace / STATE_DIRNAME / SEEN_FILENAME
        return retval


def default_workspaces_root() -> Path:
    """Where workspaces live when only an agent name is given.

    `$AGENT_WORKSPACES_ROOT`, else `~/agent-workspaces`. `agent run` never uses
    this: it takes an explicit `--workspace` path.
    """
    configured = os.environ.get("AGENT_WORKSPACES_ROOT")
    if configured:
        retval = Path(configured).expanduser().resolve()
    else:
        retval = (Path.home() / "agent-workspaces").resolve()
    return retval


def _require_component(raw: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    block = raw.get(key)
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: '{key}' must be an object with a 'type' key")
    if not block.get("type"):
        raise ConfigError(f"{path}: '{key}.type' is required")
    return block


def load_workspace(workspace: str | Path) -> AgentConfig:
    """Load and framework-validate `<workspace>/config.json`."""
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise ConfigError(f"workspace directory not found: {workspace_path}")
    config_path = workspace_path / CONFIG_FILENAME
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        loaded = json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{config_path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{config_path}: top level must be an object")

    agent = loaded.get("agent")
    if not agent:
        raise ConfigError(f"{config_path}: 'agent' (name) is required")

    retval = AgentConfig(
        agent=str(agent),
        source=_require_component(loaded, "source", config_path),
        scorer=_require_component(loaded, "scorer", config_path),
        destination=_require_component(loaded, "destination", config_path),
        path=config_path,
        workspace=workspace_path,
        root=workspace_path.parent,
        raw=loaded,
    )
    return retval
