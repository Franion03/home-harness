"""home-harness -- an LLM-agnostic assistant for the house.

FastAPI surface:
    GET  /                      the voice PWA
    GET  /health                liveness plus the active configuration
    POST /v1/chat               text in, text out
    POST /v1/voice              audio in, transcript + spoken answer out
    POST /v1/transcribe         audio in, text out
    POST /v1/speak              text in, audio out
    GET  /v1/sessions           recent sessions
    DEL  /v1/sessions/{id}      forget a conversation
    POST /v1/admin/reload       re-read models.yaml without a restart
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import Agent
from config import Settings
from gateway import Gateway
from google_auth import GoogleAuth
from llm import Router, build_registry
from memory import Memory
from provider_base import ProviderError
from speech import Speech
from tool_registry import ToolRegistry, obj, string
import tool_calendar
import tool_homeassistant

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("harness")

STATIC_DIR = Path(__file__).parent / "static"


class Services:
    """Everything built once at startup and shared by the request handlers."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.gateway = Gateway(
            account_id=self.settings.cf_account_id,
            gateway_name=self.settings.cf_gateway,
            mode=self.settings.gateway_mode,
            gateway_token=self.settings.cf_gateway_token,
            cache_ttl=self.settings.cache_ttl,
        )
        self.router = Router(self.settings, build_registry(self.settings, self.gateway))
        self.memory = Memory(self.settings.db_path)
        self.speech = Speech(self.settings, self.gateway)
        self.registry = ToolRegistry()
        self.ha: tool_homeassistant.HomeAssistant | None = None
        self.calendar: tool_calendar.Calendar | None = None
        self.google_auth: GoogleAuth | None = None
        self._wire_tools()
        self.agent = Agent(
            settings=self.settings,
            router=self.router,
            registry=self.registry,
            memory=self.memory,
        )

    def _wire_tools(self) -> None:
        s = self.settings
        if s.ha_url and s.ha_token:
            self.ha = tool_homeassistant.HomeAssistant(s.ha_url, s.ha_token)
            tool_homeassistant.register(self.registry, self.ha)
            log.info("Home Assistant tools enabled (%s)", s.ha_url)
        else:
            log.warning("Home Assistant tools disabled -- HA_URL/HA_TOKEN not set")

        self.google_auth = GoogleAuth(
            s.google_client_id, s.google_client_secret, s.google_refresh_token
        )
        if self.google_auth.configured:
            self.calendar = tool_calendar.Calendar(
                self.google_auth, s.calendar_id, s.timezone
            )
            tool_calendar.register(self.registry, self.calendar)
            log.info("Google Calendar tools enabled (calendar=%s)", s.calendar_id)
        else:
            log.warning(
                "Google Calendar tools disabled -- run scripts/google_oauth.py to "
                "get a refresh token"
            )

        self.registry.add(
            "remember_nothing",
            "Discard the current conversation history and start fresh. Use only "
            "when the user explicitly asks to forget or start over.",
            obj({"session_id": string("Session to clear.")}, ["session_id"]),
            self._forget,
        )

    async def _forget(self, session_id: str) -> str:
        removed = await self.memory.clear(session_id)
        return f"Cleared {removed} stored messages."

    async def aclose(self) -> None:
        await self.gateway.aclose()
        if self.ha:
            await self.ha.aclose()
        if self.calendar:
            await self.calendar.aclose()
        if self.google_auth:
            await self.google_auth.aclose()


services: Services | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global services
    services = Services()
    log.info("harness ready: %s", services.settings.describe())
    if not services.settings.api_key:
        log.warning(
            "HARNESS_API_KEY is unset -- the API is unauthenticated. Only safe "
            "behind Cloudflare Access or on a trusted LAN."
        )
    try:
        yield
    finally:
        await services.aclose()


app = FastAPI(title="home-harness", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def svc() -> Services:
    if services is None:  # pragma: no cover - only before lifespan runs
        raise HTTPException(503, "starting up")
    return services


async def require_key(request: Request) -> None:
    """Shared-secret check. A no-op when HARNESS_API_KEY is unset."""
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


# ---- models ------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str = ""
    route: str = "chat"
    speak: bool = False


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1)


