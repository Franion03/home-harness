"""home-harness — an OpenAPI tool server for the house.

Open WebUI owns the chat UI, sessions, voice and model routing. This service
owns the two things it cannot: control of Home Assistant and management of a
Google Calendar. Open WebUI reads the OpenAPI spec this app publishes and
turns every operation below into a tool the model can call.

That is why the summaries and field descriptions here are unusually
deliberate: they are not documentation for a human reader, they are the
prompt the model sees when deciding whether and how to call a tool.

    Open WebUI → Settings → Tools → add  http://harness.assistant.svc.cluster.local
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import tool_calendar
import tool_homeassistant
import tool_tasks
from config import Settings
from google_auth import GoogleAuth

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("harness")


class Services:
    def __init__(self) -> None:
        self.settings = Settings()
        self.ha: tool_homeassistant.HomeAssistant | None = None
        self.calendar: tool_calendar.Calendar | None = None
        self.tasks: tool_tasks.Tasks | None = None
        self.google_auth: GoogleAuth | None = None

        if self.settings.ha_enabled:
            self.ha = tool_homeassistant.HomeAssistant(
                self.settings.ha_url, self.settings.ha_token
            )
            log.info("Home Assistant tools enabled (%s)", self.settings.ha_url)
        else:
            log.warning("Home Assistant tools disabled — HA_URL/HA_TOKEN not set")

        self.google_auth = GoogleAuth(
            self.settings.google_client_id,
            self.settings.google_client_secret,
            self.settings.google_refresh_token,
        )
        if self.settings.calendar_enabled:
            self.calendar = tool_calendar.Calendar(
                self.google_auth, self.settings.calendar_id, self.settings.timezone
            )
            log.info("Google Calendar tools enabled (%s)", self.settings.calendar_id)
        if self.settings.tasks_enabled:
            self.tasks = tool_tasks.Tasks(
                self.google_auth, self.settings.tasklist_id, self.settings.timezone
            )
            log.info("Google Tasks tools enabled (%s)", self.settings.tasklist_id)
        else:
            log.warning(
                "Google Calendar tools disabled — run scripts/google_oauth.py "
                "for a refresh token"
            )

    async def aclose(self) -> None:
        if self.ha:
            await self.ha.aclose()
        if self.calendar:
            await self.calendar.aclose()
        if self.tasks:
            await self.tasks.aclose()
        if self.google_auth:
            await self.google_auth.aclose()


services: Services | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global services
    services = Services()
    log.info("tool server ready: %s", services.settings.describe())
    if not services.settings.api_key:
        log.warning(
            "TOOLS_API_KEY is unset — this server is unauthenticated. It can "
            "unlock doors and delete calendar events; keep it inside the cluster."
        )
    try:
        yield
    finally:
        await services.aclose()


app = FastAPI(
    title="Home Tools",
    version="3.0.0",
    summary="Control Home Assistant and manage Google Calendar.",
    description=(
        "Tools for a household assistant. Use the Home Assistant operations to "
        "read and change the state of the house, and the calendar operations to "
        "read and manage the user's schedule."
    ),
    lifespan=lifespan,
)

# Open WebUI calls this server from the browser when a user registers it as a
# personal tool server, so the spec and the operations must be reachable
# cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── error handling ───────────────────────────────────────────────────────
#
# A tool call that fails must come back as a legible message, because the
# model is the one that reads it and decides what to do next. An unhandled
# exception would surface to Open WebUI as a bare 500 with a traceback, which
# tells the model nothing and tells the user less.


@app.exception_handler(httpx.HTTPStatusError)
async def upstream_status_error(request: Request, exc: httpx.HTTPStatusError):
    upstream = "Home Assistant" if "/ha/" in request.url.path else "Google Calendar"
    log.warning("%s returned %s for %s", upstream, exc.response.status_code, request.url.path)
    return JSONResponse(
        status_code=502,
        content={"detail": f"{upstream} returned {exc.response.status_code}. "
                           f"The request was not applied."},
    )


@app.exception_handler(httpx.HTTPError)
async def upstream_transport_error(request: Request, exc: httpx.HTTPError):
    upstream = "Home Assistant" if "/ha/" in request.url.path else "Google Calendar"
    log.warning("%s unreachable: %s", upstream, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": f"{upstream} is unreachable right now."},
    )


@app.exception_handler(RuntimeError)
async def runtime_error(request: Request, exc: RuntimeError):
    # Raised by GoogleAuth when a refresh fails, among others.
    log.warning("tool failed: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def svc() -> Services:
    if services is None:  # pragma: no cover - only before lifespan runs
        raise HTTPException(503, "starting up")
    return services


async def require_key(request: Request) -> None:
    expected = svc().settings.api_key
    if not expected:
        return
    supplied = request.headers.get("x-api-key") or ""
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    if supplied != expected:
        raise HTTPException(401, "invalid or missing API key")


Guarded = Depends(require_key)


def ha() -> tool_homeassistant.HomeAssistant:
    h = svc().ha
    if h is None:
        raise HTTPException(
            503, "Home Assistant is not configured on this server (HA_URL/HA_TOKEN)"
        )
    return h


def cal() -> tool_calendar.Calendar:
    c = svc().calendar
    if c is None:
        raise HTTPException(
            503, "Google Calendar is not configured on this server (no refresh token)"
        )
    return c


def tasks() -> tool_tasks.Tasks:
    t = svc().tasks
    if t is None:
        raise HTTPException(
            503, "Google Tasks is not configured on this server (no refresh token)"
        )
    return t


# ── health ───────────────────────────────────────────────────────────────


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "home-harness", "version": app.version,
            **svc().settings.describe()}


# ── Home Assistant ───────────────────────────────────────────────────────


class ServiceCall(BaseModel):
    domain: str = Field(
        description="Service domain, for example 'light', 'switch', 'climate', "
                    "'cover', 'lock', 'media_player', 'scene' or 'script'.",
        examples=["light"],
    )
    service: str = Field(
        description="Service name within the domain, for example 'turn_on', "
                    "'turn_off', 'toggle', 'set_temperature' or 'open_cover'.",
        examples=["turn_off"],
    )
    entity_id: str = Field(
        default="",
        description="The entity to act on, for example 'light.kitchen'. Leave "
                    "empty only for services that need no target.",
        examples=["light.kitchen"],
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra parameters for the service, for example "
                    '{"brightness_pct": 40} or {"temperature": 21}.',
        examples=[{"brightness_pct": 40}],
    )


class Phrase(BaseModel):
    text: str = Field(
        description="A natural-language command to hand to Home Assistant's own "
                    "intent engine.",
        examples=["turn off everything downstairs"],
    )


@app.get(
    "/ha/entities",
    operation_id="list_home_entities",
    summary="List devices and sensors in the house with their current state",
    description=(
        "Search the house for devices, lights, switches, sensors, covers, locks "
        "and media players. Call this first whenever you do not already know an "
        "entity's exact id. Filter by domain, by a search word matching the "
        "name, or both."
    ),
    dependencies=[Guarded],
    tags=["Home Assistant"],
)
async def list_home_entities(
    domain: Annotated[str, Query(
        description="Restrict to one domain, e.g. 'light', 'sensor', 'climate'.",
    )] = "",
    search: Annotated[str, Query(
        description="Case-insensitive substring of the friendly name or entity id.",
    )] = "",
) -> dict[str, Any]:
    return {"result": await ha().list_entities(domain=domain, search=search)}


@app.get(
    "/ha/entities/{entity_id}",
    operation_id="get_home_entity_state",
    summary="Get the current state and attributes of one device or sensor",
    description=(
        "Read one entity in full: its state and its attributes, such as "
        "brightness, temperature or battery level."
    ),
    dependencies=[Guarded],
    tags=["Home Assistant"],
)
async def get_home_entity_state(
    entity_id: Annotated[str, Path(
        description="Exact entity id, for example 'light.kitchen' or "
                    "'sensor.living_room_temperature'.",
    )],
) -> dict[str, Any]:
    return {"result": await ha().get_state(entity_id)}


@app.post(
    "/ha/service",
    operation_id="control_home_device",
    summary="Turn a device on or off, or change a setting in the house",
    description=(
        "Change something in the house: switch lights or plugs on and off, set "
        "brightness or a thermostat, open or close covers, lock or unlock, or "
        "run a scene or script. Confirm with the user first before switching "
        "off something someone may be using, or unlocking anything."
    ),
    dependencies=[Guarded],
    tags=["Home Assistant"],
)
async def control_home_device(call: ServiceCall) -> dict[str, Any]:
    return {
        "result": await ha().call_service(
            domain=call.domain,
            service=call.service,
            entity_id=call.entity_id,
            data=call.data,
        )
    }


@app.post(
    "/ha/conversation",
    operation_id="ask_home_assistant",
    summary="Send a phrase to Home Assistant's own built-in intent engine",
    description=(
        "Hand a natural-language command straight to Home Assistant. Useful for "
        "area-wide commands like 'turn off everything downstairs', and for "
        "custom sentences the user has configured in Home Assistant, which this "
        "server knows nothing about."
    ),
    dependencies=[Guarded],
    tags=["Home Assistant"],
)
async def ask_home_assistant(phrase: Phrase) -> dict[str, Any]:
    return {"result": await ha().conversation(phrase.text)}


# ── Google Calendar ──────────────────────────────────────────────────────


class NewEvent(BaseModel):
    summary: str = Field(
        description="The event title.", examples=["Dentist"]
    )
    start: str = Field(
        description="ISO 8601 start time, for example '2026-09-02T18:00:00'. "
                    "Resolve relative dates like 'tomorrow at 6' yourself before "
                    "calling.",
        examples=["2026-09-02T18:00:00"],
    )
    end: str = Field(
        default="",
        description="ISO 8601 end time. Defaults to one hour after the start.",
    )
    description: str = Field(default="", description="Longer notes for the event.")
    location: str = Field(default="", description="Where the event takes place.")
    all_day: bool = Field(
        default=False,
        description="True for an all-day event; start and end are then dates.",
    )
    calendar_id: str = Field(
        default="", description="Calendar id. Defaults to the primary calendar."
    )


class EventPatch(BaseModel):
    summary: str = Field(default="", description="New title, if changing it.")
    start: str = Field(default="", description="New ISO 8601 start, if moving it.")
    end: str = Field(default="", description="New ISO 8601 end, if moving it.")
    description: str = Field(default="", description="New description.")
    location: str = Field(default="", description="New location.")
    calendar_id: str = Field(
        default="", description="Calendar id. Defaults to the primary calendar."
    )


@app.get(
    "/calendar/events",
    operation_id="list_calendar_events",
    summary="Look up what is scheduled on the user's calendar",
    description=(
        "Read upcoming events. Use this for anything about what is scheduled, "
        "when the user is free or busy, or what is coming up. Returns each "
        "event's id, title, start, end and location."
    ),
    dependencies=[Guarded],
    tags=["Calendar"],
)
async def list_calendar_events(
    days_ahead: Annotated[int, Query(
        ge=1, le=365,
        description="How many days forward to look from now. Ignored if "
                    "time_min and time_max are given.",
    )] = 7,
    time_min: Annotated[str, Query(
        description="ISO 8601 start of the window. Overrides days_ahead.",
    )] = "",
    time_max: Annotated[str, Query(
        description="ISO 8601 end of the window. Overrides days_ahead.",
    )] = "",
    query: Annotated[str, Query(
        description="Free-text search over event titles and descriptions.",
    )] = "",
    calendar_id: Annotated[str, Query(
        description="Calendar id. Defaults to the primary calendar.",
    )] = "",
    max_results: Annotated[int, Query(
        ge=1, le=100, description="Maximum events to return.",
    )] = 25,
) -> dict[str, Any]:
    return {
        "result": await cal().list_events(
            days_ahead=days_ahead, time_min=time_min, time_max=time_max,
            query=query, calendar_id=calendar_id, max_results=max_results,
        )
    }


@app.post(
    "/calendar/events",
    operation_id="create_calendar_event",
    summary="Add a new event to the user's calendar",
    description=(
        "Create an event. Work out the absolute date and time before calling — "
        "the server does not interpret phrases like 'next Tuesday'."
    ),
    dependencies=[Guarded],
    tags=["Calendar"],
)
async def create_calendar_event(event: NewEvent) -> dict[str, Any]:
    return {
        "result": await cal().create_event(
            summary=event.summary, start=event.start, end=event.end,
            description=event.description, location=event.location,
            all_day="true" if event.all_day else "", calendar_id=event.calendar_id,
        )
    }


@app.patch(
    "/calendar/events/{event_id}",
    operation_id="update_calendar_event",
    summary="Change an existing calendar event",
    description=(
        "Modify an event. Only the fields you provide are changed. Get the "
        "event_id from list_calendar_events first."
    ),
    dependencies=[Guarded],
    tags=["Calendar"],
)
async def update_calendar_event(
    event_id: Annotated[str, Path(description="Id of the event to change.")],
    patch: EventPatch,
) -> dict[str, Any]:
    return {
        "result": await cal().update_event(
            event_id=event_id, summary=patch.summary, start=patch.start,
            end=patch.end, description=patch.description, location=patch.location,
            calendar_id=patch.calendar_id,
        )
    }


@app.delete(
    "/calendar/events/{event_id}",
    operation_id="delete_calendar_event",
    summary="Delete an event from the user's calendar",
    description=(
        "Permanently remove an event. Always confirm with the user before "
        "calling this."
    ),
    dependencies=[Guarded],
    tags=["Calendar"],
)
async def delete_calendar_event(
    event_id: Annotated[str, Path(description="Id of the event to delete.")],
    calendar_id: Annotated[str, Query(
        description="Calendar id. Defaults to the primary calendar.",
    )] = "",
) -> dict[str, Any]:
    return {"result": await cal().delete_event(event_id=event_id, calendar_id=calendar_id)}


@app.get(
    "/calendar/calendars",
    operation_id="list_calendars",
    summary="List the calendars this account can see",
    description="Return every calendar available, with its id and access level.",
    dependencies=[Guarded],
    tags=["Calendar"],
)
async def list_calendars() -> dict[str, Any]:
    return {"result": await cal().list_calendars()}


# ── Google Tasks ─────────────────────────────────────────────────────────
#
# Same OAuth grant as the calendar. A refresh token minted before the tasks
# scope was added returns 403 here while the calendar keeps working.


class NewTask(BaseModel):
    title: str = Field(
        description="What needs doing, phrased as the user would say it.",
        examples=["Renew the car insurance"],
    )
    due: str = Field(
        default="",
        description="Due DATE as ISO 8601, for example '2026-09-04'. Google "
                    "Tasks stores no due time, so any time you send is "
                    "discarded — do not promise the user a reminder at an "
                    "hour. Resolve 'next Friday' yourself before calling.",
        examples=["2026-09-04"],
    )
    notes: str = Field(default="", description="Longer notes attached to the task.")
    list_id: str = Field(
        default="", description="Task list id. Defaults to the user's default list."
    )


@app.get(
    "/tasks",
    operation_id="list_tasks",
    summary="Look up the user's to-do list",
    description=(
        "Read outstanding tasks. Use this for anything about what the user has "
        "to do, what is due, or what is outstanding. Returns each task's id, "
        "title, due date and status. Completed tasks are excluded unless asked "
        "for. Note that due dates have no time of day."
    ),
    dependencies=[Guarded],
    tags=["Tasks"],
)
async def list_tasks(
    due_within_days: Annotated[int, Query(
        ge=0, le=365,
        description="Only return tasks due within this many days. 0 means no "
                    "due-date filter, which also includes tasks with no due date.",
    )] = 0,
    include_completed: Annotated[bool, Query(
        description="Include tasks already ticked off. Normally false.",
    )] = False,
    list_id: Annotated[str, Query(
        description="Task list id. Defaults to the user's default list.",
    )] = "",
    max_results: Annotated[int, Query(
        ge=1, le=100, description="Maximum tasks to return.",
    )] = 50,
) -> dict[str, Any]:
    return {"result": await tasks().list_tasks(
        due_within_days=due_within_days,
        include_completed=include_completed,
        list_id=list_id,
        max_results=max_results,
    )}


@app.post(
    "/tasks",
    operation_id="add_task",
    summary="Add something to the user's to-do list",
    description=(
        "Create a task. Use this when the user says they need to do something, "
        "or asks you to remember an errand. Prefer this over creating a "
        "calendar event when there is no particular time involved."
    ),
    dependencies=[Guarded],
    tags=["Tasks"],
)
async def add_task(body: NewTask) -> dict[str, Any]:
    return {"result": await tasks().add_task(
        title=body.title, due=body.due, notes=body.notes, list_id=body.list_id
    )}


@app.post(
    "/tasks/{task_id}/complete",
    operation_id="complete_task",
    summary="Tick a task off the user's to-do list",
    description=(
        "Mark a task as done. Call list_tasks first to find the task id — "
        "never guess one. Confirm with the user if more than one task could "
        "plausibly be the one they meant."
    ),
    dependencies=[Guarded],
    tags=["Tasks"],
)
async def complete_task(
    task_id: Annotated[str, Path(description="Id of the task to mark done.")],
    list_id: Annotated[str, Query(
        description="Task list id. Defaults to the user's default list.",
    )] = "",
) -> dict[str, Any]:
    return {"result": await tasks().complete_task(task_id=task_id, list_id=list_id)}


@app.get(
    "/tasks/lists",
    operation_id="list_task_lists",
    summary="List the user's task lists",
    description=(
        "Return every task list with its id and name. Only needed when the "
        "user refers to a list other than their default one."
    ),
    dependencies=[Guarded],
    tags=["Tasks"],
)
async def list_task_lists() -> dict[str, Any]:
    return {"result": await tasks().list_task_lists()}
