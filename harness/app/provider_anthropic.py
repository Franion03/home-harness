"""Anthropic adapter -- native Messages API (not an OpenAI-compatible shim).

Using the native shape keeps tool_use/tool_result fidelity and leaves room for
thinking and effort, which the compatibility endpoints flatten away.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway import Gateway
from provider_base import (
    Completion,
    Message,
    ProviderError,
    Text,
    ToolResult,
    ToolSpec,
    ToolUse,
    Usage,
)

log = logging.getLogger("harness.provider.anthropic")

ANTHROPIC_VERSION = "2023-06-01"

# These models reject temperature/top_p/top_k with a 400, and reject
# thinking.budget_tokens. Sending sampling params to them is a hard error, so
# the adapter drops them rather than letting a stray config value break chat.
NO_SAMPLING_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def _rejects_sampling(model: str) -> bool:
    return model.startswith(NO_SAMPLING_PREFIXES)


class AnthropicProvider:
    slug = "anthropic"

    def __init__(self, gateway: Gateway, api_key: str, *, options: dict[str, Any] | None = None):
        self._gw = gateway
        self._key = api_key
        self._options = options or {}

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float | None = None,
    ) -> Completion:
        if not self._key:
            raise ProviderError("ANTHROPIC_API_KEY is not set", retryable=False)

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [self._encode_message(m) for m in messages],
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        if temperature is not None:
            if _rejects_sampling(model):
                log.debug("dropping temperature for %s (model rejects sampling params)", model)
            else:
                body["temperature"] = temperature

        # Opt-in extras, straight from the route config in models.yaml.
        thinking = self._options.get("thinking")
        if thinking == "adaptive":
            body["thinking"] = {"type": "adaptive"}
        effort = self._options.get("effort")
        if effort:
            body["output_config"] = {"effort": effort}

        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        data = await self._gw.post_json("anthropic", "v1/messages", headers=headers, json=body)
        return self._decode(data, model)

    # ---- wire encoding -------------------------------------------------

    def _encode_message(self, m: Message) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, Text):
                if b.text:
                    blocks.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUse):
                blocks.append(
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                )
            elif isinstance(b, ToolResult):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.tool_use_id,
                        "content": b.content,
                        **({"is_error": True} if b.is_error else {}),
                    }
                )
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        return {"role": m.role, "content": blocks}

    def _decode(self, data: dict[str, Any], model: str) -> Completion:
        stop = data.get("stop_reason") or "end_turn"
        if stop == "refusal":
            details = data.get("stop_details") or {}
            raise ProviderError(
                f"anthropic declined the request (category={details.get('category')})",
                retryable=True,
            )

        text_parts: list[str] = []
        calls: list[ToolUse] = []
        for block in data.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                calls.append(
                    ToolUse(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input") or {},
                    )
                )

        usage = data.get("usage") or {}
        return Completion(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=stop,
            usage=Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            model=data.get("model", model),
            raw=data,
        )
