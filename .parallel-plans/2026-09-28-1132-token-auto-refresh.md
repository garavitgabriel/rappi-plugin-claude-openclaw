# Build: Rappi token auto-refresh

> Single-builder package, prepared 2026-09-28 11:32 by the chief session (Rappi expired auth).
> One batch, run unattended under `/goal`. **The chief reviews before anything merges or deploys.**
> Builder stops at DONE: no push, no merge, no `/end-worktree`, no Railway changes.

## Project context

- Repo: `~/Projects/Rappi Claude Plugin` (origin `garavitgabriel/rappi-plugin-claude-openclaw`, branch off `main` @ `6e7ca92`)
- Project guide: `CLAUDE.md` (read it first)
- Stack: Python 3.12 · uv · httpx · pydantic · FastMCP · typer · pytest + pytest-asyncio + pytest-httpx
- Baseline on 2026-09-28: `uv run pytest tests/ -q` → **195 passed**; `uvx ruff check src tests` → **173 pre-existing errors** (not yours to fix)
- Verification triple:
  1. `uv run pytest tests/ -q` → all pass, count > 195
  2. `uvx ruff check src/rappi/auth_refresh.py tests/test_auth_refresh.py` → clean, AND `uvx ruff check src tests --statistics` total ≤ 173
  3. `uv run python -c "import rappi.mcp.server, rappi.cli"` → exits 0

## Why we're building this

The hosted `claude.ai rappi` connector (Railway: project `sublime-wholeness`, service `rappi-claude-plugin`)
dies about once a week with `Token expired`. The cause is now known:

- Rappi access tokens (`ft.…`, 423 chars, opaque) have `expires_in: 604800` → **exactly 7 days**.
  Confirmed from every login response in the old HAR captures.
- Every login response (`POST {BASE_URL}/api/rocket/login/{email|whatsapp|twilio|google|apple|facebook}/application_user`)
  returns `{access_token, refresh_token, expires_in, token_type, first_login}`.
- Rappi's own web app renews access tokens with
  `POST https://services.grability.rappi.com/api/rocket/refresh-token` and JSON body `{"refresh_token": "<rt>"}`, sending only
  `Content-Type: application/json`. It does this from an axios-auth-refresh interceptor, triggered when a 401
  response carries the header `x-refresh-token: true`. The web app stores the refresh token in a 3-month cookie (`rappi_refresh_token`).
- The endpoint is live. A fake token returns `401 {"error":{"code":"invalid_credentials", … "Check the invalid refresh token parameter."}}`.
- **Our plugin never saves the refresh token.** `services/browser_auth.py` grabs only the Bearer header off
  `/ms/application-user/auth`, then throws the browser context away. So every token dies at day 7.

**Not yet known (design for both cases):**
- (a) whether the refresh response returns a *new* refresh_token (rotation) or omits it;
- (b) the refresh token's own lifetime (likely ≥ 3 months);
- (c) whether the refresh endpoint also wants `deviceid` / `origin` headers.
  Send the standard non-auth headers from `build_headers` minus `authorization`. The chief verifies against the real endpoint during review.

## Locked design

### 1. Config / state (`src/rappi/config.py`)
- Add to `RappiConfig`: `refresh_token: str | None = None`, `token_expires_at: str | None = None` (ISO-8601 UTC),
  `seed_fingerprint: str | None = None`.
- Add env `RAPPI_CONFIG_DIR` to override the config directory. Default stays `~/.rappi`.
  On Railway it will point at a mounted volume (`/data/rappi`).
- Resolve the path when `ConfigManager` is constructed, not at import time, so tests can monkeypatch it.
- **Precedence rule. This is the heart of the Railway fix, so implement it exactly:**
  - Env seeds are `RAPPI_TOKEN` and the new `RAPPI_REFRESH_TOKEN`. The seed fingerprint is
    `sha256(RAPPI_REFRESH_TOKEN or RAPPI_TOKEN)[:16]`.
  - If the persisted file has a token and its `seed_fingerprint` equals the current env fingerprint, **the file wins**.
    That file holds tokens refreshed from this same seed.
  - Otherwise **the env wins**: a new manual login was pushed. Use the env token and refresh token, and persist them with
    the new fingerprint. Leave `token_expires_at` empty unless it's known.
  - No env seed: file only (the local CLI case, same as today).
  - With only the legacy `RAPPI_TOKEN` set, the behavior matches today's. Existing config tests must still pass unchanged.
- `save()` must be atomic (write a temp file in the same dir, then `os.replace`) and chmod the file to `0600`.

### 2. Refresh module: new file `src/rappi/auth_refresh.py`
- `REFRESH_PATH = "/api/rocket/refresh-token"`. Base URL is `BASE_URL` from `constants.py`; add the path to `constants.py` if that
  matches house style.
