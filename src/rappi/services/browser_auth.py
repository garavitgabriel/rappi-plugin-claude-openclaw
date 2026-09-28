"""Browser-based authentication — opens Rappi login and intercepts the Bearer token."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from rappi.constants import RAPPI_DOMAIN, USER_AGENT


LOGIN_URL = f"https://{RAPPI_DOMAIN}/login"
AUTH_ENDPOINT = "/ms/application-user/auth"
LOGIN_RESPONSE_PREFIX = "/api/rocket/login/"  # .../{email|whatsapp|twilio|google|...}/application_user
TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes
LOGIN_TOKENS_GRACE_S = 3.0  # wait this long for the login response after the auth call lands


@dataclass
class CapturedCredentials:
    token: str
    device_id: str
    user_name: str | None = None
    email: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None


@dataclass
class LoginTokens:
    """Token set from a ``/api/rocket/login/.../application_user`` response."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None


def parse_login_response(url: str, status: int, body: Any) -> LoginTokens | None:
    """Extract tokens from a Rappi login response, or None if it isn't one.

    Pure function (no Playwright) so the capture logic is unit-testable.
    """
    if status != 200:
        return None
    path = urlparse(url).path.rstrip("/")
    if LOGIN_RESPONSE_PREFIX not in path or not path.endswith("application_user"):
        return None
    if not isinstance(body, dict) or not body.get("access_token"):
        return None
    expires_in = body.get("expires_in")
    try:
        expires_in = int(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        expires_in = None
    return LoginTokens(
        access_token=str(body["access_token"]),
        refresh_token=body.get("refresh_token") or None,
        expires_in=expires_in,
    )


async def login_with_browser(
    headless: bool = False,
    on_status: callable | None = None,
    channel: str | None = "chrome",
) -> CapturedCredentials:
    """Launch a browser for the user to log in, intercept the auth token.

    Args:
        headless: If True, run in headless mode (mainly for testing).
        channel: Playwright browser channel. Defaults to the installed Google
            Chrome with its own user agent — Rappi's login rejects Playwright's
            bundled "Chrome for Testing" plus a spoofed UA ("Algo ha salido mal").
            None uses the bundled build with the legacy mobile UA.
        on_status: Optional callback(message: str) for progress updates.

    Returns:
        CapturedCredentials with token, device_id and (when Rappi returned one)
        the refresh token and its access-token lifetime.

    Raises:
        TimeoutError: If the user doesn't complete login within 5 minutes.
        RuntimeError: If Playwright browsers aren't installed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is required for browser login. "
            "Run: uv run playwright install chromium"
        )

    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    result: CapturedCredentials | None = None
    login_tokens: LoginTokens | None = None
    done_event = asyncio.Event()
    login_event = asyncio.Event()

    async with async_playwright() as pw:
        _status("Launching browser...")
        browser = await pw.chromium.launch(
            headless=headless,
            channel=channel,
            # Hide navigator.webdriver — Rappi's anti-bot check trips on it
            args=["--disable-blink-features=AutomationControlled"],
        )
        context_opts: dict[str, Any] = {"viewport": {"width": 420, "height": 800}}
        if channel is None:
            context_opts["user_agent"] = USER_AGENT
        context = await browser.new_context(**context_opts)
        page = await context.new_page()

        async def _on_response(response):
            nonlocal result, login_tokens
            # The login response carries the refresh token + expiry
            if LOGIN_RESPONSE_PREFIX in response.url and response.status == 200:
                try:
                    body = await response.json()
                except Exception:
                    return
                parsed = parse_login_response(response.url, response.status, body)
                if parsed:
                    login_tokens = parsed
                    login_event.set()
                return

            # We're looking for a successful GET to the auth endpoint
            if AUTH_ENDPOINT not in response.url:
                return
            if response.status != 200:
                return

            try:
                body = await response.json()
            except Exception:
                return

            # Verify it's a real auth response (has user id and email)
            if not body.get("id") or not body.get("email"):
                return

            # Extract token from the request headers
            request = response.request
            auth_header = request.headers.get("authorization", "")
            device_id = request.headers.get("deviceid", "")

            if not auth_header.startswith("Bearer ft."):
                return

            token = auth_header.removeprefix("Bearer ").strip()
            result = CapturedCredentials(
                token=token,
                device_id=device_id,
                user_name=body.get("name"),
                email=body.get("email"),
            )
            _status(f"Token captured for {result.user_name or result.email}!")
            done_event.set()

        page.on("response", _on_response)

        _status("Opening Rappi login page...")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        _status("Please log in with your phone number and OTP...")

        # Wait for the token to be captured or timeout
        try:
            await asyncio.wait_for(done_event.wait(), timeout=TIMEOUT_MS / 1000)
        except asyncio.TimeoutError:
            await browser.close()
            raise TimeoutError(
                "Login timed out after 5 minutes. Please try again."
            )

        # The login response normally precedes the auth call; allow a short grace
        # period in case its JSON body is still being read.
        if login_tokens is None:
            try:
                await asyncio.wait_for(login_event.wait(), timeout=LOGIN_TOKENS_GRACE_S)
            except TimeoutError:
                _status("No refresh token captured — auto-refresh will be unavailable for this login.")

        await browser.close()

    if result is None:
        raise RuntimeError("Failed to capture authentication token.")

    if login_tokens is not None:
        # The Bearer header on the auth call is authoritative for the access token.
        if login_tokens.access_token != result.token:
            _status("Note: login response token differs from the auth header; using the auth header.")
        result.refresh_token = login_tokens.refresh_token
        result.expires_in = login_tokens.expires_in

    return result
