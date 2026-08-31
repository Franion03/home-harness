"""Configuration.

This service holds two credentials: a Home Assistant long-lived token and a
Google OAuth refresh token. It holds no model-vendor keys — Open WebUI owns
model routing, so nothing here ever talks to an LLM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Settings:
    # -- Home Assistant
    ha_url: str = field(default_factory=lambda: _env("HA_URL").rstrip("/"))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN"))

    # -- Google Calendar (OAuth user consent -> refresh token)
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_refresh_token: str = field(default_factory=lambda: _env("GOOGLE_REFRESH_TOKEN"))
    calendar_id: str = field(default_factory=lambda: _env("GOOGLE_CALENDAR_ID", "primary"))
    # "@default" is the user's default task list; a real id also works.
    tasklist_id: str = field(default_factory=lambda: _env("GOOGLE_TASKLIST_ID", "@default"))

    # -- Health metrics (read-only; Home Assistant does the writing)
    influx_url: str = field(default_factory=lambda: _env("INFLUXDB_URL").rstrip("/"))
    influx_token: str = field(default_factory=lambda: _env("INFLUXDB_TOKEN"))
    influx_org: str = field(default_factory=lambda: _env("INFLUXDB_ORG", "casa"))
    influx_bucket: str = field(default_factory=lambda: _env("INFLUXDB_BUCKET", "health"))

    # -- This service
    api_key: str = field(default_factory=lambda: _env("TOOLS_API_KEY"))
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/Madrid"))

    @property
    def ha_enabled(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    @property
    def health_enabled(self) -> bool:
        return bool(self.influx_url and self.influx_token)

    @property
    def tasks_enabled(self) -> bool:
        # Same OAuth grant as the calendar -- there is no separate credential.
        # Whether the grant carries the tasks scope only shows up as a 403 at
        # call time, so this cannot be checked here.
        return self.calendar_enabled

    @property
    def calendar_enabled(self) -> bool:
        return bool(
            self.google_client_id and self.google_client_secret and self.google_refresh_token
        )

    def describe(self) -> dict[str, object]:
        return {
            "home_assistant": {"enabled": self.ha_enabled, "url": self.ha_url or None},
            "google_calendar": {
                "enabled": self.calendar_enabled,
                "calendar_id": self.calendar_id,
            },
            "google_tasks": {
                "enabled": self.tasks_enabled,
                "tasklist_id": self.tasklist_id,
            },
            "health": {
                "enabled": self.health_enabled,
                "bucket": self.influx_bucket if self.health_enabled else None,
            },
            "timezone": self.timezone,
            "auth_required": bool(self.api_key),
        }
