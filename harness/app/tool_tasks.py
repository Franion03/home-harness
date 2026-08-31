"""Google Tasks tools -- Tasks API v1 over plain HTTPS.

Same shape as tool_calendar: raw REST over httpx, authed with the same user
refresh token. Tasks and Calendar are separate Google APIs but share one
OAuth grant, so this needs no credential of its own -- only the extra
`auth/tasks` scope on the consent that produced the refresh token. A token
minted before that scope was added returns 403 here while Calendar keeps
working, which is the confusing symptom to watch for.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from google_auth import GoogleAuth

log = logging.getLogger("harness.tools.tasks")

API = "https://tasks.googleapis.com/tasks/v1"


class Tasks:
    def __init__(
        self,
        auth: GoogleAuth,
        default_list: str,
        timezone: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._auth = auth
        self._default = default_list or "@default"
        self._tz = timezone
        self._client = httpx.AsyncClient(timeout=25.0, transport=transport)

    @property
    def configured(self) -> bool:
        return self._auth.configured

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._auth.token()}",
            "Content-Type": "application/json",
        }

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self._tz))

    async def _request(self, method: str, path: str, **kw) -> dict:
        resp = await self._client.request(
            method, f"{API}{path}", headers=await self._headers(), **kw
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Google Tasks {method} {path} -> {resp.status_code}: {resp.text[:250]}"
            )
        return resp.json() if resp.content else {}

    # ---- tool implementations -----------------------------------------

    async def list_task_lists(self) -> str:
        data = await self._request("GET", "/users/@me/lists")
        rows = [
            {"id": t.get("id"), "name": t.get("title")}
            for t in data.get("items", [])
        ]
        if not rows:
            return "No task lists found."
        return json.dumps(rows, ensure_ascii=False)

    async def list_tasks(
        self,
        due_within_days: int = 0,
        include_completed: bool = False,
        list_id: str = "",
        max_results: int = 50,
    ) -> str:
        params: dict[str, str] = {
            "maxResults": str(min(max(1, max_results), 100)),
            "showCompleted": "true" if include_completed else "false",
            # showHidden must accompany showCompleted or completed items are
            # filtered out again further down the API.
            "showHidden": "true" if include_completed else "false",
        }
        if due_within_days > 0:
            end = self._now() + timedelta(days=due_within_days)
            # dueMax is exclusive-ish and compares RFC3339 in UTC; the API
            # ignores the time part of due dates, so widen to end of day.
            params["dueMax"] = end.replace(
                hour=23, minute=59, second=59, microsecond=0
            ).isoformat()

        data = await self._request(
            "GET", f"/lists/{self._list(list_id)}/tasks", params=params
        )
        rows = [self._summarise(t) for t in data.get("items", [])]
        # A task with no due date sorts last; otherwise soonest first.
        rows.sort(key=lambda r: (r["due"] is None, r["due"] or ""))
        if not rows:
            return "No tasks found." if not due_within_days else (
                f"No tasks due in the next {due_within_days} day(s)."
            )
        return json.dumps(rows, ensure_ascii=False)

    async def add_task(
        self,
        title: str,
        due: str = "",
        notes: str = "",
        list_id: str = "",
    ) -> str:
        body: dict = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            # Google Tasks stores only the DATE part of due; sending a time is
            # accepted and then silently truncated, so normalise here rather
            # than let the model believe a time was kept.
            body["due"] = f"{due[:10]}T00:00:00.000Z"

        data = await self._request(
            "POST", f"/lists/{self._list(list_id)}/tasks", json=body
        )
        when = f", due {data['due'][:10]}" if data.get("due") else ""
        return f"Added '{data.get('title')}' (id {data.get('id')}){when}."

    async def complete_task(self, task_id: str, list_id: str = "") -> str:
        data = await self._request(
            "PATCH",
            f"/lists/{self._list(list_id)}/tasks/{quote(task_id, safe='')}",
            json={"status": "completed"},
        )
        return f"Completed '{data.get('title', task_id)}'."

    # ---- helpers -------------------------------------------------------

    def _list(self, list_id: str) -> str:
        # '@default' is a literal Google accepts for the user's default list;
        # real ids are opaque strings, but both land in a path segment.
        return quote(list_id or self._default, safe="@")

    @staticmethod
    def _summarise(task: dict) -> dict:
        due = task.get("due")
        return {
            "id": task.get("id"),
            "title": task.get("title", "(untitled)"),
            # Date only -- Google Tasks has no due *time*, whatever it stores.
            "due": due[:10] if due else None,
            "status": task.get("status"),
            "notes": task.get("notes"),
        }

    async def aclose(self) -> None:
        await self._client.aclose()
