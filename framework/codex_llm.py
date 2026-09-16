"""Codex subscription as a waterfall vendor (issue #44).

Ported from shmobster's `codex_llm.py`, verbatim where the two codebases allow,
so the copies can still be diffed against each other.

Every other rung in the waterfall is an API-key HTTP row, so LiteLLM can dial it
from config alone. A codex *subscription* is not: it authenticates with the
ChatGPT OAuth tokens the codex CLI keeps in `~/.codex/auth.json`, and it speaks
the Responses API rather than chat/completions. This module is the bridge that
makes it look like any other rung.

Three decisions, all deliberate:

**In-process, not a separate server.** Registered as a `litellm.CustomLLM` via
`litellm.custom_provider_map`, so the waterfall row is
`{"name": "codex", "model": "codex/chatgpt/<model>"}` -- no `api_key_env`, no
`api_base`, and no second process under the scheduler.

**We read codex's tokens; we do NOT run codex.** The framework used to drive the
binary (`codex exec`), which kept the token out of our hands but handed us an
*agent*: codex brings its own tool set, sandbox and approval policy, prints hook
chatter around its answer, and costs a process start of several seconds per
call. A waterfall rung needs a plain model call and nothing else, and reading
the token gives exactly that.

**We do not refresh the token -- we warn.** `auth.json` is re-read on every
call, so any rotation the codex CLI performs is picked up for free; the access
token is a ~10-day JWT and the CLI rotates it whenever it runs. Owning the
refresh would mean writing back to `auth.json` and racing the CLI over a refresh
token that may well be single-use -- a bug there breaks the operator's actual
codex login, which is far worse than one dead rung. Instead the token's own
`exp` claim is read on every call and an expiry inside `_WARN_BEFORE_SEC` is
printed to stderr (at most hourly, so a busy run does not turn the warning into
spam). An already-dead token surfaces as a 401: LiteLLM cools the deployment,
the chain falls through, and the fix is `codex login` (or any codex CLI use).

Contract, established against the live endpoint rather than from docs:
  - POST https://chatgpt.com/backend-api/codex/responses
  - `Authorization: Bearer <tokens.access_token>` + `ChatGPT-Account-Id`
  - `stream: true` is REQUIRED -- a non-streaming request is refused with
    400 "Stream must be set to true", so the SSE body is read whole and parsed
    here even though nothing downstream streams.
  - `store: false`, and the model must be one the ChatGPT plan allows.
  - function tools come back as `function_call` items with call_id/name/
    arguments, which map 1:1 onto OpenAI tool_calls. `LLMClient` sends no
    tools today; the mapping is kept so this file stays a straight port.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid

import httpx
import litellm
from litellm import CustomLLM
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Function,
    Message,
    Usage,
)
from litellm.utils import custom_llm_setup

PROVIDER = "codex"

_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"

# The originator identifies the *client the credentials belong to*. These are
# codex's tokens, so this is codex's originator -- not this framework's.
_ORIGINATOR = "codex_cli_rs"

_DEFAULT_TIMEOUT = 300.0

# Waterfall rows say `codex/chatgpt/<model>`, and the `chatgpt/` segment is
# load-bearing rather than decoration. litellm dispatches on the model name
# BEFORE it dispatches on the custom provider: `model in
# open_ai_chat_completion_models` is an earlier branch than the custom-provider
# one, so a row of `codex/gpt-5.5` leaves litellm holding the bare `gpt-5.5`,
# recognising it as an OpenAI model, and quietly billing a platform API key
# instead of ever reaching this handler. A segment that is not a known OpenAI
# model id is what keeps the dispatch honest; it is stripped back off here.
MODEL_PREFIX = "chatgpt/"

# Warn this far ahead of the access token's own expiry. A day is enough notice
# to run `codex login` before the rung goes dead, and short enough that the
# warning still means something when it appears.
_WARN_BEFORE_SEC = 24 * 3600
_WARN_EVERY_SEC = 3600
_last_warned = 0.0


def _auth_path():
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    retval = os.path.join(home, "auth.json")
    return retval


def _auth(model):
    """The live access token and account id, re-read every call so a rotation by
    the codex CLI is picked up without a restart. Never logged, never returned to
    a caller that might."""
    path = _auth_path()
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise litellm.AuthenticationError(
            f"codex: cannot read {path} -- run `codex login`",
            llm_provider=PROVIDER, model=model)
    tokens = data.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise litellm.AuthenticationError(
            "codex: no ChatGPT access token in auth.json -- run `codex login`",
            llm_provider=PROVIDER, model=model)
    _warn_if_expiring(token)
    retval = (token, tokens.get("account_id") or "")
    return retval


def _expiry(token):
    """The `exp` claim, read straight off the JWT payload. Unsigned on purpose:
    this is not a trust decision -- the server verifies the token -- it is only
    "how long before an operator has to act". A token we cannot parse gets no
    warning rather than a wrong one."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        retval = float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        retval = None
    return retval


