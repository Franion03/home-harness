"""Google OAuth -- exchanges a stored refresh token for access tokens.

The refresh token comes from a one-time browser consent run via
scripts/google_oauth.py. A service account cannot reach a personal calendar,
which is why this flow is used instead.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("harness.google_auth")

TOKEN_URL = "https://oauth2.googleapis.com/token"
# Refresh a little early so a token never expires mid-request.
EXPIRY_SKEW_SECONDS = 60


class GoogleAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token = ""
        self._expires_at = 0.0
        self._client = httpx.AsyncClient(timeout=20.0, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._refresh_token)

    async def token(self) -> str:
        if not self.configured:
            raise RuntimeError(
                "Google Calendar is not configured -- set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN"
            )
        if self._access_token and time.time() < self._expires_at - EXPIRY_SKEW_SECONDS:
            return self._access_token

        resp = await self._client.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Google token refresh failed ({resp.status_code}): {resp.text[:200]}"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        log.info("refreshed Google access token")
        return self._access_token

    async def aclose(self) -> None:
        await self._client.aclose()
