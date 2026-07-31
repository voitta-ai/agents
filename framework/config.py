"""Config loading for the `agent run` entrypoint.

A config file is a single agent's declaration: which source/scorer/destination
to build, plus each plugin's own settings. The framework validates only the
shape it needs to resolve plugins; everything else in a component block is
opaque and handed to that plugin's factory verbatim (see docs/PLUGIN-CONTRACT.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a config file is missing required framework-level shape."""


@dataclass
class AgentConfig:
    """Parsed, framework-validated view of one agent's config file."""

    agent: str
    source: dict[str, Any]
    scorer: dict[str, Any]
    destination: dict[str, Any]
    path: Path
    raw: dict[str, Any]

    @property
    def dir(self) -> Path:
        retval = self.path.parent
        return retval


def _require_component(raw: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    block = raw.get(key)
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: '{key}' must be a mapping with a 'type' key")
    if not block.get("type"):
        raise ConfigError(f"{path}: '{key}.type' is required")
    return block


def load_config(path: str | Path) -> AgentConfig:
    """Load and framework-validate an agent config file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        loaded = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    agent = loaded.get("agent")
    if not agent:
        raise ConfigError(f"{config_path}: 'agent' (name) is required")

    retval = AgentConfig(
        agent=str(agent),
        source=_require_component(loaded, "source", config_path),
        scorer=_require_component(loaded, "scorer", config_path),
        destination=_require_component(loaded, "destination", config_path),
        path=config_path,
        raw=loaded,
    )
    return retval