def _warn_if_expiring(token):
    """Tell the operator to rotate before the rung dies, not after. Rate-limited
    because this runs on every single call, and the token is never printed --
    only how long is left on it."""
    global _last_warned
    exp = _expiry(token)
    now = time.time()
    if exp is None or exp - now > _WARN_BEFORE_SEC:
        return
    if now - _last_warned < _WARN_EVERY_SEC:
        return
    _last_warned = now
    left = exp - now
    if left <= 0:
        print("llm: codex: the ChatGPT token has expired; run `codex login` to rotate it",
              file=sys.stderr)
    else:
        # Rounded up: "0h" on a token with 50 minutes left reads as "already
        # dead" when the operator still has time to act.
        print(
            "llm: codex: the ChatGPT token expires in ~%dh; run `codex login` to rotate it "
            "(this framework does not refresh it -- the codex CLI owns that file)"
            % max(1, -(-int(left) // 3600)),
            file=sys.stderr)


def _headers(token, account_id):
    retval = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "originator": _ORIGINATOR,
        "User-Agent": _ORIGINATOR,
        "session_id": str(uuid.uuid4()),
    }
    if account_id:
        retval["ChatGPT-Account-Id"] = account_id
    return retval


def _text_of(content):
    """Flatten OpenAI chat content into plain text. A content *list* is the
    multimodal shape: the text parts are kept and image parts are dropped rather
    than translated, since an image block the Responses API rejects would fail
    the call outright."""
    if content is None:
        retval = ""
    elif isinstance(content, str):
        retval = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, dict) and item.get("type") == "image_url":
                parts.append("[image omitted: codex fallback is text-only]")
        retval = "\n".join(p for p in parts if p)
    else:
        retval = str(content)
    return retval


def _to_input(messages):
    """OpenAI chat messages -> (instructions, Responses input items).

    System/developer turns become `instructions` because the Responses API has no
    system role. The rest map one-for-one, including the tool round-trip:
    an assistant `tool_calls` entry becomes a `function_call` item and the
    matching `role: tool` reply becomes `function_call_output`, keyed by the same
    call_id -- without that pairing the next turn is rejected."""
    instructions = []
    items = []
    for msg in messages:
        role = msg.get("role")
        if role in ("system", "developer"):
            instructions.append(_text_of(msg.get("content")))
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or "",
                "output": _text_of(msg.get("content")),
            })
        elif role == "assistant":
            text = _text_of(msg.get("content"))
            if text:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
        else:
            items.append({"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": _text_of(msg.get("content"))}]})
    retval = ("\n\n".join(i for i in instructions if i), items)
    return retval


def _to_tools(tools):
    """Chat tool schemas -> Responses tool schemas: same JSON Schema, but flat
    (name/description/parameters at the top level) instead of nested under
    `function`."""
    if not tools:
        retval = None
        return retval
    out = []
    for tool in tools:
        fn = tool.get("function") or {}
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            "strict": False,
        })
    retval = out
    return retval


