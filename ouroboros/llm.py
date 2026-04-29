"""Ouroboros — LLM client (Claude Code subscription edition).

Patched 2026-04-29 for fork `data_to_inside_brain` (variant B):
the only LLM communication path is now `claude-agent-sdk` against a
locally-installed `claude` CLI, authenticated by the user's Claude Code
subscription. No API keys, no OpenRouter, no per-call cost.

Public contract preserved (so the rest of brain doesn't change):
- ``LLMClient.chat(messages, model, tools, reasoning_effort, max_tokens, tool_choice) → (msg, usage)``
- ``LLMClient.vision_query(prompt, images, model, max_tokens, reasoning_effort) → (text, usage)``
- ``LLMClient.default_model() → str``
- ``LLMClient.available_models() → list[str]``
- ``add_usage(total, usage)`` — accumulate
- ``DEFAULT_LIGHT_MODEL`` constant (used by ``ouroboros.context``)
- ``fetch_openrouter_pricing()`` — kept for API compat, returns {} (vacuous on subscription)
- ``normalize_reasoning_effort()``, ``reasoning_rank()`` — kept verbatim

Limitations vs the original OpenRouter implementation:
- Single provider (Anthropic). Non-Anthropic model IDs (``google/...``,
  ``openai/...``) silently fall back to the configured Claude default.
- ``{"type": "web_search"}`` tool entries are replaced by Claude's built-in
  ``WebSearch`` (added to ``allowed_tools``). Custom function tools work
  normally via SDK MCP servers.
- No explicit prompt caching control — ``claude-agent-sdk`` manages cache
  internally. ``cached_tokens`` is surfaced when the SDK reports it.
- ``cost`` field is always 0.0 (subscription has no per-call cost).
- ``reasoning_effort`` is mapped: none/minimal/low → low, medium → medium,
  high → high, xhigh → max.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

log = logging.getLogger(__name__)

# Lightweight default — used by context.py for compaction summaries.
DEFAULT_LIGHT_MODEL = os.environ.get("OUROBOROS_MODEL_LIGHT", "claude-haiku-4-5")

# Default if OUROBOROS_MODEL is not set.
_DEFAULT_CLAUDE = "claude-opus-4-7"


# --------------------------------------------------------------------------
# Public utilities — kept verbatim from original (other modules import them).
# --------------------------------------------------------------------------


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """Vacuous on Claude subscription — no per-call cost. Kept for API compat."""
    return {}


# --------------------------------------------------------------------------
# Mapping helpers — OpenRouter conventions → Claude SDK conventions.
# --------------------------------------------------------------------------


_EFFORT_MAP = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
}


def _map_effort(effort: str) -> str:
    """Map ouroboros effort literal to claude-agent-sdk effort literal."""
    return _EFFORT_MAP.get(normalize_reasoning_effort(effort), "medium")


def _map_model(openrouter_id: str) -> str:
    """Map OpenRouter-style model id to Claude CLI model name.

    Anthropic models pass through (with the ``anthropic/`` prefix stripped and
    dots replaced by hyphens). Non-Anthropic models (``google/...``,
    ``openai/...``, ``meta-llama/...``) fall back to the configured Claude
    default — we can only reach Claude through the subscription.
    """
    if not openrouter_id:
        return os.environ.get("OUROBOROS_MODEL", _DEFAULT_CLAUDE)
    raw = str(openrouter_id).strip()
    if raw.startswith("anthropic/"):
        return raw.replace("anthropic/", "").replace(".", "-")
    if raw.startswith(("openai/", "google/", "meta-llama/", "x-ai/", "qwen/")):
        log.warning(
            "llm: non-Anthropic model %r requested — falling back to Claude (subscription path).",
            raw,
        )
        return os.environ.get("OUROBOROS_MODEL", _DEFAULT_CLAUDE)
    # Probably already a Claude name like 'claude-opus-4-7'.
    return raw


# --------------------------------------------------------------------------
# Tool capture machinery — OpenAI tools → SDK MCP server with capturing
# handlers. Each chat() call gets a fresh capture_id to isolate concurrent
# calls. Handlers acknowledge the call to keep Claude moving but the real
# tool execution stays in the caller's loop (loop.py / agent.py).
# --------------------------------------------------------------------------


_CAPTURE: Dict[str, List[Dict[str, Any]]] = {}
_CAPTURE_LOCK = threading.Lock()


def _make_capture_tool(spec: Dict[str, Any], capture_id: str):
    func = spec.get("function") or {}
    name = func.get("name") or "unnamed"
    description = func.get("description") or name
    raw_params = func.get("parameters") or {"type": "object", "properties": {}}
    # SDK input_schema accepts either a Python dict (the JSON schema) or a
    # mapping of field-name → type. Pass the raw schema through.
    schema = raw_params if isinstance(raw_params, dict) else {"type": "object", "properties": {}}

    @tool(name=name, description=description, input_schema=schema)
    async def _captured(args: Dict[str, Any]) -> Dict[str, Any]:
        with _CAPTURE_LOCK:
            _CAPTURE.setdefault(capture_id, []).append(
                {"name": name, "arguments": args}
            )
        return {"content": [{"type": "text", "text": "tool call captured"}]}

    return _captured


def _build_capture_servers(
    tools: Optional[List[Dict[str, Any]]],
    capture_id: str,
) -> Tuple[Dict[str, Any], List[str], bool]:
    """Return (mcp_servers, allowed_tools, web_search_requested)."""
    servers: Dict[str, Any] = {}
    allowed: List[str] = []
    web_search = False
    if not tools:
        return servers, allowed, web_search

    for spec in tools:
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "web_search":
            # OpenRouter quirk — replace with Claude's built-in WebSearch.
            web_search = True
            continue
        if spec.get("type") != "function":
            log.debug("llm: skipping non-function tool entry: %r", spec.get("type"))
            continue
        captured = _make_capture_tool(spec, capture_id)
        func_name = (spec.get("function") or {}).get("name") or "unnamed"
        server_name = f"oai_{func_name}"
        server = create_sdk_mcp_server(name=server_name, version="1.0", tools=[captured])
        servers[server_name] = server
        allowed.append(f"mcp__{server_name}__{func_name}")
    return servers, allowed, web_search


# --------------------------------------------------------------------------
# Message serialization — OpenAI messages list → single prompt string.
# claude-agent-sdk's `query()` takes a single string per turn. We fold the
# entire conversation into one string with explicit role markers; tool_use
# and tool_result are serialized as JSON blocks. This loses some of the
# native Anthropic structure but keeps full information flow.
# --------------------------------------------------------------------------


def _stringify_content(content: Any) -> str:
    """Flatten OpenAI content (string or list of blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(str(block.get("text") or ""))
                elif t == "image_url":
                    url = (block.get("image_url") or {}).get("url") or ""
                    parts.append(f"[image: {url[:80]}{'…' if len(url) > 80 else ''}]")
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _build_messages_prompt(messages: List[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """Split messages into (prompt, system_prompt)."""
    system_parts: List[str] = []
    body_parts: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            system_parts.append(_stringify_content(msg.get("content")))
            continue
        if role == "user":
            body_parts.append(f"USER: {_stringify_content(msg.get('content'))}")
        elif role == "assistant":
            text = _stringify_content(msg.get("content"))
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                tc_blob = json.dumps(tool_calls, ensure_ascii=False)
                text = (text + "\n" if text else "") + f"[tool_calls]: {tc_blob}"
            body_parts.append(f"ASSISTANT: {text}")
        elif role == "tool":
            tcid = msg.get("tool_call_id") or "?"
            body_parts.append(
                f"TOOL_RESULT[{tcid}]: {_stringify_content(msg.get('content'))}"
            )
        else:
            body_parts.append(f"{str(role).upper()}: {_stringify_content(msg.get('content'))}")
    system_prompt = "\n\n".join(p for p in system_parts if p) or None
    prompt = "\n\n".join(body_parts) or "(empty conversation)"
    return prompt, system_prompt


# --------------------------------------------------------------------------
# Async core — one chat call.
# --------------------------------------------------------------------------


async def _chat_async(
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]],
    reasoning_effort: str,
    max_tokens: int,
    tool_choice: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    capture_id = uuid4().hex
    prompt, system_prompt = _build_messages_prompt(messages)
    mcp_servers, allowed, web_search = _build_capture_servers(tools, capture_id)

    if web_search:
        # Claude's built-in WebSearch / WebFetch tools, available on subscription.
        for t in ("WebSearch", "WebFetch"):
            if t not in allowed:
                allowed.append(t)

    options = ClaudeAgentOptions(
        model=_map_model(model),
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers if mcp_servers else {},
        allowed_tools=allowed,
        effort=_map_effort(reasoning_effort),
        # tools+1 turn lets Claude call once and then return.
        # No tools → 1 turn (single response).
        max_turns=2 if tools else 1,
    )

    text_parts: List[str] = []
    captured_calls: List[Dict[str, Any]] = []
    usage: Dict[str, Any] = {}
    model_used: Optional[str] = options.model
    started = time.perf_counter()

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in (msg.content or []):
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            full = block.name or ""
                            short = full.split("__")[-1] if "__" in full else full
                            captured_calls.append(
                                {
                                    "id": block.id,
                                    "type": "function",
                                    "function": {
                                        "name": short,
                                        "arguments": json.dumps(
                                            block.input or {}, ensure_ascii=False
                                        ),
                                    },
                                }
                            )
                        elif isinstance(block, ThinkingBlock):
                            # Don't expose thinking — compatible with `exclude=true`
                            # behaviour the original had.
                            continue
                elif isinstance(msg, ResultMessage):
                    raw_usage = getattr(msg, "usage", None)
                    if isinstance(raw_usage, dict):
                        usage = raw_usage
                    raw_model = getattr(msg, "model", None)
                    if isinstance(raw_model, str):
                        model_used = raw_model
                    break
    finally:
        with _CAPTURE_LOCK:
            _CAPTURE.pop(capture_id, None)

    duration_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "llm.chat done: model=%s effort=%s tools=%d ms=%d text_len=%d tool_calls=%d",
        model_used,
        reasoning_effort,
        len(tools or []),
        duration_ms,
        sum(len(t) for t in text_parts),
        len(captured_calls),
    )

    msg_dict: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if captured_calls:
        msg_dict["tool_calls"] = captured_calls

    norm_usage = _normalize_usage(usage)
    return msg_dict, norm_usage


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Map Anthropic SDK usage shape to OpenRouter shape brain expects."""
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or 0
    )
    completion_tokens = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or 0
    )
    cached_tokens = int(
        usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or 0
    )
    cache_write_tokens = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or 0
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost": 0.0,  # subscription path — no per-call cost
    }


# --------------------------------------------------------------------------
# Sync↔async bridge. brain code is sync; we run the async core on a fresh
# event loop in a background thread so we don't conflict with any caller's
# loop. ThreadPoolExecutor with max_workers=1 keeps it cheap.
# --------------------------------------------------------------------------


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-claude-")


def _run_sync(coro):
    def _runner():
        return asyncio.run(coro)

    return _executor.submit(_runner).result()


# --------------------------------------------------------------------------
# Public LLMClient — same shape as before.
# --------------------------------------------------------------------------


class LLMClient:
    """Claude Code subscription wrapper. All LLM calls go through this class.

    ``api_key`` and ``base_url`` parameters are accepted for backwards
    compatibility but ignored — auth is handled by the local ``claude`` CLI.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        # Stored only for trace/debug; no longer used.
        self._api_key = api_key
        self._base_url = base_url

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns (response_message_dict, usage_dict)."""
        return _run_sync(
            _chat_async(messages, model, tools, reasoning_effort, max_tokens, tool_choice)
        )

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 1024,
        reasoning_effort: str = "low",
    ) -> Tuple[str, Dict[str, Any]]:
        """Send a vision query — text + images, no tools, no loop."""
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images or []:
            if not isinstance(img, dict):
                continue
            if "url" in img:
                content.append({"type": "image_url", "image_url": {"url": img["url"]}})
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                    }
                )
            else:
                log.warning("vision_query: skipping image with unknown format")

        messages = [{"role": "user", "content": content}]
        msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        return os.environ.get("OUROBOROS_MODEL", _DEFAULT_CLAUDE)

    def available_models(self) -> List[str]:
        main = self.default_model()
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models: List[str] = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models
