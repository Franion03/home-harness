"""Speech in and out, routed through the same gateway as the LLM calls.

Both directions follow the model pattern: a `provider/model` reference and a
fallback, read from models.yaml. Swapping Whisper for Deepgram, or OpenAI TTS
for ElevenLabs, is a ConfigMap edit.
"""

from __future__ import annotations

import logging
from typing import Any

from config import Settings
from gateway import Gateway
from llm import split_ref
from provider_base import ProviderError

log = logging.getLogger("harness.speech")

# Content types we can hand upstream, keyed by the extension browsers produce.
AUDIO_TYPES = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "flac": "audio/flac",
}


class Speech:
    def __init__(self, settings: Settings, gateway: Gateway):
        self._s = settings
        self._gw = gateway

    def _key_for(self, provider: str) -> str:
        keys = {
            "openai": self._s.openai_key,
            "groq": self._s.groq_key,
            "deepgram": self._s.deepgram_key,
            "elevenlabs": self._s.elevenlabs_key,
        }
        return keys.get(provider, "")

    # ---- speech to text -------------------------------------------------

    async def transcribe(self, audio: bytes, *, filename: str = "audio.webm") -> str:
        cfg = self._s.speech("stt")
        chain = [r for r in (cfg.get("primary"), cfg.get("fallback")) if r]
        if not chain:
            raise ProviderError("no STT model configured", retryable=False)

        last: Exception | None = None
        for ref in chain:
            provider, model = split_ref(ref)
            key = self._key_for(provider)
            if not key:
                last = ProviderError(f"no API key for STT provider '{provider}'", retryable=False)
                continue
            try:
                if provider == "deepgram":
                    return await self._deepgram(audio, model, key, filename, cfg)
                return await self._openai_stt(audio, model, key, filename, provider, cfg)
            except Exception as exc:  # noqa: BLE001 - try the fallback
                last = exc
                log.warning("STT via %s failed: %s", ref, exc)
        raise last or ProviderError("transcription failed")

    async def _openai_stt(
        self, audio: bytes, model: str, key: str, filename: str,
        provider: str, cfg: dict[str, Any],
    ) -> str:
        data: dict[str, Any] = {"model": model}
        if cfg.get("language"):
            data["language"] = cfg["language"]
        result = await self._gw.post_multipart(
            provider,
            "v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio, _content_type(filename))},
            data=data,
        )
        text = (result.get("text") or "").strip()
        if not text:
            raise ProviderError(f"{provider}: transcription came back empty")
        return text

    async def _deepgram(
        self, audio: bytes, model: str, key: str, filename: str, cfg: dict[str, Any]
    ) -> str:
        params: dict[str, Any] = {"model": model, "smart_format": "true"}
        if cfg.get("language"):
            params["language"] = cfg["language"]
        result = await self._gw.post_raw(
            "deepgram",
            "v1/listen",
            headers={"Authorization": f"Token {key}", "Content-Type": _content_type(filename)},
            content=audio,
            params=params,
        )
        try:
            alt = result["results"]["channels"][0]["alternatives"][0]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"deepgram: unexpected response shape ({exc})") from exc
        text = (alt.get("transcript") or "").strip()
        if not text:
            raise ProviderError("deepgram: transcription came back empty")
        return text

    # ---- text to speech -------------------------------------------------

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """Returns (audio_bytes, mime_type)."""
        cfg = self._s.speech("tts")
        chain = [r for r in (cfg.get("primary"), cfg.get("fallback")) if r]
        if not chain:
            raise ProviderError("no TTS model configured", retryable=False)

        fmt = cfg.get("format", "mp3")
        last: Exception | None = None
        for ref in chain:
            provider, model = split_ref(ref)
            key = self._key_for(provider)
            if not key:
                last = ProviderError(f"no API key for TTS provider '{provider}'", retryable=False)
                continue
            try:
                if provider == "elevenlabs":
                    return await self._elevenlabs(text, model, key, cfg), "audio/mpeg"
                audio = await self._openai_tts(text, model, key, provider, cfg, fmt)
                return audio, AUDIO_TYPES.get(fmt, "audio/mpeg")
            except Exception as exc:  # noqa: BLE001 - try the fallback
                last = exc
                log.warning("TTS via %s failed: %s", ref, exc)
        raise last or ProviderError("speech synthesis failed")

    async def _openai_tts(
        self, text: str, model: str, key: str, provider: str,
        cfg: dict[str, Any], fmt: str,
    ) -> bytes:
        return await self._gw.post_bytes(
            provider,
            "v1/audio/speech",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "input": text,
                "voice": cfg.get("voice", "alloy"),
                "response_format": fmt,
            },
        )

    async def _elevenlabs(
        self, text: str, model: str, key: str, cfg: dict[str, Any]
    ) -> bytes:
        voice_id = cfg.get("voice")
        if not voice_id:
            raise ProviderError(
                "elevenlabs TTS needs `voice` set to a voice id in models.yaml",
                retryable=False,
            )
        return await self._gw.post_bytes(
            "elevenlabs",
            f"v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model},
        )


def _content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    return AUDIO_TYPES.get(ext, "application/octet-stream")
