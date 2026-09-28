"""Tests for token auto-refresh: config precedence, refresh exchange, client retry, CLI, keep-alive.

All HTTP is mocked with pytest-httpx; all tokens are fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from rappi import constants
from rappi.auth_refresh import (
    REFRESH_PATH,
    RefreshFailedError,
    expires_at_from,
    needs_proactive_refresh,
    refresh_tokens,
)
from rappi.cli.auth import app as auth_app
from rappi.client import RappiClient, TokenExpiredError
from rappi.config import ConfigManager, RappiConfig, seed_fingerprint
from rappi.constants import Endpoints
from rappi.services.auth import set_token
from rappi.services.browser_auth import parse_login_response

ACCESS_1 = "ft.fake-access-1"
ACCESS_2 = "ft.fake-access-2"
REFRESH_1 = "ft.fake-refresh-1"
REFRESH_2 = "ft.fake-refresh-2"
ALL_FAKE_TOKENS = (ACCESS_1, ACCESS_2, REFRESH_1, REFRESH_2)
DEVICE = "dev-fake-uuid"

REFRESH_URL = f"{constants.BASE_URL}{REFRESH_PATH}"
PROFILE_URL = f"{constants.BASE_URL}{Endpoints.USER_PROFILE}"
PRIME_URL = f"{constants.BASE_URL}{Endpoints.IS_PRIME}"


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _in(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


@pytest.fixture
def cm() -> ConfigManager:
    """ConfigManager at the isolated RAPPI_CONFIG_DIR (see conftest)."""
    return ConfigManager()


def _seed(cm: ConfigManager, **overrides) -> RappiConfig:
    fields = {
        "token": ACCESS_1,
        "refresh_token": REFRESH_1,
        "token_expires_at": _in(24 * 6),  # well outside the 48h margin
        "device_id": DEVICE,
    }
    fields.update(overrides)
    config = RappiConfig(**fields)
    cm.save(config)
    return config


def _refresh_ok(httpx_mock, access=ACCESS_2, refresh=None, expires_in=604800, old_refresh=REFRESH_1):
    body = {"access_token": access, "expires_in": expires_in, "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = refresh
    httpx_mock.add_response(
        method="POST", url=REFRESH_URL, match_json={"refresh_token": old_refresh}, json=body
    )


def _refresh_rejected(httpx_mock, status=401):
    httpx_mock.add_response(
        method="POST",
        url=REFRESH_URL,
        status_code=status,
        json={"error": {"code": "invalid_credentials", "message": "Check the invalid refresh token parameter."}},
    )


# ---------------------------------------------------------------------------
# AC1-3: 401 handling in the client
# ---------------------------------------------------------------------------

class TestUnauthorizedRetry:
    async def test_401_triggers_refresh_retry_and_persists_new_token(self, cm, httpx_mock):
        _seed(cm)
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1),
            status_code=401, headers={"x-refresh-token": "true"},
        )
        _refresh_ok(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), json={"id": 1}, is_reusable=True
        )

        async with RappiClient(config_manager=cm) as client:
            assert await client.get(Endpoints.USER_PROFILE) == {"id": 1}
            # Later requests go out with the new Bearer header directly
            assert await client.get(Endpoints.USER_PROFILE) == {"id": 1}
            assert client.config.token == ACCESS_2

        saved = cm.load()
        assert saved.token == ACCESS_2
        assert saved.token_expires_at is not None
        refresh_request = httpx_mock.get_request(method="POST")
        assert "authorization" not in refresh_request.headers
        assert refresh_request.headers["content-type"] == "application/json"
        assert len(httpx_mock.get_requests(method="GET")) == 3

    async def test_second_401_after_refresh_raises_actionable_error(self, cm, httpx_mock):
        _seed(cm)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1), status_code=401)
        _refresh_ok(httpx_mock)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), status_code=401)

        async with RappiClient(config_manager=cm) as client:
            with pytest.raises(TokenExpiredError) as exc_info:
                await client.get(Endpoints.USER_PROFILE)

        message = str(exc_info.value)
        assert "rappi auth login" in message
        assert "push-railway" in message
        assert "<new-token>" not in message
        assert len(httpx_mock.get_requests(method="POST")) == 1  # retried once, not looped

    async def test_401_without_refresh_token_raises_and_skips_refresh(self, cm, httpx_mock):
        _seed(cm, refresh_token=None)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, status_code=401)

        async with RappiClient(config_manager=cm) as client:
            with pytest.raises(TokenExpiredError, match="rappi auth login"):
                await client.get(Endpoints.USER_PROFILE)

        assert httpx_mock.get_requests(method="POST") == []

    async def test_401_with_failed_refresh_raises_token_expired(self, cm, httpx_mock):
        _seed(cm)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, status_code=401)
        _refresh_rejected(httpx_mock)

        async with RappiClient(config_manager=cm) as client:
            with pytest.raises(TokenExpiredError, match="rappi auth login"):
                await client.get(Endpoints.USER_PROFILE)
        assert cm.load().token == ACCESS_1  # nothing overwritten

    async def test_no_token_message_is_actionable(self, cm):
        _seed(cm, token=None, refresh_token=None)
        with pytest.raises(TokenExpiredError, match="No token configured") as exc_info:
            async with RappiClient(config_manager=cm):
                pass
        assert "rappi auth login" in str(exc_info.value)
        assert "<your-token>" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC4: refresh-token rotation
# ---------------------------------------------------------------------------

class TestRefreshTokenRotation:
    async def test_refresh_keeps_old_refresh_token_when_omitted(self, cm, httpx_mock):
        _seed(cm)
        _refresh_ok(httpx_mock, refresh=None)
        fresh = await refresh_tokens(cm)
        assert fresh.token == ACCESS_2
        assert fresh.refresh_token == REFRESH_1
        assert cm.load().refresh_token == REFRESH_1

    async def test_refresh_replaces_refresh_token_when_rotated(self, cm, httpx_mock):
        _seed(cm)
        _refresh_ok(httpx_mock, refresh=REFRESH_2)
        fresh = await refresh_tokens(cm)
        assert fresh.refresh_token == REFRESH_2
        assert cm.load().refresh_token == REFRESH_2

    async def test_refresh_sets_expiry_from_expires_in(self, cm, httpx_mock):
        _seed(cm)
        _refresh_ok(httpx_mock, expires_in=3600)
        fresh = await refresh_tokens(cm)
        expires = datetime.fromisoformat(fresh.token_expires_at)
        assert timedelta(minutes=55) < expires - datetime.now(UTC) <= timedelta(hours=1)

    async def test_refresh_keeps_seed_fingerprint(self, cm, httpx_mock):
        _seed(cm, seed_fingerprint="abcd1234abcd1234")
        _refresh_ok(httpx_mock)
        await refresh_tokens(cm)
        assert json.loads(cm.path.read_text())["seed_fingerprint"] == "abcd1234abcd1234"

    async def test_refresh_failure_raises_with_code_only(self, cm, httpx_mock):
        _seed(cm)
        _refresh_rejected(httpx_mock)
        with pytest.raises(RefreshFailedError) as exc_info:
            await refresh_tokens(cm)
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "invalid_credentials"
        assert REFRESH_1 not in str(exc_info.value)

    async def test_refresh_without_refresh_token_raises_without_http(self, cm, httpx_mock):
        _seed(cm, refresh_token=None)
        with pytest.raises(RefreshFailedError, match="no_refresh_token"):
            await refresh_tokens(cm)


# ---------------------------------------------------------------------------
# AC5: proactive refresh
# ---------------------------------------------------------------------------

class TestProactiveRefresh:
    async def test_expiry_within_48h_refreshes_before_first_request(self, cm, httpx_mock):
        _seed(cm, token_expires_at=_in(12))
        _refresh_ok(httpx_mock)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), json={"id": 1})

        async with RappiClient(config_manager=cm) as client:
            assert await client.get(Endpoints.USER_PROFILE) == {"id": 1}
        assert cm.load().token == ACCESS_2

    async def test_expiry_beyond_48h_makes_no_refresh_call(self, cm, httpx_mock):
        _seed(cm, token_expires_at=_in(72))
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1), json={"id": 1})

        async with RappiClient(config_manager=cm) as client:
            assert await client.get(Endpoints.USER_PROFILE) == {"id": 1}
        assert httpx_mock.get_requests(method="POST") == []

    async def test_unknown_expiry_with_refresh_token_refreshes(self, cm, httpx_mock):
        _seed(cm, token_expires_at=None)
        _refresh_ok(httpx_mock)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), json={"id": 1})

        async with RappiClient(config_manager=cm) as client:
            await client.get(Endpoints.USER_PROFILE)
        assert len(httpx_mock.get_requests(method="POST")) == 1

    async def test_proactive_failure_keeps_unexpired_token(self, cm, httpx_mock):
        _seed(cm, token_expires_at=_in(12))
        _refresh_rejected(httpx_mock, status=500)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1), json={"id": 1})

        async with RappiClient(config_manager=cm) as client:
            assert await client.get(Endpoints.USER_PROFILE) == {"id": 1}

    async def test_proactive_failure_with_expired_token_raises(self, cm, httpx_mock):
        _seed(cm, token_expires_at=_in(-1))
        _refresh_rejected(httpx_mock)

        with pytest.raises(TokenExpiredError, match="rappi auth login"):
            async with RappiClient(config_manager=cm):
                pass

    def test_needs_proactive_refresh_rules(self):
        assert not needs_proactive_refresh(RappiConfig(token=ACCESS_1))  # no refresh token
        assert needs_proactive_refresh(RappiConfig(refresh_token=REFRESH_1))  # unknown expiry
        assert needs_proactive_refresh(RappiConfig(refresh_token=REFRESH_1, token_expires_at=_in(47)))
        assert not needs_proactive_refresh(RappiConfig(refresh_token=REFRESH_1, token_expires_at=_in(49)))
        assert needs_proactive_refresh(RappiConfig(refresh_token=REFRESH_1, token_expires_at="garbage"))


# ---------------------------------------------------------------------------
# AC6: concurrency
# ---------------------------------------------------------------------------

class TestConcurrentRefresh:
    async def test_two_simultaneous_401s_make_exactly_one_refresh_call(self, cm, httpx_mock):
        _seed(cm)
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1), status_code=401, is_reusable=True
        )
        _refresh_ok(httpx_mock)  # registered once: a second POST would be unmatched
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), json={"id": 1}, is_reusable=True
        )

        # Two clients (e.g. CLI + server) both holding the old token → both see a 401.
        async with RappiClient(config_manager=cm) as a, RappiClient(config_manager=cm) as b:
            results = await asyncio.gather(a.get(Endpoints.USER_PROFILE), b.get(Endpoints.USER_PROFILE))
            assert a.config.token == b.config.token == ACCESS_2

        assert results == [{"id": 1}, {"id": 1}]
        assert len(httpx_mock.get_requests(method="POST")) == 1
        assert len(httpx_mock.get_requests(method="GET", match_headers=_bearer(ACCESS_1))) == 2

    async def test_same_client_concurrent_401s_make_one_refresh_call(self, cm, httpx_mock):
        _seed(cm)
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_1), status_code=401, is_reusable=True
        )
        _refresh_ok(httpx_mock)
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), json={"id": 1}, is_reusable=True
        )

        async with RappiClient(config_manager=cm) as client:
            results = await asyncio.gather(
                client.get(Endpoints.USER_PROFILE), client.get(Endpoints.USER_PROFILE)
            )
        assert results == [{"id": 1}, {"id": 1}]
        assert len(httpx_mock.get_requests(method="POST")) == 1


# ---------------------------------------------------------------------------
# AC7: env seed vs persisted file precedence
# ---------------------------------------------------------------------------

class TestSeedPrecedence:
    def test_matching_fingerprint_file_wins(self, cm, monkeypatch):
        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_1)
        monkeypatch.setenv("RAPPI_REFRESH_TOKEN", REFRESH_1)
        # File holds tokens refreshed from this same seed
        _seed(
            cm, token=ACCESS_2, refresh_token=REFRESH_2, token_expires_at=_in(100),
            seed_fingerprint=seed_fingerprint(REFRESH_1),
        )
        config = cm.load()
        assert config.token == ACCESS_2
        assert config.refresh_token == REFRESH_2
        assert config.token_expires_at is not None

    def test_changed_env_seed_wins_and_reseeds(self, cm, monkeypatch):
        _seed(
            cm, token=ACCESS_1, refresh_token=REFRESH_1, token_expires_at=_in(100),
            seed_fingerprint=seed_fingerprint(REFRESH_1),
        )
        # A new manual login was pushed
        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_2)
        monkeypatch.setenv("RAPPI_REFRESH_TOKEN", REFRESH_2)

        config = cm.load()
        assert config.token == ACCESS_2
        assert config.refresh_token == REFRESH_2
        assert config.token_expires_at is None
        persisted = json.loads(cm.path.read_text())
        assert persisted["token"] == ACCESS_2
        assert persisted["refresh_token"] == REFRESH_2
        assert persisted["seed_fingerprint"] == seed_fingerprint(REFRESH_2)
        assert persisted["token_expires_at"] is None
        # ...and on the next load the (same-seed) file wins
        assert cm.load().token == ACCESS_2

    def test_fingerprint_is_sha256_prefix_of_refresh_or_access_seed(self, cm, monkeypatch):
        import hashlib

        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_1)
        cm.load()
        expected = hashlib.sha256(ACCESS_1.encode()).hexdigest()[:16]
        assert json.loads(cm.path.read_text())["seed_fingerprint"] == expected
        assert ACCESS_1[3:] not in expected

    def test_legacy_env_token_only_behaves_as_before(self, cm, monkeypatch):
        _seed(cm, token="file-token", refresh_token=None, token_expires_at=None)
        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_1)
        monkeypatch.setenv("RAPPI_DEVICE_ID", "env-device")
        config = cm.load()
        assert config.token == ACCESS_1
        assert config.refresh_token is None
        assert config.device_id == "env-device"
        assert not needs_proactive_refresh(config)

    def test_legacy_env_token_with_no_file(self, cm, monkeypatch):
        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_1)
        assert cm.load().token == ACCESS_1

    def test_no_env_seed_uses_file_only(self, cm):
        _seed(cm)
        config = cm.load()
        assert config.token == ACCESS_1
        assert config.refresh_token == REFRESH_1
        assert config.seed_fingerprint is None

    def test_config_dir_env_resolved_at_construction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAPPI_CONFIG_DIR", str(tmp_path / "vol" / "rappi"))
        manager = ConfigManager()
        assert manager.path == tmp_path / "vol" / "rappi" / "config.json"
        monkeypatch.delenv("RAPPI_CONFIG_DIR")
        assert ConfigManager().path == Path.home() / ".rappi" / "config.json"


# ---------------------------------------------------------------------------
# AC8: atomic save with 0600
# ---------------------------------------------------------------------------

class TestAtomicSave:
    def test_save_sets_mode_0600(self, cm):
        cm.path.parent.mkdir(parents=True, exist_ok=True)
        cm.path.write_text("{}")
        os.chmod(cm.path, 0o644)
        cm.save(RappiConfig(token=ACCESS_1))
        assert stat.S_IMODE(cm.path.stat().st_mode) == 0o600

    def test_save_is_atomic_replace_from_same_dir(self, cm, monkeypatch):
        calls = []
        real_replace = os.replace

        def spy(src, dst):
            calls.append((Path(src), Path(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr("rappi.config.os.replace", spy)
        cm.save(RappiConfig(token=ACCESS_1))
        [(src, dst)] = calls
        assert src.parent == dst.parent == cm.path.parent
        assert dst == cm.path
        assert not src.exists()

    def test_failed_save_leaves_original_intact_and_no_temp_files(self, cm, monkeypatch):
        cm.save(RappiConfig(token=ACCESS_1))
        original = cm.path.read_text()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("rappi.config.os.replace", boom)
        with pytest.raises(OSError):
            cm.save(RappiConfig(token=ACCESS_2))
        assert cm.path.read_text() == original
        assert [p.name for p in cm.path.parent.iterdir()] == ["config.json"]


# ---------------------------------------------------------------------------
# AC9: login-response capture (pure parsing)
# ---------------------------------------------------------------------------

LOGIN_URL = "https://services.grability.rappi.com/api/rocket/login/whatsapp/application_user"
LOGIN_BODY = {
    "access_token": ACCESS_1,
    "refresh_token": REFRESH_1,
    "expires_in": 604800,
    "token_type": "Bearer",
    "first_login": False,
}


class TestLoginResponseParsing:
    def test_extracts_refresh_token_and_expires_in(self):
        tokens = parse_login_response(LOGIN_URL, 200, LOGIN_BODY)
        assert tokens is not None
        assert tokens.access_token == ACCESS_1
        assert tokens.refresh_token == REFRESH_1
        assert tokens.expires_in == 604800

    @pytest.mark.parametrize("provider", ["email", "twilio", "google", "apple", "facebook"])
    def test_all_login_providers(self, provider):
        url = f"https://services.grability.rappi.com/api/rocket/login/{provider}/application_user"
        assert parse_login_response(url, 200, LOGIN_BODY) is not None

    def test_ignores_application_user_auth_bodies(self):
        url = "https://services.grability.rappi.com/ms/application-user/auth"
        assert parse_login_response(url, 200, {"id": 42, "email": "f@example.com", "name": "Fake"}) is None
        assert parse_login_response(url, 200, LOGIN_BODY) is None

    def test_ignores_non_200_and_bodies_without_access_token(self):
        assert parse_login_response(LOGIN_URL, 401, LOGIN_BODY) is None
        assert parse_login_response(LOGIN_URL, 200, {"refresh_token": REFRESH_1}) is None
        assert parse_login_response(LOGIN_URL, 200, ["not", "a", "dict"]) is None
        other = "https://services.grability.rappi.com/api/rocket/login/whatsapp/send-code"
        assert parse_login_response(other, 200, LOGIN_BODY) is None

    def test_missing_refresh_token_and_expiry(self):
        tokens = parse_login_response(LOGIN_URL, 200, {"access_token": ACCESS_1})
        assert tokens.refresh_token is None
        assert tokens.expires_in is None

    def test_set_token_saves_refresh_token_and_expiry(self, cm):
        set_token(cm, ACCESS_1, DEVICE, refresh_token=REFRESH_1, expires_in=604800)
        config = cm.load()
        assert config.refresh_token == REFRESH_1
        expires = datetime.fromisoformat(config.token_expires_at)
        assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)

    def test_set_token_without_refresh_clears_previous_pair(self, cm):
        _seed(cm)
        set_token(cm, ACCESS_2)
        config = cm.load()
        assert config.token == ACCESS_2
        assert config.refresh_token is None
        assert config.token_expires_at is None


# ---------------------------------------------------------------------------
# AC10: server keep-alive tick + wiring
# ---------------------------------------------------------------------------

class TestKeepalive:
    async def test_tick_refreshes_when_due(self, cm, httpx_mock):
        from rappi.mcp.server import keepalive_tick

        _seed(cm, token_expires_at=_in(10))
        _refresh_ok(httpx_mock)
        result = await keepalive_tick()
        assert result.startswith("refreshed, expires ")
        assert cm.load().token == ACCESS_2

    async def test_tick_is_noop_when_not_due(self, cm, httpx_mock):
        from rappi.mcp.server import keepalive_tick

        _seed(cm, token_expires_at=_in(100))
        assert (await keepalive_tick()).startswith("not due")
        assert httpx_mock.get_requests() == []

    async def test_tick_is_noop_without_refresh_token(self, cm, httpx_mock):
        from rappi.mcp.server import keepalive_tick

        _seed(cm, refresh_token=None)
        assert (await keepalive_tick()).startswith("skipped")

    async def test_tick_swallows_and_reports_refresh_errors(self, cm, httpx_mock):
        from rappi.mcp.server import keepalive_tick

        _seed(cm, token_expires_at=_in(10))
        _refresh_rejected(httpx_mock)
        result = await keepalive_tick()
        assert result.startswith("error")
        assert "invalid_credentials" in result
        assert cm.load().token == ACCESS_1

    async def test_tick_swallows_unexpected_errors_without_leaking(self, cm, monkeypatch):
        from rappi.mcp import server

        _seed(cm, token_expires_at=_in(10))

        async def boom(*_args, **_kwargs):
            raise RuntimeError(f"unexpected {ACCESS_1}")

        monkeypatch.setattr(server, "refresh_tokens", boom)
        result = await server.keepalive_tick()
        assert result == "error: RuntimeError"

    def test_http_app_health_and_keepalive_lifespan(self, monkeypatch):
        from starlette.testclient import TestClient

        from rappi.mcp import server

        started = []

        async def fake_loop(*_args, **_kwargs):
            started.append(True)
            await asyncio.sleep(3600)

        monkeypatch.setattr(server, "_keepalive_loop", fake_loop)
        app = server.build_http_app()
        with TestClient(app) as client:
            assert client.get("/health").text == "ok"
        assert started == [True]
        paths = {getattr(r, "path", None) for r in app.router.routes}
        assert {"/mcp", "/sse", "/health"} <= paths


    def test_missing_volume_warns_only_when_refresh_seed_without_config_dir(self, monkeypatch, capsys):
        from rappi.mcp import server

        # Refresh seed + no persistent config dir → one loud stderr line, no secrets
        monkeypatch.setenv("RAPPI_REFRESH_TOKEN", REFRESH_1)
        monkeypatch.delenv("RAPPI_CONFIG_DIR")
        assert server.warn_if_no_config_volume() is True
        err = capsys.readouterr().err
        assert err.strip() == server.MISSING_VOLUME_WARNING
        assert REFRESH_1 not in err

        # Volume configured → silent
        monkeypatch.setenv("RAPPI_CONFIG_DIR", "/data/rappi")
        assert server.warn_if_no_config_volume() is False
        # Legacy token-only seed → silent
        monkeypatch.delenv("RAPPI_REFRESH_TOKEN")
        monkeypatch.delenv("RAPPI_CONFIG_DIR")
        monkeypatch.setenv("RAPPI_TOKEN", ACCESS_1)
        assert server.warn_if_no_config_volume() is False
        assert capsys.readouterr().err == ""

    def test_http_lifespan_emits_missing_volume_warning(self, monkeypatch, capsys):
        from starlette.testclient import TestClient

        from rappi.mcp import server

        async def idle_loop(*_args, **_kwargs):
            await asyncio.sleep(3600)

        monkeypatch.setattr(server, "_keepalive_loop", idle_loop)
        monkeypatch.setattr(server.mcp, "_session_manager", None, raising=False)
        monkeypatch.setenv("RAPPI_REFRESH_TOKEN", REFRESH_1)
        monkeypatch.delenv("RAPPI_CONFIG_DIR")
        with TestClient(server.build_http_app()) as client:
            assert client.get("/health").text == "ok"
        assert server.MISSING_VOLUME_WARNING in capsys.readouterr().err


class TestAuthStatusTool:
    async def test_auth_status_reports_expiry_and_auto_refresh(self, cm, httpx_mock, monkeypatch):
        from rappi.mcp import server

        async def no_sync(_client):
            return None

        monkeypatch.setattr(server, "_sync_address_coords", no_sync)
        expires = _in(100)
        _seed(cm, token_expires_at=expires)
        httpx_mock.add_response(method="GET", url=PROFILE_URL, json={"id": 1, "name": "Fake", "email": "f@x.co"})
        httpx_mock.add_response(method="GET", url=PRIME_URL, json={"is_prime": False})

        result = await server.auth_status()
        assert result["auto_refresh"] is True
        assert result["token_expires_at"] == expires
        assert not any(t in json.dumps(result) for t in ALL_FAKE_TOKENS)


# ---------------------------------------------------------------------------
# AC11-12: CLI refresh / status / push-railway, and no token leaks anywhere
# ---------------------------------------------------------------------------

def _completed(returncode=0, stderr=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=b"", stderr=stderr)


class TestPushRailway:
    def test_push_railway_argv_list_no_shell_all_three_pairs(self, cm, monkeypatch):
        _seed(cm)
        fake_run = MagicMock(return_value=_completed())
        monkeypatch.setattr("rappi.cli.auth.subprocess.run", fake_run)

        result = CliRunner().invoke(auth_app, ["push-railway"])

        assert result.exit_code == 0, result.output
        fake_run.assert_called_once()
        args, kwargs = fake_run.call_args
        argv = args[0] if args else kwargs["args"]
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert argv[:2] == ["railway", "variables"]
        pairs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--set"]
        assert pairs == [
            f"RAPPI_TOKEN={ACCESS_1}",
            f"RAPPI_REFRESH_TOKEN={REFRESH_1}",
            f"RAPPI_DEVICE_ID={DEVICE}",
        ]

    def test_push_railway_refuses_without_refresh_token(self, cm, monkeypatch):
        _seed(cm, refresh_token=None)
        fake_run = MagicMock(return_value=_completed())
        monkeypatch.setattr("rappi.cli.auth.subprocess.run", fake_run)

        result = CliRunner().invoke(auth_app, ["push-railway"])

        assert result.exit_code == 1
        assert "rappi auth login" in result.output
        fake_run.assert_not_called()

    def test_push_railway_failure_does_not_echo_stderr(self, cm, monkeypatch):
        _seed(cm)
        fake_run = MagicMock(return_value=_completed(1, stderr=f"bad value {ACCESS_1}".encode()))
        monkeypatch.setattr("rappi.cli.auth.subprocess.run", fake_run)

        result = CliRunner().invoke(auth_app, ["push-railway"])

        assert result.exit_code == 1
        assert "failed" in result.output
        assert ACCESS_1 not in result.output


class TestNoTokenLeaks:
    def test_no_token_material_in_output_or_logs(self, cm, httpx_mock, monkeypatch, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        _seed(cm)
        runner = CliRunner()
        outputs = []

        # 1. Successful forced refresh (rotates the refresh token)
        _refresh_ok(httpx_mock, refresh=REFRESH_2)
        r = runner.invoke(auth_app, ["refresh"])
        assert r.exit_code == 0, r.output
        assert r.output.startswith("ok, expires ")
        outputs.append(r.output)

        # 2. Failed refresh
        _refresh_rejected(httpx_mock)
        r = runner.invoke(auth_app, ["refresh"])
        assert r.exit_code == 1
        assert "invalid_credentials" in r.output
        outputs.append(r.output)

        # 3. Failed refresh inside the client after a 401
        httpx_mock.add_response(method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2), status_code=401)
        _refresh_rejected(httpx_mock)

        async def _call():
            async with RappiClient(config_manager=cm) as client:
                await client.get(Endpoints.USER_PROFILE)

        with pytest.raises(TokenExpiredError) as exc_info:
            asyncio.run(_call())
        outputs.append(str(exc_info.value))

        # 4. auth status
        httpx_mock.add_response(
            method="GET", url=PROFILE_URL, match_headers=_bearer(ACCESS_2),
            json={"id": 42, "name": "Fake User", "email": "f@example.com"},
        )
        httpx_mock.add_response(method="GET", url=PRIME_URL, json={"is_prime": False})
        r = runner.invoke(auth_app, ["status"])
        assert r.exit_code == 0, r.output
        assert "Expires" in r.output
        assert "Auto-refresh" in r.output
        outputs.append(r.output)

        # 5. push-railway (subprocess mocked)
        fake_run = MagicMock(return_value=_completed())
        monkeypatch.setattr("rappi.cli.auth.subprocess.run", fake_run)
        r = runner.invoke(auth_app, ["push-railway"])
        assert r.exit_code == 0, r.output
        outputs.append(r.output)

        captured = capsys.readouterr()
        everything = "\n".join([*outputs, captured.out, captured.err, caplog.text])
        for secret in ALL_FAKE_TOKENS:
            assert secret not in everything
        assert "fake-" not in everything  # no partial token fragments either


def test_cli_refresh_network_error_prints_error_type_only(cm, httpx_mock):
    import httpx

    _seed(cm)
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), method="POST", url=REFRESH_URL)
    result = CliRunner().invoke(auth_app, ["refresh"])
    assert result.exit_code == 1
    assert result.output.strip() == "refresh failed: ConnectError"


def test_expires_at_from_defaults_to_seven_days():
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert expires_at_from(None, now=now) == "2026-10-05T12:00:00+00:00"
    assert expires_at_from("3600", now=now) == "2026-09-28T13:00:00+00:00"
