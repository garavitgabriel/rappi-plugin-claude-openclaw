---
batch: 1
branch: feat/token-auto-refresh
worktree_path: /Users/gabrielgaravit/Projects/Rappi Claude Plugin/.claude/worktrees/token-auto-refresh
completed_at: 2026-09-28 11:47
verification: pass
---
## Acceptance criteria
All named tests are in `tests/test_auth_refresh.py` unless noted.
- [✓] 1 401 → refresh → retry succeeds, new token persisted, later requests use new Bearer: `TestUnauthorizedRetry::test_401_triggers_refresh_retry_and_persists_new_token`
- [✓] 2 401 → refresh → second 401 raises `TokenExpiredError`; the message has `rappi auth login` and no `<new-token>`: `TestUnauthorizedRetry::test_second_401_after_refresh_raises_actionable_error` (also `test_401_with_failed_refresh_raises_token_expired`, `test_no_token_message_is_actionable`)
- [✓] 3 401 with no refresh_token raises and makes no POST: `TestUnauthorizedRetry::test_401_without_refresh_token_raises_and_skips_refresh`
- [✓] 4 omitted refresh_token is kept and a rotated one is replaced: `TestRefreshTokenRotation::test_refresh_keeps_old_refresh_token_when_omitted`, `::test_refresh_replaces_refresh_token_when_rotated`
- [✓] 5 proactive refresh: `TestProactiveRefresh::test_expiry_within_48h_refreshes_before_first_request`, `::test_expiry_beyond_48h_makes_no_refresh_call`, `::test_unknown_expiry_with_refresh_token_refreshes` (plus best-effort on failure: `::test_proactive_failure_keeps_unexpired_token`, `::test_proactive_failure_with_expired_token_raises`)
- [✓] 6 two simultaneous 401s make exactly one refresh call: `TestConcurrentRefresh::test_two_simultaneous_401s_make_exactly_one_refresh_call` (two clients) and `::test_same_client_concurrent_401s_make_one_refresh_call`. Mutation-checked: disabling the "already refreshed" guard fails both.
- [✓] 7 precedence: `TestSeedPrecedence::test_matching_fingerprint_file_wins`, `::test_changed_env_seed_wins_and_reseeds`, `::test_legacy_env_token_only_behaves_as_before` (+ `::test_no_env_seed_uses_file_only`, `::test_config_dir_env_resolved_at_construction`)
- [✓] 8 atomic save and mode 0600: `TestAtomicSave::test_save_sets_mode_0600`, `::test_save_is_atomic_replace_from_same_dir`, `::test_failed_save_leaves_original_intact_and_no_temp_files`
- [✓] 9 login-response parsing: `TestLoginResponseParsing::test_extracts_refresh_token_and_expires_in`, `::test_ignores_application_user_auth_bodies` (+ providers, non-200, `set_token` saving refresh_token and expiry)
- [✓] 10 keepalive_tick: `TestKeepalive::test_tick_refreshes_when_due`, `::test_tick_is_noop_when_not_due`, `::test_tick_swallows_and_reports_refresh_errors`, `::test_tick_swallows_unexpected_errors_without_leaking`. Wiring: `::test_http_app_health_and_keepalive_lifespan`, which checks that `/health` returns ok, the lifespan starts the loop, and `/mcp` and `/sse` are mounted.
- [✓] 11 no-leak: `TestNoTokenLeaks::test_no_token_material_in_output_or_logs` covers refresh ok, refresh failed, client 401 plus failed refresh, `auth status`, and `push-railway` with `subprocess.run` mocked. It checks CLI output, capsys stdout/stderr and caplog at DEBUG for all 4 fake tokens and for `fake-` fragments. `TestAuthStatusTool::test_auth_status_reports_expiry_and_auto_refresh` covers the MCP tool.
- [✓] 12 push-railway: `TestPushRailway::test_push_railway_argv_list_no_shell_all_three_pairs`, `::test_push_railway_refuses_without_refresh_token` (+ `::test_push_railway_failure_does_not_echo_stderr`)
- [✓] 13 all 195 pre-existing tests pass **unmodified**. No assertion on the old `--token <new-token>` text existed, so none was changed. The only change to existing test infrastructure is an autouse isolation fixture added to `tests/conftest.py`: it sets `RAPPI_CONFIG_DIR` to a tmp dir, clears `RAPPI_TOKEN`, `RAPPI_REFRESH_TOKEN`, `RAPPI_DEVICE_ID`, `RAPPI_LAT` and `RAPPI_LNG`, and pins `RAPPI_COUNTRY`. On main, `test_default_config_manager` read the real `~/.rappi/config.json`; now it can't.
- [✓] 14 the verification triple is green (below).

## Verification
- pytest: 248 passed · ruff new files: clean · ruff total: 172 (≤173) · import smoke: ok (exit 0)

## Diff summary
4 commits on `feat/token-auto-refresh`: 11 files, +1348 / −62.
- Added: `src/rappi/auth_refresh.py` (~200), `tests/test_auth_refresh.py` (~700)
- Modified: `src/rappi/config.py` (~+90: new fields, `RAPPI_CONFIG_DIR`, seed precedence, atomic 0600 save), `src/rappi/client.py` (~+70: proactive refresh, 401 → refresh → retry once, actionable messages), `src/rappi/services/browser_auth.py` (~+75: pure `parse_login_response`, captures login response), `src/rappi/services/auth.py` (`set_token` + refresh_token/expires_in), `src/rappi/cli/auth.py` (~+85: `--refresh-token`, `refresh`, `push-railway`, status rows, logout clears refresh), `src/rappi/mcp/server.py` (~+100: `keepalive_tick`, `_keepalive_loop`, `build_http_app` wrapping the session lifespan, `auth_status` fields, prompt text), `src/rappi/constants.py` (`Endpoints.REFRESH_TOKEN`), `README.md` (Auth runbook, env vars, CLI table, security), `tests/conftest.py` (isolation fixture)

