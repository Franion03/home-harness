"""Per-session conversation history in SQLite on the PVC.

Messages are stored in the canonical block form, so a conversation started
against one provider continues cleanly against another after you swap models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from provider_base import Block, Message, Text, ToolResult, ToolUse

log = logging.getLogger("harness.memory")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


def encode_block(b: Block) -> dict[str, Any]:
    if isinstance(b, Text):
        return {"t": "text", "text": b.text}
    if isinstance(b, ToolUse):
        return {"t": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResult):
        return {
            "t": "tool_result",
            "tool_use_id": b.tool_use_id,
            "content": b.content,
            "is_error": b.is_error,
        }
    raise TypeError(f"cannot encode block {b!r}")


def decode_block(d: dict[str, Any]) -> Block:
    kind = d.get("t")
    if kind == "text":
        return Text(d.get("text", ""))
    if kind == "tool_use":
        return ToolUse(d.get("id", ""), d.get("name", ""), d.get("input") or {})
    if kind == "tool_result":
        return ToolResult(
            d.get("tool_use_id", ""), d.get("content", ""), bool(d.get("is_error"))
        )
    raise ValueError(f"unknown stored block type: {kind}")


class Memory:
    def __init__(self, path: str):
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        log.info("session store ready at %s", self._path)

    # ---- sync bodies, run off the event loop by the async wrappers -----

    def _append(self, session_id: str, messages: list[Message]) -> None:
        rows = [
            (session_id, m.role, json.dumps([encode_block(b) for b in m.content]))
            for m in messages
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", rows
            )

    def _history(self, session_id: str, limit: int) -> list[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()

        messages: list[Message] = []
        for row in reversed(rows):
            try:
                blocks = [decode_block(b) for b in json.loads(row["content"])]
            except (ValueError, TypeError) as exc:
                log.warning("skipping unreadable stored message: %s", exc)
                continue
            messages.append(Message(role=row["role"], content=blocks))
        return _repair(messages)

    def _clear(self, session_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            return cur.rowcount

    def _sessions(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, COUNT(*) AS n, MAX(created_at) AS last "
                "FROM messages GROUP BY session_id ORDER BY last DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- async surface -------------------------------------------------

    async def append(self, session_id: str, messages: list[Message]) -> None:
        await asyncio.to_thread(self._append, session_id, messages)

    async def history(self, session_id: str, limit: int) -> list[Message]:
        return await asyncio.to_thread(self._history, session_id, limit)

    async def clear(self, session_id: str) -> int:
        return await asyncio.to_thread(self._clear, session_id)

    async def sessions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._sessions)


def _repair(messages: list[Message]) -> list[Message]:
    """Drop a leading fragment that would make an invalid request.

    Fetching the last N messages can slice a conversation mid tool-call, and
    every provider rejects a tool_result with no matching tool_use (or a
    history that does not start with a user turn).
    """
    while messages and (
        messages[0].role != "user"
        or any(isinstance(b, ToolResult) for b in messages[0].content)
    ):
        messages.pop(0)

    # A trailing assistant turn holding unanswered tool_use blocks is equally
    # invalid -- it must be followed by results we no longer have.
    while messages and any(isinstance(b, ToolUse) for b in messages[-1].content):
        messages.pop()

    return messages
