"""`{name}` templating for action steps (issue #21).

Later steps of an approved action see the results of earlier ones by name: the
`agent approvals` pass merges the proposal's payload with each step's returned
dict and hands the merged mapping to the next step, and a step renders its own
config strings against it (`"text": "Published: {url}"`).

This is plain `str.format_map`, not a template language. Format specs and
conversions work because they come for free; nothing else is added. A
placeholder with no value is an error naming the placeholder, never an empty
string: an approved post that went out reading "Published: " is exactly the
kind of quiet wrongness this repo keeps paying for.
"""
from __future__ import annotations

from typing import Any


class TemplateError(ValueError):
    """A template named a value the merged payload does not have."""


class _Strict(dict):
    def __missing__(self, key: str) -> Any:
        raise TemplateError(f"template placeholder {{{key}}} has no value")


def render_template(template: str, values: dict[str, Any]) -> str:
    """Render `template` against `values`; a missing placeholder raises."""
    try:
        retval = str(template).format_map(_Strict(values))
    except (IndexError, ValueError) as exc:
        raise TemplateError(f"template {template!r} is malformed: {exc}") from exc
    return retval