- `async def refresh_tokens(config_manager) -> RappiConfig`:
  - Takes a process-wide `asyncio.Lock` plus an `fcntl` file lock on `<config_dir>/.refresh.lock`, because the CLI and the
    server may share a directory.
  - Inside the lock, **reload** the config. If the token already differs from the one the caller saw failing, another
    caller refreshed first: return the fresh config without calling the API.
  - POST the refresh request. On 2xx, set `token=access_token`, `refresh_token=new rt or keep old`,
    `token_expires_at=now+expires_in (default 604800)`, keep `seed_fingerprint`, save, and return.
  - On non-2xx, raise `RefreshFailedError`, with the status and Rappi's `error.code` but never token material.
- `def needs_proactive_refresh(config, margin=timedelta(hours=48)) -> bool`: true when `refresh_token` is present and
  `token_expires_at` is either unknown or within `margin` of now.
- Log lines (stderr or `logging`) may say `refreshed, expires 2026-10-05T…`. **They must never include any part of a token.**

### 3. Client (`src/rappi/client.py`)
- In `__aenter__`: if `needs_proactive_refresh`, try `refresh_tokens` first. If the refresh fails and the token is not past
  `token_expires_at`, continue with the current token (best effort).
- In `_request`: on 401, if the config has a `refresh_token` and this request hasn't retried yet, refresh, rebuild the
  auth header on `self._http`, and **retry once**. Refresh on any 401 (don't require the `x-refresh-token` header, just log
  whether it was present).
- A second 401, a refresh failure, or no refresh token raises `TokenExpiredError` with an **actionable** message. It must not use
  the `--token <new-token>` placeholder. It points to `uv run rappi auth login` (run in `~/Projects/"Rappi Claude Plugin"`)
  plus `uv run rappi auth push-railway`, and names the README § Auth runbook.
- Also fix the `No token configured` message the same way.

### 4. Login capture (`src/rappi/services/browser_auth.py`, `src/rappi/services/auth.py`, `src/rappi/cli/auth.py`)
- `CapturedCredentials` gains `refresh_token: str | None` and `expires_in: int | None`.
- `_on_response` also listens for 200 responses on `/api/rocket/login/` URLs ending `application_user` whose JSON has
  `access_token`, and records `refresh_token` and `expires_in`. Keep the existing `/ms/application-user/auth` capture as the
  completion signal and the source of `device_id`. If the login response's access_token differs from the Bearer header, prefer
  the Bearer header and note it in the DONE report.
- Put the parsing in a **pure function** so it's unit-testable without Playwright.
- `set_token(...)` also saves `refresh_token` and `token_expires_at`.
- `rappi auth login --token` gets an optional `--refresh-token`.
- New CLI commands:
  - `rappi auth refresh`: force one refresh and print only `ok, expires <iso>` or the error code.
    The chief uses it to verify the real endpoint.
  - `rappi auth push-railway`: run `railway variables --set RAPPI_TOKEN=… --set RAPPI_REFRESH_TOKEN=… --set RAPPI_DEVICE_ID=…`
    through `subprocess.run([...], shell=False, capture_output=True)` in the repo dir. Print only success or failure, **never
    the values**, and never echo the command line. If `refresh_token` is missing, refuse and tell the user to log in again.
  - `rappi auth status` also shows `Expires: <date>` and `Auto-refresh: yes/no`, with no token values.

### 5. Server (`src/rappi/mcp/server.py`)
- In HTTP transports, start a **keep-alive** background task that wakes every 6 h and, if `needs_proactive_refresh`,
  calls `refresh_tokens`. It catches and logs all exceptions and never crashes the server.
  Wire it so the streamable app's lifespan and session manager still work, `/health` still returns `ok`, and stdio mode is
  untouched. Expose the tick as a plain function, `async def keepalive_tick() -> str`, so it can be unit-tested.
- The `auth_status` MCP tool adds `token_expires_at` and `auto_refresh: bool` to its output (no secrets).
- Update the two `"Token expired" → tell user to run rappi auth login` instruction lines in the server prompt text if they
  become inaccurate.

### 6. Docs
- Repo `README.md`: update or add the auth section to reflect auto-refresh and the one-time Railway setup below.
  Add a short "What still needs a human" line: the refresh token eventually dies too, or Rappi revokes it.
- Document these one-time Railway steps. **Document them only; do not run them:**
  1. Attach a volume to `rappi-claude-plugin` mounted at `/data`.
  2. Set `RAPPI_CONFIG_DIR=/data/rappi`.
  3. Run `uv run rappi auth login`, then `uv run rappi auth push-railway`.
  4. Confirm with the MCP `auth_status` showing `auto_refresh: true`.

## Scope

