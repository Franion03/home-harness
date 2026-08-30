"""OpenAI-compatible adapter.

One adapter covers every vendor that speaks /v1/chat/completions: openai,
openrouter, groq, mistral, deepseek and Cloudflare Workers AI. They differ only
in the gateway slug and which key they want, so the registry instantiates this
class once per vendor.
"""

from __future__ import annotations

import json
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

log = logging.getLogger("harness.provider.openai")


class OpenAICompatProvider:
    def __init__(
        self,
        gateway: Gateway,
        api_key: str,
        *,
        slug: str = "openai",
        path: str = "v1/chat/completions",
        extra_headers: dict[str, str] | None = None,
        options: dict[str, Any] | None = None,
    ):
        self._gw = gateway
        self._key = api_key
        self.slug = slug
        self._path = path
        self._extra_headers = extra_headers or {}
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
            raise ProviderError(f"no API key configured for '{self.slug}'", retryable=False)

        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            wire.extend(self._encode_message(m))

        body: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if temperature is not None:
            body["temperature"] = temperature

        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        data = await self._gw.post_json(self.slug, self._path, headers=headers, json=body)
        return self._decode(data, model)

    # ---- wire encoding -------------------------------------------------

    def _encode_message(self, m: Message) -> list[dict[str, Any]]:
        """One canonical Message can become several OpenAI messages.

        OpenAI puts each tool result in its own `role: "tool"` message, whereas
        the canonical form (like Anthropic) groups them into one user turn.
        """
        results = [b for b in m.content if isinstance(b, ToolResult)]
        if results:
            return [
                {
                    "role": "tool",
                    "tool_call_id": r.tool_use_id,
                    "content": r.content,
                }
                for r in results
            ]

        text = "".join(b.text for b in m.content if isinstance(b, Text))
        calls = [b for b in m.content if isinstance(b, ToolUse)]
        if calls:
            return [
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.input),
                            },
                        }
                        for c in calls
                    ],
                }
            ]
        return [{"role": m.role, "content": text}]

    def _decode(self, data: dict[str, Any], model: str) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.slug}: response contained no choices")
        message = choices[0].get("message") or {}

        calls: list[ToolUse] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                log.warning("%s: unparseable tool arguments: %s", self.slug, raw_args[:200])
                args = {}
            calls.append(
                ToolUse(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), input=args)
            )

        usage = data.get("usage") or {}
        return Completion(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason=choices[0].get("finish_reason") or "stop",
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            model=data.get("model", model),
            raw=data,
        )
