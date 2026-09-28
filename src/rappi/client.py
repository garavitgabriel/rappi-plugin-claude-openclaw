"""Async HTTP client for the Rappi API with automatic header injection and error handling."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from rappi.auth_refresh import (
    RefreshFailedError,
    is_token_expired,
    needs_proactive_refresh,
    refresh_tokens,
)
from rappi.config import ConfigManager, RappiConfig
from rappi.constants import BASE_URL, build_headers

if TYPE_CHECKING:
    from rappi.memory.manager import MemoryManager

logger = logging.getLogger(__name__)

REAUTH_HINT = (
    'To re-authenticate, run in ~/Projects/"Rappi Claude Plugin": '
    "`uv run rappi auth login`, then `uv run rappi auth push-railway` "
    "to update the hosted server. See README § Auth runbook."
)
NO_TOKEN_MESSAGE = f"No token configured. {REAUTH_HINT}"
TOKEN_EXPIRED_MESSAGE = f"Token expired and could not be refreshed automatically. {REAUTH_HINT}"

# Everything a refresh attempt can reasonably fail with (never token-bearing messages).
_REFRESH_ERRORS = (RefreshFailedError, httpx.HTTPError, OSError, ValueError)


class TokenExpiredError(Exception):
    """Raised when the API returns 401, indicating the token has expired."""


class RappiAPIError(Exception):
    """Raised for non-2xx responses from the Rappi API."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class RappiClient:
    """Async HTTP client that injects auth headers and handles common errors."""

    def __init__(
        self,
        config: RappiConfig | None = None,
        config_manager: ConfigManager | None = None,
        memory: MemoryManager | None = None,
    ):
        self._config_manager = config_manager or ConfigManager()
        self._config = config or self._config_manager.load()
        self._http: httpx.AsyncClient | None = None
        self._memory = memory

    @property
    def config(self) -> RappiConfig:
        return self._config

    @property
    def config_manager(self) -> ConfigManager:
        return self._config_manager

    @property
    def memory(self) -> MemoryManager | None:
        return self._memory

    async def __aenter__(self) -> RappiClient:
        needs_token = not self._config.token and self._config.refresh_token
        if needs_token or needs_proactive_refresh(self._config):
            try:
                await self._refresh(failed_token=self._config.token)
            except _REFRESH_ERRORS as e:
                # Best effort: keep using the current token unless it is known to be dead.
                logger.warning("Proactive token refresh failed: %s", _safe_error(e))
                if not self._config.token or is_token_expired(self._config):
                    raise TokenExpiredError(TOKEN_EXPIRED_MESSAGE) from e
        if not self._config.token:
            raise TokenExpiredError(NO_TOKEN_MESSAGE)
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=build_headers(self._config.token, self._config.device_id),
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.aclose()

    async def _refresh(self, failed_token: str | None) -> None:
        """Refresh tokens and copy them onto the live config (other fields stay as-is)."""
        fresh = await refresh_tokens(self._config_manager, failed_token=failed_token)
        self._config.token = fresh.token
        self._config.refresh_token = fresh.refresh_token
        self._config.token_expires_at = fresh.token_expires_at
        self._config.seed_fingerprint = fresh.seed_fingerprint
        if self._http is not None:
            self._http.headers["authorization"] = f"Bearer {fresh.token}"

    async def _handle_unauthorized(
        self, response: httpx.Response, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """On 401: refresh once and retry, or raise an actionable TokenExpiredError."""
        if not self._config.refresh_token:
            raise TokenExpiredError(TOKEN_EXPIRED_MESSAGE)
        headers = getattr(response, "headers", None) or {}
        flagged = "x-refresh-token" in headers
        logger.info("401 from Rappi (x-refresh-token header present: %s); refreshing", flagged)
        try:
            await self._refresh(failed_token=self._config.token)
        except _REFRESH_ERRORS as e:
            logger.warning("Token refresh after 401 failed: %s", _safe_error(e))
            raise TokenExpiredError(TOKEN_EXPIRED_MESSAGE) from e
        retry = await self._http.request(method, path, **kwargs)
        if retry.status_code == 401:
            raise TokenExpiredError(TOKEN_EXPIRED_MESSAGE)
        return retry

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._http is not None, "Use 'async with RappiClient() as client:'"
        response = await self._http.request(method, path, **kwargs)
        if response.status_code == 401:
            response = await self._handle_unauthorized(response, method, path, **kwargs)
        if response.status_code >= 400:
            raise RappiAPIError(response.status_code, response.text[:500])
        if not response.content:
            return {}
        return response.json()

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)


def _safe_error(e: BaseException) -> str:
    """Describe a refresh failure without echoing anything that could hold a token."""
    if isinstance(e, RefreshFailedError):
        return str(e)
    return type(e).__name__
