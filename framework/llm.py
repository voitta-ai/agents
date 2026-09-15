"""LLM access through a vendor waterfall on LiteLLM's Router (issue #44).

Ported from shmobster's `llm.py`, which we also own, so there is one waterfall
shape to maintain rather than two. Not a shared package: that would tie
shmobster's release cadence to this repo's, and the owned surface is small.

The chain is a list of vendor rows, tried in order until one answers:

    {
      "waterfall": [
        {"name": "anthropic",  "model": "anthropic/<model>", "api_key_env": "ANTHROPIC_API_KEY"},
        {"name": "codex",      "model": "codex/chatgpt/<model>"},
        {"name": "openrouter", "model": "openrouter/<vendor>/<model>:free", "api_key_env": "OPENROUTER_API_KEY"}
      ],
      "budget_park_sec": 3600
    }

Row keys: `name` (unique; what logs, `model_used` and parking call the rung),
`model` (a LiteLLM model id), `api_key_env` (the variable holding the key),
optional `api_base` (OpenAI-compatible endpoints) and `timeout_sec` (per-request
ceiling for that rung; else the plugin's `llm_timeout`, default 120).
`budget_park_sec` defaults to 3600; 0 turns parking off.

Where the chain comes from (`build_client`): the plugin block's own `llm` key if
it has one, else `<workspaces-root>/_shared/llm.json`, else `LLMConfigError`
naming both. One shared file means one waterfall per deployment; the per-plugin
block exists for the agent that wants a cheap model for scoring and the good one
for the weekly post. The root and the workspace reach the plugin as `_root` and
`_workspace` in its block, injected by `agent run`.

Differences from shmobster's rows, both deliberate. A row names the variable
that holds its key (`api_key_env`), never the key: secrets live in the
environment in this repo, and a row carrying `api_key` is refused rather than
honoured. Every non-codex row must name one, because a row without a key would
let LiteLLM go looking for its own idea of the right variable, which is a
credential read nobody configured. A `codex/` row takes no key at all: it
authenticates from the codex CLI's own token file (see `framework/codex_llm.py`),
so a chain can run on the ChatGPT subscription with nothing in the environment.
A named variable that is unset fails the build, naming the variable, rather
than quietly shortening the chain.

The Router is built as `primary` plus `fb0..fbN` groups with
`fallbacks=[{"primary": [...]}]`, `num_retries=1`, `cooldown_time=60` and
`allowed_fails=0`, so a vendor that fails is cooled on its first failure instead
of being re-dialled on every call of the run.

Budget parking. LiteLLM cools only on status (429/401/408/404), and budget
exhaustion is neither: Anthropic answers a usage cap with 400, OpenRouter answers
no-credit with 402. Fallback still covers the call, but the dead vendor would be
re-dialled first on every run for as long as the cap lasts -- weeks, in the case
that prompted shmobster's version. So a failure whose status is 400/402/403 and
whose message names a spend problem (`_BUDGET_MARKERS`) parks that vendor: its
expiry goes into `<workspace>/state/llm-parked.json`, and the Router is rebuilt
without it until then. The expiry is the vendor's own "regain access on
YYYY-MM-DD" when it states one and the date is in the future, else
`budget_park_sec` from now. The file is pruned on read, which is what brings a
vendor back: no timer, the first call after the window simply stops seeing it.
A fully parked chain is tried in full anyway, since refusing to answer is worse
than one wasted call.

Parking hangs off LiteLLM's per-deployment failure callback, not off an `except`
around the call: when a fallback succeeds, the Router returns that answer and
the primary's exception never propagates, so an `except` would only see the case
parking cannot help. The same callback records one reason per vendor, which is
what `LLMError` reports when the whole chain fails and what the one stderr line
about a fallback says when it does not. Deployments carry a per-client id, so a
failure is attributed to exactly the vendor row that raised it, even with two
clients (scorer and digest) alive in one process.

Logging. LiteLLM's own loggers are capped at WARNING and verbose mode is off
before any Router exists, because at DEBUG they print raw request params,
api_key included. This module prints its own events to stderr, as the rest of
the framework does, and never prints keys, headers or request bodies. Failure
reasons quote the vendor's error message, first line only, with every
configured key value redacted, since `LLMError` can end up in a Slack channel.

`LLMClient.complete(system, user) -> str` and `model_used` keep their contract.
`LLMError` still means no rung produced an answer, now with per-vendor reasons;
an empty completion counts as that rung failing, as it did before.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sys
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import litellm

# Before anything else touches litellm: never let it log raw request params,
# which carry api_key. Verbose mode off AND all three loggers capped, because
# litellm's debug switch flips every one of them to DEBUG and it is
# "LiteLLM Router", not "LiteLLM", that services the Router used here.
litellm.set_verbose = False
litellm.suppress_debug_info = True
for _logger_name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

import openai  # noqa: E402  (litellm's own dependency; the base of its exceptions)
from litellm import Router  # noqa: E402
from litellm.integrations.custom_logger import CustomLogger  # noqa: E402
from litellm.types.router import RouterRateLimitError  # noqa: E402

from framework import codex_llm  # noqa: E402
from framework.config import CONFIG_FILENAME, STATE_DIRNAME, ConfigError  # noqa: E402

SHARED_DIRNAME = "_shared"
SHARED_FILENAME = "llm.json"
PARKED_FILENAME = "llm-parked.json"

_DEFAULT_BUDGET_PARK_SEC = 3600
_DEFAULT_TIMEOUT = 120
_CODEX_MODEL_PREFIX = f"{codex_llm.PROVIDER}/{codex_llm.MODEL_PREFIX}"

# Budget exhaustion, by status plus message shape. Status alone is too broad --
# a 400 is also a malformed request, and parking a vendor for an hour over a bad
# prompt would be worse than the problem.
_BUDGET_MARKERS = (
    "usage limit",
    "usage limits",
    "insufficient credit",
    "insufficient_quota",
    "credit balance",
    "exceeded your current quota",
    "billing",
    "payment required",
)

# Anthropic states when access returns: "You will regain access on 2026-09-01".
_REGAIN_DATE = re.compile(r"regain access on (\d{4})-(\d{2})-(\d{2})")

# litellm prefixes its messages with its own class path; the class is reported
# separately, so the prefix is noise in a reason.
_LITELLM_PREFIX = re.compile(r"^litellm\.[A-Za-z]+:\s*")

_REASON_CHARS = 160

# Live clients by deployment-id token, so the one global failure callback can
# hand a failure to the client whose Router raised it.
_CLIENTS: "weakref.WeakValueDictionary[str, LLMClient]" = weakref.WeakValueDictionary()


class LLMError(RuntimeError):
    """No rung of the waterfall produced an answer; the message has one reason per vendor."""


class LLMConfigError(ConfigError):
    """No usable waterfall: none configured, unreadable, or naming an unset credential.

    A `ConfigError`, so `agent run` reports it in one line and exits 2 like any
    other config problem.
    """


def _say(message: str) -> None:
    print(f"llm: {message}", file=sys.stderr)


@dataclass(frozen=True)
class _Vendor:
    name: str
    model: str
    # Resolved from `api_key_env` at build time. Kept out of repr so an
    # accidental print of a vendor cannot show it.
    api_key: str | None = field(repr=False)
    api_base: str | None
    timeout_sec: float | None


def is_budget_error(exc: BaseException) -> bool:
    """A 4xx that names a spend problem. Status alone is not enough: 400 is also
    'your request was malformed', and 402 is unambiguous but rare."""
    status = getattr(exc, "status_code", None)
    if status not in (400, 402, 403):
        retval = False
        return retval
    text = str(getattr(exc, "message", "") or str(exc)).lower()
    retval = any(marker in text for marker in _BUDGET_MARKERS)
    return retval


def _park_until(message: str, window: int) -> tuple[float, str]:
    """When the vendor says it will be back, honour that; otherwise use the
    configured window. A date we cannot parse falls back to the window rather
    than being trusted -- a mis-parse could park a vendor for a year."""
    match = _REGAIN_DATE.search(message or "")
    if match:
        try:
            when = datetime.datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                tzinfo=datetime.timezone.utc,
            ).timestamp()
            if when > time.time():
                retval = (when, f"until {match.group(0).split('on ')[1]}")
                return retval
        except ValueError:
            _say("unparseable regain date; using the default park window")
    retval = (time.time() + window, f"for {window}s")
    return retval


class _ChainWatch(CustomLogger):
    """LiteLLM's per-deployment failure callback.

    The only hook that sees a failure a fallback went on to cover, which is the
    parking case: the call still gets answered, so nothing raises, yet the
    exhausted vendor must not be dialled again."""

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _on_failure(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _on_failure(kwargs)


def _on_failure(kwargs: dict) -> None:
    exc = kwargs.get("exception")
    if exc is None:
        return
    metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    deployment_id = str((metadata.get("model_info") or {}).get("id") or "")
    token, sep, name = deployment_id.partition(":")
    client = _CLIENTS.get(token) if sep else None
    if client is None:
        return
    client._record_failure(name, exc)


_WATCH = _ChainWatch()


def _watch() -> None:
    """Register the failure callback once. `litellm.callbacks` is global, and a
    duplicate registration would record (and park) twice per failure."""
    if not any(isinstance(cb, _ChainWatch) for cb in litellm.callbacks):
        litellm.callbacks = list(litellm.callbacks) + [_WATCH]


class LLMClient:
    def __init__(
        self,
        vendors: list[_Vendor],
        parked_path: str | os.PathLike,
        budget_park_sec: int = _DEFAULT_BUDGET_PARK_SEC,
        timeout: int = _DEFAULT_TIMEOUT,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ):
        if not vendors:
            raise LLMConfigError("the waterfall is empty -- add at least one vendor")
        self.vendors = list(vendors)
        self.parked_path = Path(parked_path)
        self.budget_park_sec = budget_park_sec
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Which rung actually answered, for the caller to report.
        self.model_used: str | None = None
        self._token = uuid.uuid4().hex[:12]
        self._router: Router | None = None
        self._router_vendors: tuple[str, ...] | None = None
        # Vendor name -> reason, for the call in progress.
        self._failures: dict[str, str] = {}
        _CLIENTS[self._token] = self

    # -- parked-vendor state ------------------------------------------------

    def _read_parked(self) -> dict:
        if not self.parked_path.exists():
            retval: dict = {}
            return retval
        try:
            raw = json.loads(self.parked_path.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"{self.parked_path} could not be read ({exc}); delete it to clear parked vendors"
            ) from exc
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"{self.parked_path} is not a JSON object; delete it to clear parked vendors"
            )
        retval = raw
        return retval

    def _write_parked(self, parked: dict[str, float]) -> None:
        self.parked_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.parked_path.with_name(self.parked_path.name + ".tmp")
        tmp.write_text(json.dumps(parked, indent=2, sort_keys=True))
        os.replace(tmp, self.parked_path)

    def _parked(self) -> dict[str, float]:
        """Vendor -> expiry, with anything already expired dropped and the
        drop written back. Pruning on read is what brings a vendor back."""
        raw = self._read_parked()
        now = time.time()
        live = {
            str(name): float(until) for name, until in raw.items()
            if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now
        }
        if len(live) != len(raw):
            self._write_parked(live)
            for name in sorted(set(raw) - set(live)):
                _say(f"{name} is out of its park window; back in the chain")
        retval = live
        return retval

    def _park(self, name: str, exc: BaseException) -> None:
        if not self.budget_park_sec:
            return
        parked = self._parked()
        if name in parked:
            # Already parked; the callback fires once per attempt.
            return
        until, human = _park_until(str(getattr(exc, "message", "") or str(exc)), self.budget_park_sec)
        parked[name] = until
        self._write_parked(parked)
        # The vendor's prose is deliberately not repeated here; the reason
        # recorded for this call carries the redacted first line of it.
        _say(f"{name} is out of budget; parked {human}")
        self._router = None
        self._router_vendors = None

    # -- failures -----------------------------------------------------------

    def _redact(self, text: str) -> str:
        retval = text
        for vendor in self.vendors:
            if vendor.api_key:
                retval = retval.replace(vendor.api_key, "<redacted>")
        return retval

    def _reason(self, exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        lines = str(exc).strip().splitlines()
        first = _LITELLM_PREFIX.sub("", lines[0]) if lines else ""
        reason = type(exc).__name__
        if status:
            reason += f" {status}"
        if first:
            reason += f": {first[:_REASON_CHARS]}"
        retval = self._redact(reason)
        return retval

    def _record_failure(self, name: str, exc: BaseException) -> None:
        self._failures[name] = self._reason(exc)
        if is_budget_error(exc):
            self._park(name, exc)

    def _summary(self, live: list[_Vendor]) -> str:
        parts = [f"{v.name}: {self._failures.get(v.name, 'not tried')}" for v in live]
        live_names = {v.name for v in live}
        parked = sorted(name for name in self._parked() if name not in live_names)
        if parked:
            parts.append(f"parked: {', '.join(parked)}")
        retval = "; ".join(parts)
        return retval

    # -- router -------------------------------------------------------------

    def _live(self) -> list[_Vendor]:
        """The configured waterfall minus parked vendors -- but never empty."""
        parked = self._parked()
        live = [v for v in self.vendors if v.name not in parked]
        if not live:
            _say("every vendor is parked; trying the full chain anyway")
            live = list(self.vendors)
        retval = live
        return retval

    def _deployment(self, group: str, vendor: _Vendor) -> dict:
        # A codex row has no api_key at all. Sending `api_key: None` is not the
        # same as omitting it -- litellm treats the key as present-but-empty.
        params: dict[str, Any] = {
            "model": vendor.model,
            "timeout": vendor.timeout_sec or self.timeout,
        }
        if vendor.api_key:
            params["api_key"] = vendor.api_key
        if vendor.api_base:
            params["api_base"] = vendor.api_base
        retval = {
            "model_name": group,
            "litellm_params": params,
            "model_info": {"id": f"{self._token}:{vendor.name}"},
        }
        return retval

    def _build(self, live: list[_Vendor]) -> Router:
        _watch()
        # Registered before the Router is built: litellm resolves every
        # deployment's provider at construction, and must know `codex/` by then.
        codex_llm.register()
        model_list = [self._deployment("primary", live[0])]
        rest = live[1:]
        for i, vendor in enumerate(rest):
            model_list.append(self._deployment(f"fb{i}", vendor))
        fallbacks = [{"primary": [f"fb{i}" for i in range(len(rest))]}] if rest else []
        retval = Router(
            model_list=model_list,
            fallbacks=fallbacks,
            num_retries=1,
            cooldown_time=60,
            # Cool a failing deployment on its FIRST failure rather than after
            # the default threshold: a run is short, and by the time a
            # threshold is reached every fail in it was a wasted round-trip.
            allowed_fails=0,
        )
        return retval

    def _ensure(self) -> tuple[Router, list[_Vendor]]:
        """Build the Router, or rebuild it when the set of usable vendors
        changed -- a park added one, or a park expired and gave one back."""
        live = self._live()
        names = tuple(v.name for v in live)
        if self._router is None or names != self._router_vendors:
            if self._router_vendors is not None:
                _say(f"rebuilding, chain is now {' -> '.join(names)}")
            self._router = self._build(live)
            self._router_vendors = names
        retval = (self._router, live)
        return retval

    def _answered_by(self, resp: Any) -> _Vendor | None:
        hidden = getattr(resp, "_hidden_params", None) or {}
        token, _, name = str(hidden.get("model_id") or "").partition(":")
        retval = None
        if token == self._token:
            retval = next((v for v in self.vendors if v.name == name), None)
        return retval

    def complete(self, system: str, user: str) -> str:
        """One system+user turn through the waterfall; the first answer wins.

        No budget handling here on purpose: when a fallback covers a failure
        nothing is raised, so parking lives in the failure callback. An
        exception reaching this point means every rung failed (or, later in a
        run, that every rung is still cooling from an earlier failure)."""
        router, live = self._ensure()
        self._failures = {}
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            resp = router.completion(
                model="primary",
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                # Rungs differ in which sampling params they accept (current
                # Claude models refuse any temperature but 1, and litellm raises
                # before sending). Dropping what a rung cannot take keeps it in
                # the chain instead of failing it on every call.
                drop_params=True,
            )
        except (openai.OpenAIError, RouterRateLimitError) as exc:
            raise LLMError("all models failed — " + self._summary(live)) from exc

        vendor = self._answered_by(resp)
        label = f"{vendor.name} ({vendor.model})" if vendor else str(resp.model)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            self._failures[vendor.name if vendor else label] = "empty completion"
            raise LLMError("all models failed — " + self._summary(live))

        if self._failures:
            reasons = "; ".join(f"{name}: {reason}" for name, reason in self._failures.items())
            _say(f"answered by {label} after {reasons}")
        self.model_used = label
        retval = content
        return retval


def _parse_row(row: Any, where: str) -> _Vendor:
    if not isinstance(row, dict):
        raise LLMConfigError(f"{where}: each waterfall row must be an object")
    name = row.get("name")
    model = row.get("model")
    if not isinstance(name, str) or not name:
        raise LLMConfigError(f"{where}: 'name' is required")
    where = f"{where} ({name})"
    if not isinstance(model, str) or not model:
        raise LLMConfigError(f"{where}: 'model' is required")
    if "api_key" in row:
        raise LLMConfigError(
            f"{where}: 'api_key' is not accepted; put the key in the environment "
            f"and name the variable with 'api_key_env'"
        )

    key_env = row.get("api_key_env")
    api_key = None
    if model.startswith(f"{codex_llm.PROVIDER}/"):
        if not model.startswith(_CODEX_MODEL_PREFIX):
            # See codex_llm.MODEL_PREFIX: without the segment litellm routes
            # the bare model id to OpenAI and bills a platform key instead.
            raise LLMConfigError(
                f"{where}: codex rows must be '{_CODEX_MODEL_PREFIX}<model>' (got {model!r})"
            )
        if key_env is not None:
            raise LLMConfigError(
                f"{where}: codex rows take no 'api_key_env'; they authenticate "
                f"from the codex CLI's token file"
            )
    else:
        if not isinstance(key_env, str) or not key_env:
            raise LLMConfigError(
                f"{where}: 'api_key_env' is required (only '{_CODEX_MODEL_PREFIX}' rows run without a key)"
            )
        api_key = os.environ.get(key_env)
        if not api_key:
            raise LLMConfigError(f"{where}: ${key_env} is not set")

    api_base = row.get("api_base")
    if api_base is not None and (not isinstance(api_base, str) or not api_base):
        raise LLMConfigError(f"{where}: 'api_base' must be a non-empty string")
    timeout_sec = row.get("timeout_sec")
    if timeout_sec is not None and (
        isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or timeout_sec <= 0
    ):
        raise LLMConfigError(f"{where}: 'timeout_sec' must be a positive number")

    retval = _Vendor(
        name=name,
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout_sec=float(timeout_sec) if timeout_sec is not None else None,
    )
    return retval


def _parse_chain(raw: Any, origin: str) -> tuple[list[_Vendor], int]:
    if not isinstance(raw, dict):
        raise LLMConfigError(f"{origin}: must be an object with a 'waterfall' list")
    rows = raw.get("waterfall")
    if not isinstance(rows, list) or not rows:
        raise LLMConfigError(f"{origin}: 'waterfall' must be a non-empty list of vendor rows")
    park_sec = raw.get("budget_park_sec", _DEFAULT_BUDGET_PARK_SEC)
    if isinstance(park_sec, bool) or not isinstance(park_sec, int) or park_sec < 0:
        raise LLMConfigError(
            f"{origin}: 'budget_park_sec' must be a non-negative integer (got {park_sec!r})"
        )
    vendors: list[_Vendor] = []
    for i, row in enumerate(rows):
        vendor = _parse_row(row, f"{origin}: waterfall[{i}]")
        if any(v.name == vendor.name for v in vendors):
            raise LLMConfigError(f"{origin}: vendor name {vendor.name!r} appears twice")
        vendors.append(vendor)
    retval = (vendors, park_sec)
    return retval


def _load_chain(cfg: dict, what: str, config_label: str) -> tuple[Any, str]:
    """The plugin's own `llm` block, else the deployment's shared file."""
    block = cfg.get("llm")
    if block is not None:
        retval = (block, f"{config_label}: {what} 'llm' block")
        return retval

    root = cfg.get("_root")
    if root:
        shared = Path(root) / SHARED_DIRNAME / SHARED_FILENAME
        if shared.is_file():
            try:
                raw = json.loads(shared.read_text())
            except (OSError, ValueError) as exc:
                raise LLMConfigError(f"{shared} could not be read as JSON: {exc}") from exc
            retval = (raw, str(shared))
            return retval
        shared_label = str(shared)
    else:
        shared_label = (
            f"<workspaces-root>/{SHARED_DIRNAME}/{SHARED_FILENAME} "
            f"(no workspaces root was passed as '_root')"
        )
    raise LLMConfigError(
        f"{what} has no LLM waterfall: add an 'llm' block to {config_label}, "
        f"or create {shared_label}"
    )


def build_client(
    cfg: dict,
    default_max_tokens: int = 2000,
    default_temperature: float = 0.3,
    what: str = "llm",
) -> LLMClient:
    """Build an `LLMClient` from a plugin's config block.

    Every LLM-backed plugin resolves its chain the same way, and this is that
    way, written once (see the module docstring for the order and the row
    shape). `cfg` also carries `_workspace` and `_root`, injected by
    `agent run`; the workspace holds the parked-vendor state, so a client
    cannot be built without it.

    Plugin keys read here besides `llm`: `llm_timeout` (seconds per request,
    default 120, overridden per row by `timeout_sec`), `max_tokens` and
    `temperature` (defaults supplied by the calling plugin).

    `what` names the calling plugin in error messages.
    """
    if "model" in cfg:
        raise LLMConfigError(
            f"{what}: the 'model' block was replaced by 'llm' "
            f"(a waterfall of vendor rows; see docs/llm.example.json)"
        )
    workspace = cfg.get("_workspace")
    if not workspace:
        raise LLMConfigError(
            f"{what} needs the workspace path for its parked-vendor state; the CLI "
            f"injects it as '_workspace' (it cannot be built outside `agent run`)"
        )
    config_label = str(Path(workspace) / CONFIG_FILENAME)
    raw, origin = _load_chain(cfg, what, config_label)
    vendors, park_sec = _parse_chain(raw, origin)

    retval = LLMClient(
        vendors=vendors,
        parked_path=Path(workspace) / STATE_DIRNAME / PARKED_FILENAME,
        budget_park_sec=park_sec,
        timeout=int(cfg.get("llm_timeout", _DEFAULT_TIMEOUT)),
        max_tokens=int(cfg.get("max_tokens", default_max_tokens)),
        temperature=float(cfg.get("temperature", default_temperature)),
    )
    return retval
