"""Home Assistant tools -- read state, call services, delegate to HA's own intents.

Talks to the HA REST API with a long-lived access token. The entity list is
deliberately summarised before it reaches the model: a full /api/states dump on
a real installation is tens of thousands of tokens and would dominate context.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from tool_registry import ToolRegistry, boolean, obj, string

log = logging.getLogger("harness.tools.ha")

# Domains worth exposing to an assistant. Everything else is reachable by
# explicit entity_id but is not listed during discovery.
INTERESTING_DOMAINS = (
    "light", "switch", "climate", "cover", "lock", "fan", "media_player",
    "sensor", "binary_sensor", "scene", "script", "vacuum", "person",
    "alarm_control_panel", "input_boolean", "automation", "weather",
)


class HomeAssistant:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self._headers["Authorization"] != "Bearer ")

    async def _get(self, path: str) -> Any:
        resp = await self._client.get(f"{self.base_url}{path}", headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        resp = await self._client.post(
            f"{self.base_url}{path}", headers=self._headers, json=payload
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ---- tool implementations -----------------------------------------

    async def list_entities(self, domain: str = "", search: str = "") -> str:
        states = await self._get("/api/states")
        rows = []
        for s in states:
            entity_id = s.get("entity_id", "")
            entity_domain = entity_id.split(".", 1)[0]
            if domain and entity_domain != domain:
                continue
            if not domain and entity_domain not in INTERESTING_DOMAINS:
                continue
            name = (s.get("attributes") or {}).get("friendly_name", entity_id)
            if search and search.lower() not in f"{entity_id} {name}".lower():
                continue
            rows.append({"entity_id": entity_id, "name": name, "state": s.get("state")})

        if not rows:
            return "No matching entities found."
        rows.sort(key=lambda r: r["entity_id"])
        truncated = rows[:200]
        note = "" if len(rows) <= 200 else f"\n({len(rows)} matched, showing first 200)"
        return json.dumps(truncated, ensure_ascii=False) + note

    async def get_state(self, entity_id: str) -> str:
        try:
            s = await self._get(f"/api/states/{entity_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return f"No entity called '{entity_id}'. Use ha_list_entities to find the right id."
            raise
        attrs = s.get("attributes") or {}
        # Trim the noisiest attributes; they rarely help and cost a lot of tokens.
        for noisy in ("icon", "supported_features", "supported_color_modes",
                      "device_class", "entity_picture", "hvac_modes", "fan_modes",
                      "preset_modes", "swing_modes", "source_list", "sound_mode_list"):
            attrs.pop(noisy, None)
        return json.dumps(
            {
                "entity_id": s.get("entity_id"),
                "state": s.get("state"),
                "attributes": attrs,
                "last_changed": s.get("last_changed"),
            },
            ensure_ascii=False,
        )

    async def call_service(
        self, domain: str, service: str, entity_id: str = "", data: str = ""
    ) -> str:
        payload: dict[str, Any] = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if data:
            try:
                extra = json.loads(data)
            except json.JSONDecodeError:
                return f"`data` must be a JSON object, got: {data[:120]}"
            if not isinstance(extra, dict):
                return "`data` must be a JSON object."
            payload.update(extra)

        result = await self._post(f"/api/services/{domain}/{service}", payload)
        changed = [
            r.get("entity_id")
            for r in (result if isinstance(result, list) else [])
            if isinstance(r, dict)
        ]
        if changed:
            return f"Called {domain}.{service}. Updated: {', '.join(filter(None, changed))}"
        return f"Called {domain}.{service} on {entity_id or 'no specific entity'}."

    async def conversation(self, text: str) -> str:
        """Hand a phrase to Home Assistant's own intent engine.

        Useful for things HA already understands natively (scenes, area-wide
        commands, custom sentences) without having to model them here.
        """
        result = await self._post(
            "/api/conversation/process", {"text": text, "language": "en"}
        )
        speech = (
            (result.get("response") or {}).get("speech", {}).get("plain", {}).get("speech")
        )
        return speech or json.dumps(result, ensure_ascii=False)[:800]

    async def aclose(self) -> None:
        await self._client.aclose()


def register(registry: ToolRegistry, ha: HomeAssistant) -> None:
    registry.add(
        "ha_list_entities",
        "List Home Assistant entities with their current state. Call this first "
        "when you do not already know the exact entity_id. Filter by domain "
        "(light, switch, climate, sensor, cover, lock, media_player, scene, ...) "
        "or by a search word matching the name.",
        obj(
            {
                "domain": string("Restrict to one domain, e.g. 'light'.", default=""),
                "search": string("Case-insensitive substring of the name or id."),
            }
        ),
        ha.list_entities,
    )
    registry.add(
        "ha_get_state",
        "Get the full current state and attributes of one Home Assistant entity.",
        obj({"entity_id": string("Exact entity id, e.g. 'light.kitchen'.")}, ["entity_id"]),
        ha.get_state,
    )
    registry.add(
        "ha_call_service",
        "Call a Home Assistant service to change something: turn devices on or "
        "off, set brightness or temperature, open covers, run scenes or scripts.",
        obj(
            {
                "domain": string("Service domain, e.g. 'light', 'climate', 'scene'."),
                "service": string("Service name, e.g. 'turn_on', 'set_temperature'."),
                "entity_id": string("Target entity id. Omit for services that need no target."),
                "data": string(
                    "Extra service parameters as a JSON object string, e.g. "
                    '\'{"brightness_pct": 40}\' or \'{"temperature": 21}\'.'
                ),
            },
            ["domain", "service"],
        ),
        ha.call_service,
    )
    registry.add(
        "ha_conversation",
        "Send a natural-language phrase to Home Assistant's own built-in intent "
        "engine. Use this for area-wide commands ('turn off everything "
        "downstairs') or custom sentences configured in Home Assistant.",
        obj({"text": string("The phrase to hand to Home Assistant.")}, ["text"]),
        ha.conversation,
    )
