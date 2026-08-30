"""Configuration: environment for secrets, models.yaml for routing.

The split is deliberate. Secrets come from the environment (sealed secrets in
the cluster); *which model answers which kind of request* comes from a YAML
file mounted from a ConfigMap, so swapping the LLM is a ConfigMap edit and a
rollout restart -- never a code change or an image rebuild.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("harness.config")

DEFAULT_ROUTES: dict[str, Any] = {
    "routes": {
        "chat": {
            "primary": "anthropic/claude-opus-5",
            "fallback": "openai/gpt-4o-mini",
            "max_tokens": 4096,
        },
        "voice": {
            "primary": "anthropic/claude-opus-5",
            "fallback": "google-ai-studio/gemini-2.0-flash",
            "max_tokens": 1024,
        },
    },
    "stt": {"primary": "openai/gpt-4o-transcribe", "fallback": "openai/whisper-1"},
    "tts": {
        "primary": "openai/tts-1",
        "fallback": "",
        "voice": "alloy",
        "format": "mp3",
    },
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class RouteConfig:
    primary: str
    fallback: str = ""
    max_tokens: int = 4096
    temperature: float | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def chain(self) -> list[str]:
        return [m for m in (self.primary, self.fallback) if m]


@dataclass
class Settings:
    # -- Cloudflare AI Gateway
    cf_account_id: str = field(default_factory=lambda: _env("CF_ACCOUNT_ID"))
    cf_gateway: str = field(default_factory=lambda: _env("CF_AI_GATEWAY", "home-harness"))
    cf_gateway_token: str = field(default_factory=lambda: _env("CF_AI_GATEWAY_TOKEN"))
    gateway_mode: str = field(default_factory=lambda: _env("AI_GATEWAY_MODE", "gateway"))
    cache_ttl: int = field(default_factory=lambda: int(_env("AI_GATEWAY_CACHE_TTL", "0") or 0))

    # -- Vendor keys. Every one is optional; a route only needs the keys it names.
    anthropic_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openai_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    google_key: str = field(default_factory=lambda: _env("GOOGLE_AI_API_KEY"))
    openrouter_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    groq_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    workers_ai_key: str = field(default_factory=lambda: _env("CF_WORKERS_AI_TOKEN"))
    elevenlabs_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    deepgram_key: str = field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))

    # -- Home Assistant
    ha_url: str = field(default_factory=lambda: _env("HA_URL"))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN"))

    # -- Google Calendar (OAuth user consent -> refresh token)
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_refresh_token: str = field(default_factory=lambda: _env("GOOGLE_REFRESH_TOKEN"))
    calendar_id: str = field(default_factory=lambda: _env("GOOGLE_CALENDAR_ID", "primary"))

    # -- Harness
    api_key: str = field(default_factory=lambda: _env("HARNESS_API_KEY"))
    db_path: str = field(default_factory=lambda: _env("HARNESS_DB", "/data/harness.db"))
    routes_file: str = field(default_factory=lambda: _env("HARNESS_ROUTES", "/config/models.yaml"))
    timezone: str = field(default_factory=lambda: _env("TZ", "Europe/Madrid"))
    max_tool_iterations: int = field(
        default_factory=lambda: int(_env("HARNESS_MAX_TOOL_ITERATIONS", "8") or 8)
    )
    history_turns: int = field(
        default_factory=lambda: int(_env("HARNESS_HISTORY_TURNS", "12") or 12)
    )

    _raw_routes: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._raw_routes = self._load_routes()

    def _load_routes(self) -> dict[str, Any]:
        path = Path(self.routes_file)
        if not path.is_file():
            log.warning("routes file %s not found -- using built-in defaults", path)
            return DEFAULT_ROUTES
        try:
            loaded = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            log.error("routes file %s is invalid YAML (%s) -- using defaults", path, exc)
            return DEFAULT_ROUTES
        merged = {**DEFAULT_ROUTES, **loaded}
        merged["routes"] = {**DEFAULT_ROUTES["routes"], **(loaded.get("routes") or {})}
        return merged

    def reload_routes(self) -> None:
        """Re-read models.yaml. A ConfigMap edit lands here without a restart."""
        self._raw_routes = self._load_routes()

    def route(self, name: str) -> RouteConfig:
        routes = self._raw_routes.get("routes") or {}
        raw = routes.get(name) or routes.get("chat") or DEFAULT_ROUTES["routes"]["chat"]
        return RouteConfig(
            primary=raw.get("primary", ""),
            fallback=raw.get("fallback", ""),
            max_tokens=int(raw.get("max_tokens", 4096)),
            temperature=raw.get("temperature"),
            options=raw.get("options") or {},
        )

    def speech(self, kind: str) -> dict[str, Any]:
        """kind is 'stt' or 'tts'."""
        return self._raw_routes.get(kind) or DEFAULT_ROUTES[kind]

    @property
    def system_prompt(self) -> str:
        custom = self._raw_routes.get("system_prompt")
        if custom:
            return str(custom)
        return DEFAULT_SYSTEM_PROMPT

    def describe(self) -> dict[str, Any]:
        """Non-secret view of the active configuration, for /health."""
        return {
            "gateway_mode": self.gateway_mode if self.cf_account_id else "direct",
            "gateway": self.cf_gateway,
            "routes": {
                name: {"primary": r.get("primary"), "fallback": r.get("fallback")}
                for name, r in (self._raw_routes.get("routes") or {}).items()
            },
            "stt": self.speech("stt").get("primary"),
            "tts": self.speech("tts").get("primary"),
            "providers_configured": sorted(self.configured_providers()),
            "tools": {
                "home_assistant": bool(self.ha_url and self.ha_token),
                "google_calendar": bool(self.google_refresh_token and self.google_client_id),
            },
        }

    def configured_providers(self) -> set[str]:
        pairs = {
            "anthropic": self.anthropic_key,
            "openai": self.openai_key,
            "google-ai-studio": self.google_key,
            "openrouter": self.openrouter_key,
            "groq": self.groq_key,
            "workers-ai": self.workers_ai_key,
            "elevenlabs": self.elevenlabs_key,
            "deepgram": self.deepgram_key,
        }
        return {slug for slug, key in pairs.items() if key}


DEFAULT_SYSTEM_PROMPT = """You are the assistant running on Francisco's home server.

You control the house through Home Assistant and manage his Google Calendar.

Rules:
- Prefer acting over asking. If the request is unambiguous, call the tool.
- Before switching something off that someone may be using, or deleting a
  calendar event, say what you are about to do and confirm first.
- When you look something up, answer with the value, not a description of how
  you found it.
- Your replies are often read aloud. Keep them to one or two short sentences,
  with no markdown, no bullet points and no emoji. Say "22 degrees", not "22°C".
- Today's date and the user's timezone are given below; use them to resolve
  "tomorrow", "next Tuesday" and similar into real dates.
- If a tool fails, say plainly what did not work. Never invent a result.
"""