| Files allowed | Files off-limits |
|---|---|
| `src/rappi/auth_refresh.py` (new), `src/rappi/client.py`, `src/rappi/config.py`, `src/rappi/constants.py`, `src/rappi/services/auth.py`, `src/rappi/services/browser_auth.py`, `src/rappi/cli/auth.py`, `src/rappi/mcp/server.py`, `tests/**`, `README.md`, `docs/**`, `.parallel-plans/done-batch-1.md`, `.parallel-plans/blockers-batch-1.md` | everything else, in particular `pyproject.toml` / `uv.lock` (no new dependencies; stdlib `fcntl`/`hashlib` are fine), `Dockerfile`, `railway.json`, `skills/**`, `agents/**`, `src/rappi/memory/**`, other `services/*` |

## Acceptance criteria

Each item below needs a **named test** in `tests/test_auth_refresh.py`, or in an existing test file where it fits better. Use
`pytest-httpx` mocks. Fake tokens only, like `ft.fake-access-1` and `ft.fake-refresh-1`.

1. A 401 triggers refresh, the request is retried, and it succeeds. The new token is persisted and later requests use the new
   Bearer header.
2. A 401, refresh, then a second 401 raises `TokenExpiredError`. The message contains `rappi auth login` and does **not** contain `<new-token>`.
3. A 401 with no refresh_token raises `TokenExpiredError`, and no refresh call is made.
4. When the refresh response omits `refresh_token`, the old one is kept. When it includes one, it's replaced.
5. Proactive refresh: expiry within 48 h triggers a refresh before the first request. Expiry more than 48 h away makes no
   refresh call. Unknown expiry with a refresh_token present triggers a refresh.
6. Concurrency: two simultaneous 401s make **exactly one** refresh HTTP call, and both requests succeed.
7. Precedence: a matching fingerprint means the file wins, a changed env seed means the env wins and re-seeds, and a legacy
   env-only setup behaves as before.
8. `save()` is atomic, and the file mode is `0600`.
9. Login-response parsing extracts `refresh_token` and `expires_in` from a fixture body, and ignores `/ms/application-user/auth`
   bodies that lack them.
10. `keepalive_tick()` refreshes when due, is a no-op when not, and swallows and reports errors.
11. **No-leak test:** across a refresh, a failed refresh, `auth status`, and `push-railway`, with `subprocess.run` mocked,
    captured stdout, stderr, and logs contain neither fake token string.
12. `push-railway` calls `subprocess.run` with a list argv, `shell=False`, and all three `--set` pairs, and refuses when
    `refresh_token` is missing.
13. All 195 pre-existing tests still pass without being modified to weaken them. Updating an assertion on the old
    `--token <new-token>` error text is allowed, and must be called out in the DONE report.
14. The verification triple is green.

## Hard rules

1. **Secrets:** never print, log, commit, or write a real token anywhere. Don't read `~/.rappi/config.json`, and
   don't call the real Rappi API. Tests are fully mocked. The chief does live verification.
2. **No outward actions:** no `git push`, no PR, no merge, no `railway` command, no deploy. Commit only on your branch.
3. **Stay in scope.** If you need a file outside the allowed list, STOP and write
   `.parallel-plans/blockers-batch-1.md`.
4. Commit often, with messages like `auth: …` / `tests: …`.
5. **Self-check before DONE:** re-read the full diff (`git diff main...HEAD`), grep it for `ft.` literals that aren't
   `ft.fake…`, and run the triple.
6. On DONE, write `.parallel-plans/done-batch-1.md` using the schema below, then run `/builder-close`. **Do not run
   `/end-worktree`**, because the chief reviews first.

### Done report schema: `.parallel-plans/done-batch-1.md`

```markdown
---
batch: 1
branch: <branch>
worktree_path: <path>
completed_at: YYYY-MM-DD HH:MM
verification: pass | partial | fail
---
## Acceptance criteria
- [✓/✗] 1 … 14 (one line each, name the test)
## Verification
- pytest: N passed · ruff new files: clean · ruff total: N (≤173) · import smoke: ok
## Diff summary
- Added / modified files · ~lines
## Design deviations (anything that differs from "Locked design", and why)
## Open questions for the chief's live verification
```

## After DONE (chief + Gabriel, not the builder)

1. The chief reviews the diff against this brief (`/code-review`, then a manual pass on precedence, concurrency, and leaks).
2. Gabriel runs `uv run rappi auth login` on the branch (phone + OTP), which proves the capture saves a refresh_token.
3. The chief runs `uv run rappi auth refresh`, which proves the real endpoint's shape and answers unknowns (a) and (c).
4. The Railway volume and `RAPPI_CONFIG_DIR` get set up, Gabriel runs `push-railway`, the change merges through
   `/end-worktree`, and the deploy follows.
5. MCP `auth_status` shows `auto_refresh: true`. The token is expected to renew itself about 2026-10-03, and the chief
   confirms that it did.
