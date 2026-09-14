"""LLM access with a model waterfall.

Mirrors how the OpenClaw agents are configured: a primary model with an ordered
list of fallbacks, tried in turn until one answers. There the chain is a
subscription-backed primary (`openai-codex/gpt-5.5`) followed by free OpenRouter
models; the same shape applies here.

Two transports, chosen by the prefix on each chain entry:

  - `codex/<model>` runs the local `codex` CLI, which authenticates with the
    **ChatGPT subscription** rather than an API key, putting the subscription
    into the waterfall the way OpenClaw already can. Output is read from the
    `-o` last-message file rather than scraped from stdout, which also carries
    hook chatter and token counts.
  - `openrouter/<model>` calls OpenRouter's OpenAI-compatible endpoint with an
    API key. One key covers every model behind it.

Putting the subscription first means routine digests cost nothing extra, and the
metered fallbacks only engage when it is unavailable.

A fallback fires on transport failure, rate limiting, or a 5xx — the cases where
a different model would plausibly succeed. It does **not** fire on a 4xx like a
malformed request or a bad key, because retrying identical bad input against
five models just produces five identical failures more slowly.

If every model in the chain fails, `complete()` raises with the per-model
reasons. A digest that silently produced nothing would be indistinguishable from
a quiet week, which is the failure mode this codebase keeps running into.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENCLAW_CONFIG = "~/.openclaw/openclaw.json"

# OpenClaw names the subscription transport `openai-codex/`; this module keys
# the transport off `codex/`. Same thing, different label, so normalise on the
# way in rather than teaching `complete()` two spellings.
_TRANSPORT_ALIASES = {"openai-codex": "codex"}


class LLMError(RuntimeError):
    """Every model in the waterfall failed."""


class LLMConfigError(RuntimeError):
    """A model chain was configured, but could not be read."""


class _ModelUnavailable(RuntimeError):
    """This model could not answer; try the next one in the chain."""


def _normalise_entry(entry: str) -> str:
    """Map an OpenClaw transport prefix onto this module's."""
    provider, sep, model = str(entry).partition("/")
    if sep and provider in _TRANSPORT_ALIASES:
        retval = f"{_TRANSPORT_ALIASES[provider]}/{model}"
        return retval
    retval = str(entry)
    return retval


