"""Canonical, provider-neutral types for the harness.

Nothing above this layer knows which vendor is answering. A provider adapter's
only job is to translate these dataclasses to and from one vendor's wire format.
Adding a vendor means adding one adapter and one line in PROVIDERS -- no changes
to the agent loop, the tools, or the API surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["user", "assistant"]


@dataclass
class Text:
    text: str


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = Text | ToolUse | ToolResult


@dataclass
class Message:
    role: Role
    content: list[Block]

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role="user", content=[Text(text)])

    @staticmethod
    def assistant(text: str) -> "Message":
        return Message(role="assistant", content=[Text(text)])


@dataclass
class ToolSpec:
    """A tool described once, in JSON Schema, for every provider."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolUse] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderError(RuntimeError):
    """Raised by an adapter when a vendor call fails.

    `retryable` tells the router whether falling back to another model is
    worth attempting; a 400 from a malformed request is not.
    """

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Provider(Protocol):
    """Every adapter implements exactly this."""

    slug: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float | None = None,
    ) -> Completion: ...


def text_of(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, Text))