## Design deviations (anything that differs from "Locked design", and why)
- **`refresh_tokens(config_manager, failed_token=None)`** takes an extra optional arg. The "token already differs from the one the caller saw failing" check needs to know which token the caller saw fail. `None` forces a refresh (used by `rappi auth refresh`). The proactive, 401 and keep-alive paths all pass the current token, so concurrent proactive refreshes also collapse into one.
- **Client copies only the token fields** (token, refresh_token, expires_at, fingerprint) onto the live `RappiConfig` after a refresh instead of replacing the object. `_sync_address_coords` mutates `client._config.lat/lng` in place, and replacing the object would drop those coordinates.
- **Client with only a refresh token** (env seeded `RAPPI_REFRESH_TOKEN` but no `RAPPI_TOKEN`) refreshes in `__aenter__` before raising "No token configured". `rappi auth status` accepts that state too.
- **`set_token` without a refresh token clears the old refresh token and expiry**, and so do `rappi auth token` and `logout`. A new login replaces the whole token pair, which avoids pairing a new access token with a stale refresh token. `set_token` leaves `seed_fingerprint` untouched: locally, with an env seed set, a fresh `rappi auth login` now wins over the env until the env seed changes. Before, the env always overrode the file. This only matters if someone exports `RAPPI_TOKEN` locally.
- **Env-wins persistence is best-effort.** A read-only config dir logs a warning (no token material) and still uses the env tokens in memory.
- **Keep-alive runs its first tick at startup**, then every 6 h. On the first boot after `push-railway`, expiry is unknown, so it refreshes immediately. That validates the pushed refresh token right away.
- **Login capture:** if the login response's `access_token` differs from the Bearer header on `/ms/application-user/auth`, the Bearer header wins and a status line notes the mismatch (no token values). The refresh_token from the login response is still saved. The capture waits up to 3 s after the auth call for the login response, in case its JSON is still being read. The one new `except Exception` in `browser_auth._on_response` (+1 BLE001) mirrors the existing handler. It is offset by removing an unused import and an I001, for a net total of 172.
- **`push-railway` runs from `REPO_DIR = Path(__file__).parents[3]`.** That is correct for this repo's editable `uv run` install. From a non-editable wheel install it would point at site-packages.
- **`rappi auth refresh`** prints `refresh failed: <code> (HTTP <status>)`. On network errors it prints `refresh failed: <ExceptionType>`.

## Open questions for the chief's live verification
1. **Refresh-token rotation and single use (unknown a).** If Rappi rotates *and invalidates* the old refresh token on each refresh, the local CLI and Railway must not refresh from the same seed. After `push-railway`, a local refresh (e.g. `rappi auth refresh`, or any local CLI call within 48 h of expiry) would invalidate Railway's seed, and the reverse also holds. If that's the behavior, consider having `push-railway` stop local auto-refresh, or accept "Railway owns the token after push". The log line says `refresh token rotated|kept`, and so does `rappi auth refresh` indirectly (compare `auth status` before and after).
2. **Headers (unknown c).** The refresh sends every `build_headers` header minus `authorization`, including `deviceid`, `origin`, `app-version` and `x-application-id`, plus `content-type: application/json`. If the real endpoint rejects the extra headers, trim `_refresh_headers()` in `auth_refresh.py`.
3. **`expires_in` on refresh responses.** The code defaults to 604800 when it's absent and accepts numeric strings. Check what the real response returns.
4. **Does the login response pass through `page.on("response")` with a readable JSON body** for the WhatsApp/OTP flow? If `auth login` prints "No refresh token captured", the endpoint path differs from `/api/rocket/login/*/application_user`, and `LOGIN_RESPONSE_PREFIX` needs adjusting.
5. **Cross-process race (low risk).** Seeding and refresh are race-free within one process, because `load()` is synchronous and the refresh holds an asyncio lock plus `flock`. Railway runs a single process. A local CLI and a local server sharing `~/.rappi` with an env seed set could in theory interleave the one-time env-wins write with a refresh. Not worth more locking unless it's observed.
6. After deploy, the keep-alive logs to stderr as `[rappi-mcp] token keepalive: refreshed, expires …` (or `not due, expires …`). Watch for it in the Railway logs.

## Review fixes
- FIX 1, missing-volume guard: `server.warn_if_no_config_volume()` runs at the start of the HTTP lifespan. With `RAPPI_REFRESH_TOKEN` set and `RAPPI_CONFIG_DIR` unset it prints the one-line `[rappi-mcp] WARNING: …` to stderr. It only warns and never refuses to start. Tests: `TestKeepalive::test_missing_volume_warns_only_when_refresh_seed_without_config_dir` (it warns once with no secret in the text, and stays silent with a volume set and with a legacy token-only seed) and `::test_http_lifespan_emits_missing_volume_warning`. FIX 2: a README line under the Railway setup steps says Railway owns the session after push-railway, and to log in again locally if needed. Verification: pytest 250 passed · ruff new files clean · ruff total 172 · import smoke ok.
