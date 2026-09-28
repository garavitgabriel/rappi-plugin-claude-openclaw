"""Authentication and user profile services."""

from rappi.auth_refresh import expires_at_from
from rappi.client import RappiClient
from rappi.config import ConfigManager
from rappi.constants import Endpoints
from rappi.models.user import PrimeStatus, UserProfile


async def get_profile(client: RappiClient) -> UserProfile:
    """Fetch the authenticated user's profile."""
    data = await client.get(Endpoints.USER_PROFILE)
    return UserProfile(**data)


async def is_prime(client: RappiClient) -> PrimeStatus:
    """Check if the user has Rappi Prime."""
    data = await client.get(Endpoints.IS_PRIME)
    return PrimeStatus(**data)


def set_token(
    config_manager: ConfigManager,
    token: str,
    device_id: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> None:
    """Save a Bearer token (and optionally deviceId / refresh token) to the config.

    A new login replaces the whole token set: without a refresh token the old one
    is cleared (it belonged to the previous session), and the expiry is only
    recorded when the login response reported it.
    """
    updates: dict = {
        "token": token,
        "refresh_token": refresh_token,
        "token_expires_at": expires_at_from(expires_in) if expires_in else None,
    }
    if device_id:
        updates["device_id"] = device_id
    config_manager.update(**updates)
