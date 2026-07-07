"""LLM client with schema-enforced (forced) tool calling.

Speaks two wire protocols, auto-detected from the configured base_url:
Anthropic messages for api.anthropic.com, OpenAI-compatible chat completions
for everything else (local Ollama/LM Studio servers, OpenAI, DeepSeek,
Moonshot, Perplexity, and other compatible providers). One model serves all
agent roles.

Forcing mechanism:
  * Anthropic: tool_choice = {"type": "tool", "name": <tool>}
  * OpenAI-compatible: tool_choice = "required" with a single tool offered
The returned arguments are validated with pydantic. On validation failure the
error is appended to the conversation and the call is retried, max 2 retries,
after which the run fails loudly (ForcedToolError).

Conversation format is provider-neutral: plain {"role", "content"} messages.
Tool calls and tool results are serialized back into the transcript as text,
which keeps multi-turn agent loops identical across providers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ValidationError

from config import LLMConfig

MAX_FORCED_RETRIES = 2


class LLMError(Exception):
    """Transport- or protocol-level failure talking to the model."""


class ForcedToolError(LLMError):
    """The model could not produce schema-valid tool arguments within budget."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: BaseModel

    def transcript_line(self) -> str:
        return f"[tool_call {self.name}] {self.arguments.model_dump_json()}"


def tool_result_message(name: str, result: Any) -> dict[str, str]:
    """Serialize a tool execution result back into the conversation."""
    if isinstance(result, BaseModel):
        payload = result.model_dump_json()
    else:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    return {"role": "user", "content": f"[tool_result {name}] {payload}"}


def _tool_decl_openai(name: str, description: str, model: type[BaseModel]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }


def _tool_decl_anthropic(name: str, description: str, model: type[BaseModel]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": model.model_json_schema(),
    }


class LLMClient:
    """Thin, dependency-free client over httpx with forced-tool helpers."""

    def __init__(self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.Client(
            timeout=cfg.timeout_seconds,
            transport=transport,
            follow_redirects=True,
        )

    # -- public API ---------------------------------------------------------

    def forced_tool(
        self,
        role: str,
        tool: "ToolDecl",
        messages: list[dict[str, str]],
    ) -> BaseModel:
        """Force the model to call exactly `tool`; return validated arguments."""
        calls = self._tool_turn(role, [tool], messages, force_name=tool.name)
        return calls[0].arguments

    def choose_tool(
        self,
        role: str,
        tools: list["ToolDecl"],
        messages: list[dict[str, str]],
    ) -> list[ToolCall]:
        """The model must call one of `tools` (any); direct answers are rejected."""
        return self._tool_turn(role, tools, messages, force_name=None)

    # -- internals ----------------------------------------------------------

    def _tool_turn(
        self,
        role: str,
        tools: list["ToolDecl"],
        messages: list[dict[str, str]],
        force_name: str | None,
    ) -> list[ToolCall]:
        by_name = {t.name: t for t in tools}
        convo = list(messages)
        last_error = "model returned no tool call"
        for _attempt in range(1 + MAX_FORCED_RETRIES):
            raw_calls, raw_text = self._request(role, tools, convo, force_name)
            if not raw_calls:
                last_error = (
                    "Direct answers are rejected. You MUST respond with a call to "
                    + (force_name or f"one of: {', '.join(by_name)}")
                )
                convo = convo + [
                    {"role": "assistant", "content": raw_text or "(no tool call)"},
                    {"role": "user", "content": f"VALIDATION ERROR: {last_error}"},
                ]
                continue
            try:
                calls = []
                for name, args in raw_calls:
                    if name not in by_name:
                        raise ValueError(f"tool {name!r} is not allowed here")
                    if force_name is not None and name != force_name:
                        raise ValueError(f"you must call {force_name!r}, not {name!r}")
                    calls.append(ToolCall(name, by_name[name].model.model_validate(args)))
                return calls
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                last_error = str(e)
                convo = convo + [
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            [{"tool": n, "arguments": a} for n, a in raw_calls],
                            ensure_ascii=False,
                            default=str,
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "VALIDATION ERROR: your tool arguments were rejected. "
                            f"Fix them and call the tool again.\n{last_error}"
                        ),
                    },
                ]
        raise ForcedToolError(
            f"model failed to produce valid arguments for "
            f"{force_name or '/'.join(by_name)} after {1 + MAX_FORCED_RETRIES} attempts: {last_error}"
        )

    def _request(
        self,
        role: str,
        tools: list["ToolDecl"],
        messages: list[dict[str, str]],
        force_name: str | None,
    ) -> tuple[list[tuple[str, Any]], str]:
        """Return ([(tool_name, raw_args_dict)], assistant_text)."""
        if self.cfg.protocol == "anthropic":
            return self._request_anthropic(role, tools, messages, force_name)
        return self._request_openai(role, tools, messages, force_name)

    def _request_openai(self, role, tools, messages, force_name):
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "tools": [_tool_decl_openai(t.name, t.description, t.model) for t in tools],
            "tool_choice": "required",
        }
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        data = self._post(f"{self.cfg.base_url.rstrip('/')}/chat/completions", payload, headers)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"malformed chat-completions response: {e}") from e
        raw_calls: list[tuple[str, Any]] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments", "{}")
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            raw_calls.append((fn.get("name", ""), args))
        return raw_calls, msg.get("content") or ""

    def _request_anthropic(self, role, tools, messages, force_name):
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        convo = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "messages": convo,
            "tools": [_tool_decl_anthropic(t.name, t.description, t.model) for t in tools],
            "tool_choice": (
                {"type": "tool", "name": force_name} if force_name else {"type": "any"}
            ),
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key
        # tolerate base_url given with or without a trailing /v1
        base = self.cfg.base_url.rstrip("/")
        base = base.removesuffix("/v1")
        data = self._post(f"{base}/v1/messages", payload, headers)
        raw_calls: list[tuple[str, Any]] = []
        text_parts: list[str] = []
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                raw_calls.append((block.get("name", ""), block.get("input", {})))
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        return raw_calls, "".join(text_parts)

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        try:
            resp = self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM endpoint unreachable at {url}: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"LLM endpoint error {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM endpoint returned non-JSON: {resp.text[:200]}") from e


@dataclass(frozen=True)
class ToolDecl:
    """What the client needs to offer a tool to the model."""

    name: str
    description: str
    model: type[BaseModel]
