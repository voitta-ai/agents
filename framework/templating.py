"""`{name}` templating for action steps (issue #21).

Later steps of an approved action see the results of earlier ones by name: the
`agent approvals` pass merges the proposal's payload with each step's returned
dict and hands the merged mapping to the next step, and a step renders its own
config strings against it (`"text": "Published: {url}"`).

This is `str.format`'s syntax with its field language cut down to plain
names. `str.format_map` on its own would accept `{value.__class__}` and
`{value[0]}`, walking attributes and indexes of whatever object a step put in
the payload, which is how a client or response object's internals end up in a
Slack post. So every field is parsed first with `string.Formatter().parse()`
and anything that is not a bare key of the values map is rejected by name,
before any formatting happens. Format specs (`{items:>4}`) are left alone; they
format the value, they do not reach into it. A spec can nest a field of its
own (`{items:>{width}}`), and that nested field goes through the same check.

A placeholder with no value is an error naming the placeholder, never an empty
string: an approved post that went out reading "Published: " is exactly the
kind of quiet wrongness this repo keeps paying for.
"""
from __future__ import annotations

import string
from typing import Any


class TemplateError(ValueError):
    """A template is malformed, reaches into a value, or names a missing one."""


def _fields(template: str, text: str | None = None) -> list[str]:
    """Plain field names in `text` (default: `template`), specs included.

    A format spec may itself hold a replacement field (`{a:{width}}`), which
    `format_map` resolves like any other, so the spec is parsed the same way:
    `{a:{b.__class__}}` must be refused exactly as `{b.__class__}` is.
    `template` is the whole string, for error messages.
    """
    if text is None:
        text = template
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise TemplateError(f"template {template!r} is malformed: {exc}") from exc
    retval = []
    for _literal, field_name, spec, _conversion in parsed:
        if field_name is None:
            continue
        if field_name == "" or field_name.isdigit():
            raise TemplateError(
                f"template {template!r}: positional field {{{field_name}}} is not allowed; "
                "use a name"
            )
        if "." in field_name or "[" in field_name:
            raise TemplateError(
                f"template {template!r}: field {{{field_name}}} reaches into a value; "
                "only plain names are allowed"
            )
        retval.append(field_name)
        if spec:
            retval.extend(_fields(template, spec))
    return retval


def render_template(template: str, values: dict[str, Any]) -> str:
    """Render `template` against `values`; a missing placeholder raises."""
    text = str(template)
    for name in _fields(text):
        if name not in values:
            raise TemplateError(f"template placeholder {{{name}}} has no value")
    try:
        retval = text.format_map(values)
    except ValueError as exc:
        raise TemplateError(f"template {text!r} is malformed: {exc}") from exc
    return retval
