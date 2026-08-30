"""Tool registry -- one JSON Schema description per capability, plus a handler.

Tools are described once here in provider-neutral form; each adapter translates
the same ToolSpec into its vendor's dialect.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from provider_base import ToolSpec

log = logging.getLogger("harness.tools")

Handler = Callable[..., Awaitable[str] | str]


@dataclass
class Tool:
    spec: ToolSpec
    handler: Handler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(
        self, name: str, description: str, parameters: dict[str, Any], handler: Handler
    ) -> None:
        self._tools[name] = Tool(ToolSpec(name, description, parameters), handler)

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def invoke(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (result_text, is_error).

        Tool failures are returned to the model as text rather than raised, so
        it can recover -- retry with different arguments, or tell the user what
        went wrong -- instead of the whole turn dying.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"No such tool: {name}. Available: {', '.join(self.names())}", True

        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return str(result), False
        except TypeError as exc:
            log.warning("tool %s called with bad arguments %s: %s", name, arguments, exc)
            return f"Invalid arguments for {name}: {exc}", True
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            log.exception("tool %s failed", name)
            return f"{name} failed: {exc}", True


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Shorthand for an object schema."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}
