"""LLM access with a model waterfall.

Mirrors how the OpenClaw agents are configured: a primary model with an ordered
list of fallbacks, tried in turn until one answers. There the chain is a
subscription-backed primary (`openai-codex/gpt-5.5`) followed by free OpenRouter
models; the same shape applies here.

Two transports, chosen by the prefix on each chain entry:

  - `codex/<model>` runs the local `codex` CLI, which authenticates with the
    **ChatGPT subscription** rather than an API key. This is what
    voitta-ai/shmobster#35 asked for — using the subscription as part of the
    waterfall, the way OpenClaw already can. Output is read from
    `--output-last-message` rather than scraped from stdout, which also carries
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

import os
import shutil
import subprocess
import tempfile

import requests

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(RuntimeError):
    """Every model in the waterfall failed."""


class _ModelUnavailable(RuntimeError):
    """This model could not answer; try the next one in the chain."""


class LLMClient:
    def __init__(
        self,
        api_key: str,
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

            payload = resp.json()
            choices = payload.get("choices") or []
            if not choices:
                failures.append(f"{entry}: no choices in response")
                continue
            content = (choices[0].get("message") or {}).get("content") or ""
            if not content.strip():
                failures.append(f"{entry}: empty completion")
                continue

            self.model_used = entry
            retval = content.strip()
            return retval

        raise LLMError("all models failed — " + "; ".join(failures))
