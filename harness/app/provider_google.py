"""Google AI Studio (Gemini) adapter -- generateContent.

Gemini differs from the other two in three ways the adapter has to absorb:
the assistant role is called "model", function calls carry no id, and the
schema dialect rejects several standard JSON Schema keywords.
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

log = logging.getLogger("harness.provider.google")

# Gemini's function-declaration schema is a strict subset of JSON Schema and
# 400s on anything it does not recognise.
_SCHEMA_ALLOWED = {
    "type", "format", "description", "nullable", "enum",
    "properties", "required", "items", "anyOf",
}


def _clean_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k not in _SCHEMA_ALLOWED:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
            elif k in ("items", "anyOf"):
                out[k] = (
                    [_clean_schema(i) for i in v] if isinstance(v, list) else _clean_schema(v)
                )
            else:
                out[k] = v
        return out
    return node


class GoogleProvider:
    slug = "google-ai-studio"

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
            raise ProviderError("GOOGLE_AI_API_KEY is not set", retryable=False)

        gen: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            gen["temperature"] = temperature

        body: dict[str, Any] = {
            "contents": [self._encode_message(m) for m in messages],
            "generationConfig": gen,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _clean_schema(t.parameters),
                        }
                        for t in tools
                    ]
                }
            ]

        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        data = await self._gw.post_json(
            self.slug, f"v1beta/models/{model}:generateContent", headers=headers, json=body
        )
        return self._decode(data, model)

    # ---- wire encoding -------------------------------------------------

    def _encode_message(self, m: Message) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, Text):
                if b.text:
                    parts.append({"text": b.text})
            elif isinstance(b, ToolUse):
                parts.append({"functionCall": {"name": b.name, "args": b.input}})
            elif isinstance(b, ToolResult):
                # Gemini matches results to calls by name, not by id.
                name = b.tool_use_id.split(":", 1)[0]
                parts.append(
                    {
                        "functionResponse": {
                            "name": name,
                            "response": {"result": b.content},
                        }
                    }
                )
        if not parts:
            parts = [{"text": ""}]
        role = "model" if m.role == "assistant" else "user"
        return {"role": role, "parts": parts}

    def _decode(self, data: dict[str, Any], model: str) -> Completion:
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(f"google: no candidates returned ({feedback})")

        candidate = candidates[0]
        text_parts: list[str] = []
        calls: list[ToolUse] = []
        for i, part in enumerate((candidate.get("content") or {}).get("parts") or []):
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                name = fc.get("name", "")
                # Synthesise a stable id; _encode_message reads the name back out.
                calls.append(
                    ToolUse(id=f"{name}:{i}", name=name, input=fc.get("args") or {})
                )

        usage = data.get("usageMetadata") or {}
        return Completion(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=candidate.get("finishReason", "STOP"),
            usage=Usage(
                input_tokens=usage.get("promptTokenCount", 0),
                output_tokens=usage.get("candidatesTokenCount", 0),
            ),
            model=model,
            raw=data,
        )