def _parse(body):
    """Pull the assistant turn out of the SSE body.

    Only `response.output_item.done` and `response.completed` are read: the delta
    events are the same content arriving in pieces, and nothing downstream
    streams. Reasoning items are ignored -- they carry encrypted state for a
    stored conversation, and this bridge sends the whole history every call."""
    content_parts = []
    tool_calls = []
    usage = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                tool_calls.append(ChatCompletionMessageToolCall(
                    id=item.get("call_id") or item.get("id") or "",
                    type="function",
                    function=Function(name=item.get("name") or "",
                                      arguments=item.get("arguments") or "{}"),
                ))
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        content_parts.append(part.get("text") or "")
        elif kind == "response.completed":
            usage = (event.get("response") or {}).get("usage")
    retval = ("".join(content_parts), tool_calls, usage)
    return retval


def _raise_for(status, body, model):
    """Map codex's HTTP status onto the litellm exception the Router reacts to:
    401/403 and 429 are what it cools a deployment on, so getting these right is
    what makes a dead or throttled codex fall through instead of being re-dialled
    every call."""
    text = (body or "")[:300]
    if status in (401, 403):
        raise litellm.AuthenticationError(
            f"codex: ChatGPT token rejected ({status}) -- run `codex login`",
            llm_provider=PROVIDER, model=model)
    if status == 429:
        raise litellm.RateLimitError(
            f"codex: {text}", llm_provider=PROVIDER, model=model)
    if status == 400:
        raise litellm.BadRequestError(
            f"codex: {text}", model=model, llm_provider=PROVIDER)
    raise litellm.APIError(
        status_code=status, message=f"codex: {text}",
        llm_provider=PROVIDER, model=model)


class CodexLLM(CustomLLM):
    def completion(self, model, messages, api_base, custom_prompt_dict,
                   model_response, print_verbose, encoding, api_key, logging_obj,
                   optional_params, acompletion=None, litellm_params=None,
                   logger_fn=None, headers=None, timeout=None, client=None):
        token, account_id = _auth(model)
        wire_model = model[len(MODEL_PREFIX):] if model.startswith(MODEL_PREFIX) else model
        instructions, items = _to_input(messages)
        body = {
            "model": wire_model,
            "instructions": instructions,
            "input": items,
            # Both are required, not preferences: the endpoint refuses a
            # non-streaming request outright, and `store` is what keeps the
            # conversation out of OpenAI-side retention.
            "stream": True,
            "store": False,
        }
        tools = _to_tools(optional_params.get("tools") if optional_params else None)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = optional_params.get("tool_choice") or "auto"

        secs = timeout if isinstance(timeout, (int, float)) else _DEFAULT_TIMEOUT
        try:
            resp = httpx.post(_ENDPOINT, json=body,
                              headers=_headers(token, account_id), timeout=secs)
        except httpx.HTTPError as exc:
            raise litellm.APIConnectionError(
                message=f"codex: {type(exc).__name__}",
                llm_provider=PROVIDER, model=model)
        if resp.status_code != 200:
            _raise_for(resp.status_code, resp.text, model)

        content, tool_calls, usage = _parse(resp.text)
        choice = model_response.choices[0]
        choice.message = Message(
            role="assistant",
            content=content or None,
            tool_calls=tool_calls or None,
        )
        choice.finish_reason = "tool_calls" if tool_calls else "stop"
        model_response.model = f"{PROVIDER}/{model}"
        if usage:
            model_response.usage = Usage(
                prompt_tokens=usage.get("input_tokens") or 0,
                completion_tokens=usage.get("output_tokens") or 0,
                total_tokens=usage.get("total_tokens") or 0,
            )
        retval = model_response
        return retval


_HANDLER = CodexLLM()


def register():
    """Teach litellm the `codex/` prefix, once. `custom_provider_map` is global
    and a duplicate entry would shadow itself on every rebuild of the Router.

    `custom_llm_setup()` is the second half and is not optional: the map alone
    only tells litellm how to *call* the provider, while the provider *list* is
    what lets it recognise the prefix at all. Without it the Router rejects the
    row at construction time with "LLM Provider NOT provided", because it
    resolves every deployment's provider before any completion is attempted."""
    if not any(e.get("provider") == PROVIDER for e in litellm.custom_provider_map):
        litellm.custom_provider_map = list(litellm.custom_provider_map) + [
            {"provider": PROVIDER, "custom_handler": _HANDLER}
        ]
    custom_llm_setup()
