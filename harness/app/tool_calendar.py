"""Google Calendar tools -- Calendar API v3 over plain HTTPS.

Raw REST rather than the google-api-python-client so the image stays small and
the dependency set is httpx + fastapi. Auth is a user refresh token, which is
the only way to reach a personal calendar.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from google_auth import GoogleAuth

log = logging.getLogger("harness.tools.calendar")

API = "https://www.googleapis.com/calendar/v3"


class Calendar:
    def __init__(
        self,
        auth: GoogleAuth,
        default_calendar: str,
        timezone: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._auth = auth
        self._default = default_calendar or "primary"
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
                f"Google Calendar {method} {path} -> {resp.status_code}: {resp.text[:250]}"
            )
        return resp.json() if resp.content else {}

    # ---- tool implementations -----------------------------------------

    async def list_calendars(self) -> str:
        data = await self._request("GET", "/users/me/calendarList")
        rows = [
            {
                "id": c.get("id"),
                "name": c.get("summary"),
                "primary": bool(c.get("primary")),
                "access": c.get("accessRole"),
            }
            for c in data.get("items", [])
        ]
        return json.dumps(rows, ensure_ascii=False)

    async def list_events(
        self,
        days_ahead: int = 7,
        time_min: str = "",
        time_max: str = "",
        query: str = "",
        calendar_id: str = "",
        max_results: int = 25,
    ) -> str:
        now = self._now()
        start = time_min or now.isoformat()
        end = time_max or (now + timedelta(days=max(1, days_ahead))).isoformat()

        params = {
            "timeMin": start,
            "timeMax": end,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(min(max(1, max_results), 100)),
            "timeZone": self._tz,
        }
        if query:
            params["q"] = query

        data = await self._request(
            "GET", f"/calendars/{self._cal(calendar_id)}/events", params=params
        )
        events = [self._summarise(e) for e in data.get("items", [])]
        if not events:
            return f"No events between {start} and {end}."
        return json.dumps(events, ensure_ascii=False)

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str = "",
        description: str = "",
        location: str = "",
        all_day: str = "",
        calendar_id: str = "",
    ) -> str:
        body: dict = {"summary": summary}
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        if all_day.lower() in ("true", "yes", "1"):
            body["start"] = {"date": start[:10]}
            body["end"] = {"date": (end or start)[:10]}
        else:
            if not end:
                # Default to a one-hour meeting rather than rejecting the call.
                end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
            body["start"] = {"dateTime": start, "timeZone": self._tz}
            body["end"] = {"dateTime": end, "timeZone": self._tz}

        data = await self._request(
            "POST", f"/calendars/{self._cal(calendar_id)}/events", json=body
        )
        return f"Created '{data.get('summary')}' (id {data.get('id')}) — {self._when(data)}"

    async def update_event(
        self,
        event_id: str,
        summary: str = "",
        start: str = "",
        end: str = "",
        description: str = "",
        location: str = "",
        calendar_id: str = "",
    ) -> str:
        cal = self._cal(calendar_id)
        patch: dict = {}
        if summary:
            patch["summary"] = summary
        if description:
            patch["description"] = description
        if location:
            patch["location"] = location
        if start:
            patch["start"] = {"dateTime": start, "timeZone": self._tz}
        if end:
            patch["end"] = {"dateTime": end, "timeZone": self._tz}
        if not patch:
            return "Nothing to update — provide at least one field to change."

        data = await self._request("PATCH", f"/calendars/{cal}/events/{event_id}", json=patch)
        return f"Updated '{data.get('summary')}' — {self._when(data)}"

    async def delete_event(self, event_id: str, calendar_id: str = "") -> str:
        await self._request(
            "DELETE", f"/calendars/{self._cal(calendar_id)}/events/{event_id}"
        )
        return f"Deleted event {event_id}."

    # ---- helpers -------------------------------------------------------

    def _cal(self, calendar_id: str) -> str:
        # Calendar ids are usually email addresses; the '@' must be encoded
        # because it lands in a path segment.
        return quote(calendar_id or self._default, safe="")

    @staticmethod
    def _when(event: dict) -> str:
        start = event.get("start") or {}
        return start.get("dateTime") or start.get("date") or "unknown time"

    @staticmethod
    def _summarise(event: dict) -> dict:
        start = event.get("start") or {}
        end = event.get("end") or {}
        return {
            "id": event.get("id"),
            "summary": event.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "location": event.get("location"),
            "all_day": "date" in start,
        }

    async def aclose(self) -> None:
        await self._client.aclose()