# ---- routes ------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    s = svc()
    return {
        "status": "ok",
        "service": "home-harness",
        "tools": s.registry.names(),
        **s.settings.describe(),
    }


@app.post("/v1/chat", dependencies=[Guarded])
async def chat(req: ChatRequest) -> JSONResponse:
    s = svc()
    session_id = req.session_id or uuid.uuid4().hex[:12]
    try:
        result = await s.agent.run(req.message, session_id=session_id, route_name=req.route)
    except ProviderError as exc:
        raise HTTPException(502, f"model call failed: {exc}") from exc

    payload: dict[str, Any] = {
        "text": result.text,
        "session_id": result.session_id,
        "model": result.model,
        "tool_calls": result.tool_calls,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
        "elapsed_ms": result.elapsed_ms,
    }
    if req.speak and result.text:
        audio, mime = await s.speech.synthesize(result.text)
        payload["audio"] = base64.b64encode(audio).decode()
        payload["audio_mime"] = mime
    return JSONResponse(payload)


@app.post("/v1/voice", dependencies=[Guarded])
async def voice(
    audio: UploadFile = File(...),
    session_id: str = Form(""),
    route: str = Form("voice"),
    speak: str = Form("true"),
) -> JSONResponse:
    """The phone/laptop path: record, upload, get a spoken answer back."""
    s = svc()
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "empty audio upload")

    try:
        transcript = await s.speech.transcribe(raw, filename=audio.filename or "audio.webm")
    except ProviderError as exc:
        raise HTTPException(502, f"transcription failed: {exc}") from exc

    sid = session_id or uuid.uuid4().hex[:12]
    try:
        result = await s.agent.run(transcript, session_id=sid, route_name=route)
    except ProviderError as exc:
        raise HTTPException(502, f"model call failed: {exc}") from exc

    payload: dict[str, Any] = {
        "transcript": transcript,
        "text": result.text,
        "session_id": result.session_id,
        "model": result.model,
        "tool_calls": result.tool_calls,
        "elapsed_ms": result.elapsed_ms,
    }
    if speak.lower() in ("true", "1", "yes") and result.text:
        try:
            data, mime = await s.speech.synthesize(result.text)
            payload["audio"] = base64.b64encode(data).decode()
            payload["audio_mime"] = mime
        except ProviderError as exc:
            # A missing voice should not cost you the answer.
            log.warning("TTS failed, returning text only: %s", exc)
            payload["tts_error"] = str(exc)
    return JSONResponse(payload)


@app.post("/v1/transcribe", dependencies=[Guarded])
async def transcribe(audio: UploadFile = File(...)) -> dict[str, str]:
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "empty audio upload")
    text = await svc().speech.transcribe(raw, filename=audio.filename or "audio.webm")
    return {"text": text}


@app.post("/v1/speak", dependencies=[Guarded])
async def speak(req: SpeakRequest) -> Response:
    data, mime = await svc().speech.synthesize(req.text)
    return Response(content=data, media_type=mime)


@app.get("/v1/sessions", dependencies=[Guarded])
async def sessions() -> list[dict[str, Any]]:
    return await svc().memory.sessions()


@app.delete("/v1/sessions/{session_id}", dependencies=[Guarded])
async def forget(session_id: str) -> dict[str, Any]:
    removed = await svc().memory.clear(session_id)
    return {"session_id": session_id, "removed": removed}


@app.post("/v1/admin/reload", dependencies=[Guarded])
async def reload_config() -> dict[str, Any]:
    """Pick up a models.yaml edit without restarting the pod."""
    s = svc()
    s.settings.reload_routes()
    log.info("reloaded routes: %s", s.settings.describe())
    return {"reloaded": True, **s.settings.describe()}


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:  # pragma: no cover
    log.warning("static directory %s missing -- PWA not served", STATIC_DIR)
