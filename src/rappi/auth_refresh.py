"""Rappi access-token refresh.

Rappi access tokens live 7 days (``expires_in: 604800``). Login responses also
return a ``refresh_token``; posting it to ``/api/rocket/refresh-token`` yields a
fresh access token. This module performs that exchange safely across concurrent
callers (asyncio lock per event loop + ``fcntl`` file lock per config dir) and
persists the result.

Nothing here may log or raise with token material in the message.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import weakref
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from rappi import constants
from rappi.constants import Endpoints

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from rappi.config import ConfigManager, RappiConfig

logger = logging.getLogger(__name__)

REFRESH_PATH = Endpoints.REFRESH_TOKEN
DEFAULT_EXPIRES_IN = 604800  # 7 days, as returned by every Rappi login response
PROACTIVE_MARGIN = timedelta(hours=48)
LOCK_FILE_NAME = ".refresh.lock"

# One asyncio.Lock per running event loop (the CLI calls asyncio.run repeatedly).
_process_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


class RefreshFailedError(Exception):
    """The refresh-token exchange failed. Carries only the status and Rappi's error code."""

    def __init__(self, status_code: int | None, error_code: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Token refresh failed (HTTP {status_code}, code={error_code or 'unknown'})")


def expires_at_from(expires_in: int | None, now: datetime | None = None) -> str:
    """ISO-8601 UTC timestamp ``now + expires_in`` seconds (default 7 days)."""
    now = now or datetime.now(UTC)
    try:
        seconds = int(expires_in) if expires_in else DEFAULT_EXPIRES_IN
    except (TypeError, ValueError):
        seconds = DEFAULT_EXPIRES_IN
    if seconds <= 0:
        seconds = DEFAULT_EXPIRES_IN
    return (now + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def parse_expires_at(value: str | None) -> datetime | None:
    """Parse a stored ``token_expires_at``; None if missing or malformed."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def needs_proactive_refresh(
    config: RappiConfig, margin: timedelta = PROACTIVE_MARGIN, now: datetime | None = None
) -> bool:
    """True when a refresh token exists and expiry is unknown or within ``margin``."""
    if not config.refresh_token:
        return False
    expires_at = parse_expires_at(config.token_expires_at)
    if expires_at is None:
        return True
    return expires_at - (now or datetime.now(UTC)) <= margin


def is_token_expired(config: RappiConfig, now: datetime | None = None) -> bool:
    """True only when the expiry is known and already past."""
    expires_at = parse_expires_at(config.token_expires_at)
    return expires_at is not None and expires_at <= (now or datetime.now(UTC))


def _process_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _process_locks.get(loop)
    if lock is None:
        lock = _process_locks[loop] = asyncio.Lock()
    return lock


@contextlib.asynccontextmanager
async def _file_lock(config_dir: Path) -> AsyncIterator[None]:
    """Exclusive ``flock`` on ``<config_dir>/.refresh.lock`` (shared by CLI and server)."""
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    fd = await asyncio.to_thread(os.open, config_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _refresh_headers(device_id: str) -> dict[str, str]:
    """Standard non-auth headers (no ``authorization``) + JSON content type."""
    headers = constants.build_headers("", device_id)
    headers.pop("authorization", None)
    headers["content-type"] = "application/json"
    return headers


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code) if code else None


async def _post_refresh(refresh_token: str, device_id: str) -> dict:
    async with httpx.AsyncClient(base_url=constants.BASE_URL, timeout=30.0) as http:
        response = await http.post(
            REFRESH_PATH,
            json={"refresh_token": refresh_token},
            headers=_refresh_headers(device_id),
        )
    if not response.is_success:
        raise RefreshFailedError(response.status_code, _error_code(response))
    try:
        body = response.json()
    except ValueError:
        raise RefreshFailedError(response.status_code, "invalid_json") from None
    if not isinstance(body, dict) or not body.get("access_token"):
        raise RefreshFailedError(response.status_code, "missing_access_token")
    return body


async def refresh_tokens(config_manager: ConfigManager, failed_token: str | None = None) -> RappiConfig:
    """Exchange the saved refresh token for a new access token and persist it.

    Args:
        config_manager: Where the tokens live (reloaded under the lock).
        failed_token: The access token the caller saw failing (or about to expire).
            If the persisted token already differs, another caller refreshed first
            and the fresh config is returned without calling the API. ``None``
            forces a refresh.

    Raises:
        RefreshFailedError: no refresh token, or Rappi rejected the exchange.
    """
    async with _process_lock(), _file_lock(config_manager.config_dir):
        config = config_manager.load()
        if failed_token is not None and config.token and config.token != failed_token:
            logger.info("Token already refreshed by another caller; reusing it")
            return config
        if not config.refresh_token:
            raise RefreshFailedError(None, "no_refresh_token")

        body = await _post_refresh(config.refresh_token, config.device_id)
        config.token = body["access_token"]
        config.refresh_token = body.get("refresh_token") or config.refresh_token
        config.token_expires_at = expires_at_from(body.get("expires_in"))
        config_manager.save(config)
        logger.info(
            "Rappi token refreshed, expires %s (refresh token %s)",
            config.token_expires_at,
            "rotated" if body.get("refresh_token") else "kept",
        )
        return config