def load_openclaw_chain(path: str | os.PathLike | None = None) -> tuple[str, list[str]] | None:
    """Read `agents.defaults.model` from OpenClaw's config, as (primary, fallbacks).

    The point of reading OpenClaw's config rather than duplicating a chain in
    every agent config is that there is then exactly one waterfall to maintain:
    change the fallback list once and every agent follows.

    Missing config is not an error — it means "this machine has no OpenClaw, use
    the agent's own model config", so this returns None and the caller decides.
    Config that *exists but cannot be used* is always an error, never a silent
    fallback: a typo'd chain that quietly degraded to some default would be
    exactly the class of silent failure this repo keeps getting bitten by.
    """
    config_path = Path(path).expanduser() if path is not None else Path(_OPENCLAW_CONFIG).expanduser()
    if not config_path.exists():
        return None

    try:
        raw = json.loads(config_path.read_text())
    except (OSError, ValueError) as exc:
        raise LLMConfigError(f"{config_path} exists but could not be read as JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise LLMConfigError(f"{config_path} is not a JSON object")

    model_cfg = (raw.get("agents") or {}).get("defaults", {}).get("model")
    if model_cfg is None:
        return None
    if not isinstance(model_cfg, dict):
        raise LLMConfigError(f"{config_path}: agents.defaults.model is not an object")

    primary = model_cfg.get("primary")
    if not primary or not isinstance(primary, str):
        raise LLMConfigError(f"{config_path}: agents.defaults.model.primary is missing or not a string")

    raw_fallbacks = model_cfg.get("fallbacks") or []
    if not isinstance(raw_fallbacks, list) or not all(isinstance(f, str) for f in raw_fallbacks):
        raise LLMConfigError(f"{config_path}: agents.defaults.model.fallbacks must be a list of strings")

    retval = (_normalise_entry(primary), [_normalise_entry(f) for f in raw_fallbacks])
    return retval


class LLMClient:
    def __init__(
        self,
        api_key: str | None,
        primary: str,
        fallbacks: list[str] | None = None,
        timeout: int = 120,
        max_tokens: int = 2000,
        temperature: float = 0.3,
        base_url: str = _OPENROUTER_URL,
    ):
        self.api_key = api_key
        self.chain = [primary] + list(fallbacks or [])
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.base_url = base_url
        # Which model actually answered, for the caller to report.
        self.model_used: str | None = None

    def _complete_codex(self, model: str, system: str, user: str) -> str:
        """Run one turn through the codex CLI, on the ChatGPT subscription."""
        if not shutil.which("codex"):
            raise _ModelUnavailable("codex CLI not on PATH")
        try:
            retval = self._run_codex(model, system, user)
        except OSError as exc:
            # A local temp-dir or file-read problem says nothing about whether
            # the next model can answer, so fall through instead of aborting
            # the whole waterfall.
            raise _ModelUnavailable(f"codex local I/O failed: {exc}") from exc
        return retval

    def _run_codex(self, model: str, system: str, user: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "last-message.txt")
            args = ["codex", "exec", "--skip-git-repo-check", "-o", out]
            if model:
                args += ["-m", model]
            proc = subprocess.run(
                args,
                input=f"{system}\n\n{user}",
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=tmp,
            )
            if proc.returncode != 0:
                raise _ModelUnavailable(f"codex exit {proc.returncode}: {proc.stderr.strip()[:160]}")
            if not os.path.exists(out):
                raise _ModelUnavailable("codex wrote no last-message file")
            with open(out) as fh:
                retval = fh.read().strip()
        if not retval:
            raise _ModelUnavailable("codex returned an empty message")
        return retval

    def complete(self, system: str, user: str) -> str:
        failures: list[str] = []
        for entry in self.chain:
            provider, _, model = entry.partition("/")
            if provider == "codex":
                try:
                    result = self._complete_codex(model, system, user)
                except (_ModelUnavailable, subprocess.SubprocessError) as exc:
                    failures.append(f"{entry}: {exc}")
                    continue
                self.model_used = entry
                return result
            # Anything else is an OpenRouter model id; strip a leading
            # "openrouter/" if present, since that names the transport not the model.
            model = model if provider == "openrouter" else entry
            if not self.api_key:
                # Only OpenRouter entries need a key. A codex-first chain must
                # stay runnable on the subscription alone, which is the main
                # reason that transport exists -- so this is a per-model skip,
                # not a hard precondition.
                failures.append(f"{entry}: no OpenRouter API key configured")
                continue
            try:
                resp = requests.post(
                    self.base_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "max_tokens": self.max_tokens,
                        "temperature": self.temperature,
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                failures.append(f"{entry}: {type(exc).__name__}")
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                failures.append(f"{entry}: HTTP {resp.status_code}")
                continue
            if not resp.ok:
                # A 4xx is our fault, not the model's — the next model would
                # reject the same request identically.
                raise LLMError(f"{entry}: HTTP {resp.status_code} {resp.text[:200]}")

            try:
                payload = resp.json()
                choices = payload.get("choices") or []
                content = (choices[0].get("message") or {}).get("content") or "" if choices else ""
            except (ValueError, AttributeError, TypeError, IndexError) as exc:
                # A 200 carrying HTML, an empty body, or an unexpected shape is
                # this model failing, not the request being wrong. Record it and
                # try the next one rather than aborting the chain.
                failures.append(f"{entry}: unreadable response ({type(exc).__name__})")
                continue
            if not choices:
                failures.append(f"{entry}: no choices in response")
                continue
            if not content.strip():
                failures.append(f"{entry}: empty completion")
                continue

            self.model_used = entry
            retval = content.strip()
            return retval

        raise LLMError("all models failed — " + "; ".join(failures))


def build_client(
    cfg: dict,
    default_max_tokens: int = 2000,
    default_temperature: float = 0.3,
    what: str = "llm",
) -> LLMClient:
    """Build an `LLMClient` from a plugin's config block.

    Every LLM-backed plugin resolves its chain the same way, and this is that
    way, written once: an explicit `model.primary` / `model.fallbacks` in the
    agent's own config wins; absent one, OpenClaw's shared chain is used so
    there is a single waterfall to maintain rather than one copy per agent.

    Only OpenRouter entries need a key. A chain whose entries are all `codex/...`
    runs on the ChatGPT subscription with no API key at all, which is the main
    reason that transport exists -- so requiring the key up front would defeat
    it. A missing key becomes a per-model failure at the point an OpenRouter
    entry is actually attempted (see `LLMClient.complete`).

    `what` names the calling plugin in error messages.
    """
    model_cfg = cfg.get("model") or {}
    primary = model_cfg.get("primary")
    fallbacks = model_cfg.get("fallbacks")
    if not primary:
        shared = load_openclaw_chain()
        if shared is None:
            raise RuntimeError(
                f"{what} needs 'model.primary', or an OpenClaw config at "
                f"{_OPENCLAW_CONFIG} providing agents.defaults.model"
            )
        primary, fallbacks = shared

    key_env = cfg.get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(key_env)

    retval = LLMClient(
        api_key=api_key,
        primary=primary,
        fallbacks=fallbacks,
        timeout=int(cfg.get("llm_timeout", 120)),
        max_tokens=int(cfg.get("max_tokens", default_max_tokens)),
        temperature=float(cfg.get("temperature", default_temperature)),
    )
    return retval
