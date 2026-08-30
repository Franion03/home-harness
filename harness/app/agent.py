"""The agent loop: prompt -> model -> tools -> model -> ... -> answer.

Provider-neutral throughout. It only ever sees canonical Messages, ToolSpecs
and Completions, so the same loop runs unchanged whichever vendor answers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import Settings
from llm import Router
from memory import Memory
from provider_base import Message, Text, ToolResult, ToolUse, Usage
from tool_registry import ToolRegistry

log = logging.getLogger("harness.agent")


@dataclass
class AgentResult:
    text: str
    session_id: str
    model: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    elapsed_ms: int = 0


class Agent:
    def __init__(
        self,
        *,
        settings: Settings,
        router: Router,
        registry: ToolRegistry,
        memory: Memory,
    ):
        self._settings = settings
        self._router = router
        self._registry = registry
        self._memory = memory

    def _system_prompt(self) -> str:
        tz = self._settings.timezone
        now = datetime.now(ZoneInfo(tz))
        return (
            f"{self._settings.system_prompt}\n\n"
            f"Current date and time: {now.strftime('%A, %d %B %Y, %H:%M')} ({tz}).\n"
            f"Available tools: {', '.join(self._registry.names()) or 'none'}."
        )

    async def run(
        self, prompt: str, *, session_id: str, route_name: str = "chat"
    ) -> AgentResult:
        started = time.monotonic()
        route = self._settings.route(route_name)
        tools = self._registry.specs()

        history = await self._memory.history(session_id, self._settings.history_turns)
        turn: list[Message] = [Message.user(prompt)]
        # Only the new turn gets persisted; history is already stored.
        to_persist: list[Message] = list(turn)

        total = Usage()
        performed: list[dict[str, Any]] = []
        answer = ""
        model_used = ""

        for iteration in range(self._settings.max_tool_iterations):
            completion = await self._router.complete(
                route,
                system=self._system_prompt(),
                messages=history + turn,
                tools=tools,
            )
            total.input_tokens += completion.usage.input_tokens
            total.output_tokens += completion.usage.output_tokens
            model_used = completion.model
            answer = completion.text

            assistant_blocks: list[Any] = []
            if completion.text:
                assistant_blocks.append(Text(completion.text))
            assistant_blocks.extend(completion.tool_calls)
            assistant_msg = Message(role="assistant", content=assistant_blocks)
            turn.append(assistant_msg)
            to_persist.append(assistant_msg)

            if not completion.wants_tools:
                break

            results = await self._run_tools(completion.tool_calls, performed)
            # All results for one assistant turn go back in a single user
            # message -- splitting them teaches the model to stop batching.
            result_msg = Message(role="user", content=results)
            turn.append(result_msg)
            to_persist.append(result_msg)
        else:
            log.warning(
                "session %s hit the %s-iteration tool limit",
                session_id,
                self._settings.max_tool_iterations,
            )
            if not answer:
                answer = (
                    "I went back and forth with the house too many times without "
                    "settling it. Could you narrow down what you need?"
                )

        await self._memory.append(session_id, to_persist)
        return AgentResult(
            text=answer,
            session_id=session_id,
            model=model_used,
            tool_calls=performed,
            usage=total,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    async def _run_tools(
        self, calls: list[ToolUse], performed: list[dict[str, Any]]
    ) -> list[ToolResult]:
        """Execute every tool the model asked for, concurrently."""

        async def one(call: ToolUse) -> ToolResult:
            t0 = time.monotonic()
            output, is_error = await self._registry.invoke(call.name, call.input)
            performed.append(
                {
                    "name": call.name,
                    "arguments": call.input,
                    "ok": not is_error,
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            )
            log.info(
                "tool %s(%s) -> %s", call.name, call.input, "error" if is_error else "ok"
            )
            return ToolResult(
                tool_use_id=call.id, content=output[:8000], is_error=is_error
            )

        return list(await asyncio.gather(*(one(c) for c in calls)))
