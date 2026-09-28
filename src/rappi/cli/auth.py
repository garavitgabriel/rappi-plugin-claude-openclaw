"""CLI commands for authentication."""

import asyncio
import subprocess
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table

from rappi.auth_refresh import RefreshFailedError, refresh_tokens
from rappi.client import TOKEN_EXPIRED_MESSAGE, RappiClient, TokenExpiredError
from rappi.config import ConfigManager
from rappi.services.auth import get_profile, is_prime, set_token

app = typer.Typer()
console = Console()

# src/rappi/cli/auth.py -> repo root (where the Railway project is linked)
REPO_DIR = Path(__file__).resolve().parents[3]


@app.command()
def login(
    token: str | None = typer.Option(None, "--token", "-t", help="Manual Bearer token (skips browser login)"),
    refresh_token: str | None = typer.Option(
        None, "--refresh-token", help="Refresh token to pair with --token (enables auto-refresh)"
    ),
    device_id: str | None = typer.Option(None, "--device-id", "-d", help="Device ID (UUID). Auto-generated if omitted."),
    country: str = typer.Option("co", "--country", "-c", help="Country code: co (Colombia), mx (Mexico)"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    browser: str = typer.Option(
        "chrome", "--browser", help="Login browser: chrome (installed Google Chrome) or bundled (Playwright's Chrome for Testing)"
    ),
) -> None:
    """Log in to Rappi. Opens a browser for you to sign in (or pass --token for manual auth)."""
    import os
    os.environ["RAPPI_COUNTRY"] = country
    # Re-import to pick up the new country
    import importlib
    import rappi.constants
    importlib.reload(rappi.constants)

    config_manager = ConfigManager()
    config_manager.update(country=country)

    if token:
        # Manual token flow
        if not token.startswith("ft."):
            console.print("[yellow]Warning:[/yellow] Token doesn't start with 'ft.' — this may not work.")
        set_token(config_manager, token, device_id, refresh_token=refresh_token)
    else:
        # Browser login flow
        from rappi.services.browser_auth import login_with_browser

        def on_status(msg: str) -> None:
            console.print(f"  [dim]{msg}[/dim]")

        from rappi.constants import RAPPI_DOMAIN
        console.print(Panel(
            "[bold]Browser Login[/bold]\n\n"
            f"A browser window will open to [cyan]{RAPPI_DOMAIN}/login[/cyan]\n"
            "Log in with your phone number and OTP.\n"
            "The token will be captured automatically.\n\n"
            "[dim]Timeout: 5 minutes[/dim]",
            title="Rappi Auth",
        ))

        try:
            channel = None if browser == "bundled" else browser
            creds = asyncio.run(login_with_browser(headless=headless, on_status=on_status, channel=channel))
        except TimeoutError:
            console.print("[red]Login timed out. Please try again.[/red]")
            raise typer.Exit(1)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            console.print("\nTo install browser support: [bold]uv run playwright install chromium[/bold]")
            console.print("Or use manual token: [bold]rappi auth login --token <your-token>[/bold]")
            raise typer.Exit(1)

        set_token(
            config_manager,
            creds.token,
            creds.device_id,
            refresh_token=creds.refresh_token,
            expires_in=creds.expires_in,
        )

    # Verify the token works
    async def _verify():
        async with RappiClient(config_manager=config_manager) as client:
            return await get_profile(client)

    try:
        profile = asyncio.run(_verify())
        console.print(Panel(
            f"[green]Logged in as [bold]{profile.name}[/bold][/green]\n"
            f"Email: {profile.email}\n"
            f"Phone: {profile.country_code} {profile.phone}\n"
            f"Auto-refresh: {'yes' if config_manager.load().refresh_token else 'no'}",
            title="Authentication Successful",
        ))
    except TokenExpiredError:
        console.print("[red]Token appears to be invalid or expired.[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[yellow]Token saved but verification failed:[/yellow] {e}")
        console.print("The token may still work — try [bold]rappi auth status[/bold].")


@app.command()
def status() -> None:
    """Check your authentication status and show profile info."""
    config_manager = ConfigManager()
    config = config_manager.load()

    if not config.token and not config.refresh_token:
        console.print("[red]Not authenticated.[/red] Run: [bold]rappi auth login[/bold]")
        raise typer.Exit(1)

    async def _fetch():
        async with RappiClient(config=config, config_manager=config_manager) as client:
            profile = await get_profile(client)
            prime = await is_prime(client)
            return profile, prime

    try:
        profile, prime = asyncio.run(_fetch())
    except TokenExpiredError:
        console.print(f"[red]{TOKEN_EXPIRED_MESSAGE}[/red]")
        raise typer.Exit(1)

    table = Table(title="Rappi Profile", show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Name", profile.name or "—")
    table.add_row("Email", profile.email or "—")
    table.add_row("Phone", f"{profile.country_code or ''} {profile.phone or '—'}")
    table.add_row("ID", str(profile.id))
    table.add_row("VIP", "Yes" if profile.vip else "No")
    if profile.loyalty:
        table.add_row("Loyalty", f"{profile.loyalty.name} ({profile.loyalty.type})")
    table.add_row("Prime", "[green]Yes[/green]" if prime.is_prime else "No")
    table.add_row("Expires", config.token_expires_at or "unknown")
    table.add_row("Auto-refresh", "yes" if config.refresh_token else "no")

    console.print(table)


@app.command()
def token(
    bearer_token: str = typer.Argument(..., help="Rappi Bearer token (starts with 'ft.')"),
    device_id: str = typer.Argument(..., help="Device ID (UUID from browser DevTools)"),
    country: str = typer.Option("co", "--country", "-c", help="Country code: co (Colombia), mx (Mexico)"),
) -> None:
    """Set auth credentials directly — no browser needed. Ideal for headless servers.

    Get your token and device ID from ~/.rappi/config.json on a machine where you've
    already logged in, or capture them from Rappi's website via browser DevTools.
    """
    import os
    os.environ["RAPPI_COUNTRY"] = country
    import importlib
    import rappi.constants
    importlib.reload(rappi.constants)

    if not bearer_token.startswith("ft."):
        console.print("[yellow]Warning:[/yellow] Token doesn't start with 'ft.' — this may not work.")

    config_manager = ConfigManager()
    # A manually pasted token has no known refresh token or expiry.
    config_manager.update(
        token=bearer_token, device_id=device_id, country=country, refresh_token=None, token_expires_at=None
    )

    # Verify
    async def _verify():
        async with RappiClient(config_manager=config_manager) as client:
            return await get_profile(client)

    try:
        profile = asyncio.run(_verify())
        console.print(Panel(
            f"[green]Logged in as [bold]{profile.name}[/bold][/green]\n"
            f"Email: {profile.email}\n"
            f"Country: {country.upper()}",
            title="Token Saved",
        ))
    except TokenExpiredError:
        console.print("[red]Token appears to be invalid or expired.[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[yellow]Token saved but verification failed:[/yellow] {e}")
        console.print("The token may still work — try [bold]rappi auth status[/bold].")


@app.command()
def logout() -> None:
    """Clear saved authentication token."""
    config_manager = ConfigManager()
    config_manager.update(token=None, refresh_token=None, token_expires_at=None)
    console.print("[green]Token cleared.[/green]")


@app.command()
def refresh() -> None:
    """Force one token refresh. Prints only 'ok, expires <iso>' or the error code."""
    config_manager = ConfigManager()
    try:
        config = asyncio.run(refresh_tokens(config_manager))
    except RefreshFailedError as e:
        console.print(f"refresh failed: {e.error_code or 'unknown'} (HTTP {e.status_code})", markup=False)
        raise typer.Exit(1)
    except httpx.HTTPError as e:
        console.print(f"refresh failed: {type(e).__name__}", markup=False)
        raise typer.Exit(1)
    console.print(f"ok, expires {config.token_expires_at}", markup=False)


@app.command("push-railway")
def push_railway() -> None:
    """Push the saved token set to the linked Railway service (values are never printed)."""
    config = ConfigManager().load()
    if not config.token or not config.refresh_token:
        console.print(
            "[red]No refresh token saved — refusing to push a token that can't auto-refresh.[/red]\n"
            "Log in again first: [bold]uv run rappi auth login[/bold]"
        )
        raise typer.Exit(1)

    argv = [
        "railway", "variables",
        "--set", f"RAPPI_TOKEN={config.token}",
        "--set", f"RAPPI_REFRESH_TOKEN={config.refresh_token}",
        "--set", f"RAPPI_DEVICE_ID={config.device_id}",
    ]
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, cwd=REPO_DIR, check=False)
    except FileNotFoundError:
        console.print("[red]Railway CLI not found.[/red] Install it and run [bold]railway link[/bold] in the repo.")
        raise typer.Exit(1)

    if result.returncode != 0:
        # stderr is deliberately not echoed: it could quote the command line.
        console.print(
            f"[red]Railway push failed (exit {result.returncode}).[/red] "
            "Check [bold]railway status[/bold] in the repo directory."
        )
        raise typer.Exit(1)
    console.print("[green]Pushed RAPPI_TOKEN, RAPPI_REFRESH_TOKEN and RAPPI_DEVICE_ID to Railway.[/green]")
