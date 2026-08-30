"""HTTP transport for every upstream vendor call.

All traffic is routed through a Cloudflare AI Gateway:

    https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway}/{provider}/{path}

which gives caching, cost analytics, rate limiting and a per-request log for
free, without any vendor lock-in -- the body we send is still the vendor's own
native format. Setting AI_GATEWAY_MODE=direct bypasses the gateway and talks to
each vendor's own base URL, which is the escape hatch if the gateway is ever
down or you want to A/B a request against it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from provider_base import ProviderError

log = logging.getLogger("harness.gateway")

GATEWAY_ROOT = "https://gateway.ai.cloudflare.com/v1"

# Vendor base URLs used when AI_GATEWAY_MODE=direct.
DIRECT_BASES: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "google-ai-studio": "https://generativelanguage.googleapis.com",
    "openrouter": "https://openrouter.ai/api",
    "groq": "https://api.groq.com/openai",
    "mistral": "https://api.mistral.ai",
    "deepseek": "https://api.deepseek.com",
    "elevenlabs": "https://api.elevenlabs.io",
    "deepgram": "https://api.deepgram.com",
}


class Gateway:
    def __init__(
        self,
        *,
        account_id: str,
        gateway_name: str,
        mode: str = "gateway",
        gateway_token: str = "",
        cache_ttl: int = 0,
        timeout: float = 120.0,
    ):
        self.account_id = account_id
        self.gateway_name = gateway_name
        self.mode = mode if account_id and gateway_name else "direct"
        self.gateway_token = gateway_token
        self.cache_ttl = cache_ttl
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    def url(self, provider: str, path: str) -> str:
        path = path.lstrip("/")
        if self.mode == "direct":
            base = DIRECT_BASES.get(provider)
            if not base:
                raise ProviderError(
                    f"no direct base URL known for provider '{provider}'; "
                    "set AI_GATEWAY_MODE=gateway",
                    retryable=False,
                )
            return f"{base}/{path}"
        return f"{GATEWAY_ROOT}/{self.account_id}/{self.gateway_name}/{provider}/{path}"

    def _gateway_headers(self, *, cache: bool) -> dict[str, str]:
        if self.mode == "direct":
            return {}
        h: dict[str, str] = {}
        if self.gateway_token:
            h["cf-aig-authorization"] = f"Bearer {self.gateway_token}"
        if cache and self.cache_ttl > 0:
            h["cf-aig-cache-ttl"] = str(self.cache_ttl)
        elif not cache:
            h["cf-aig-skip-cache"] = "true"
        return h

    async def post_json(
        self,
        provider: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        cache: bool = True,
    ) -> dict[str, Any]:
        resp = await self._request(
            provider, path, headers=headers, cache=cache, json=json
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"{provider}: response was not JSON: {resp.text[:200]}"
            ) from exc

    async def post_multipart(
        self,
        provider: str,
        path: str,
        *,
        headers: dict[str, str],
        files: dict[str, Any],
        data: dict[str, Any] | None = None,
        cache: bool = False,
    ) -> dict[str, Any]:
        resp = await self._request(
            provider, path, headers=headers, cache=cache, files=files, data=data or {}
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"{provider}: response was not JSON: {resp.text[:200]}"
            ) from exc

    async def post_raw(
        self,
        provider: str,
        path: str,
        *,
        headers: dict[str, str],
        content: bytes,
        params: dict[str, Any] | None = None,
        cache: bool = False,
    ) -> dict[str, Any]:
        """For vendors that take the payload as the raw request body (Deepgram)."""
        resp = await self._request(
            provider, path, headers=headers, cache=cache, content=content, params=params or {}
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"{provider}: response was not JSON: {resp.text[:200]}"
            ) from exc

    async def post_bytes(
        self,
        provider: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        cache: bool = True,
    ) -> bytes:
        """For endpoints that return audio rather than JSON (TTS)."""
        resp = await self._request(
            provider, path, headers=headers, cache=cache, json=json
        )
        return resp.content

    async def _request(
        self,
        provider: str,
        path: str,
        *,
        headers: dict[str, str],
        cache: bool,
        **kw: Any,
    ) -> httpx.Response:
        url = self.url(provider, path)
        merged = {**headers, **self._gateway_headers(cache=cache)}
        try:
            resp = await self._client.post(url, headers=merged, **kw)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{provider}: transport error: {exc}") from exc

        if resp.status_code >= 400:
            body = resp.text[:400]
            # 4xx other than 408/429 means we sent something wrong -- trying the
            # same request against a fallback model will fail the same way.
            retryable = resp.status_code in (408, 429) or resp.status_code >= 500
            log.warning(
                "%s %s -> %s %s", provider, path, resp.status_code, body
            )
            raise ProviderError(
                f"{provider} returned {resp.status_code}: {body}",
                status=resp.status_code,
                retryable=retryable,
            )
        return resp

    async def aclose(self) -> None:
        await self._client.aclose()
