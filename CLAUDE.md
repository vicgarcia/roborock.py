# CLAUDE.md — roborock.py implementation notes

## Architecture

Single-file PEP 723 script (`uv run --script`), Python 3.12+. All logic lives in `roborock.py`. The main class is `RoborockCLI`, which wraps `create_device_manager` from [python-roborock](https://github.com/Python-roborock/python-roborock) (`python-roborock>=5.31.0`).

This repo also contains the cron container for scheduled cleaning (Dockerfile, docker-compose.yml, crontab):

```
Supercronic (cron) → roborock.py CLI → Roborock Cloud MQTT → Vacuum
```

- The Dockerfile COPYs `roborock.py` from the repo into `~/.local/bin` (no downloads) and pre-warms uv deps with `roborock.py --help`
- `ROBOROCK_CACHE_DIR=/data` is set in the image; compose binds `./data:/data`, so the container uses tokens generated locally with `ROBOROCK_CACHE_DIR=./data ./roborock.py login`
- Bind mount permissions follow the groundskeeper pattern: `HOST_UID`/`HOST_GID` are set in `.env` (passed via `env_file`, no compose `environment` block needed), and `entrypoint.sh` (running as root) remaps the `app` user with `usermod` — creating the group first if the GID doesn't exist, since it may collide (GID 20 is "dialout" in debian, "staff" on macOS hosts) — re-owns `/home/app` so uv keeps its dep cache, then `exec gosu app "$@"`. The vars are named HOST_* (not UID/GID) because bash reserves `$UID` as readonly and would shadow an inherited UID env var in the entrypoint
- Crontab entries call `roborock.py` directly — no wrapper scripts
- The container never logs in (two-step verification is interactive-only); it only ever reads the bind-mounted tokens
- Cloud MQTT means no host networking (unlike the lifx container, which needs LAN discovery)

### Module name shadowing

The script is named `roborock.py`, which shadows the `roborock` package at import time. The header strips the script's own directory from `sys.path` (and pops any half-imported `roborock` from `sys.modules`) before importing the library. Do not remove this.

### Library entry points

```python
from roborock.web_api import RoborockApiClient          # login + web API (home data, rooms)
from roborock.devices.device_manager import create_device_manager, UserParams
```

Flow: login → `UserParams(username, user_data, base_url)` → `await create_device_manager(user_params, cache=..., prefer_cache=...)` → `manager.get_devices()` → per-device `device.v1_properties` traits (status, rooms, command). Cloud MQTT only, no LAN discovery needed.

## Authentication

### Two-step verification (the big one)

`pass_login(password)` fails with **response code 2031 ("need two-step validate")** for accounts with email verification enabled — which appears to be most accounts now. The interactive `login` command handles this:

1. `api.request_code_v4()` — emails a verification code
2. `input()` for the code
3. `api.code_login_v4(code)` — exchanges code for `UserData`

**Critical: both calls must happen on the same `RoborockApiClient` instance.** The client generates a random `_device_identifier` per instance (`secrets.token_urlsafe(16)`), the verification code is bound to it server-side, and a code requested by one instance is "invalid" to another.

**Use the v4 endpoints, not v1.** `code_login` (v1) fails with **code 3006 `RoborockInvalidUserAgreement`** ("user agreement must be accepted again") on accounts that haven't re-accepted the latest agreement in the app. `code_login_v4` submits the agreement version with the login and sails through.

### Token cache

- `$ROBOROCK_CACHE_DIR/.roborock.json` (default `~/.roborock.json`) — `{username, base_url, user_data}` where `user_data` is the full `UserData.as_dict()` (HTTP bearer token + `rriot` MQTT credential block). Long-lived; there is no documented expiry. Home Assistant reuses these for months.
- `ROBOROCK_CACHE_DIR` env var relocates both cache files — used by the container (`/data`) and for generating tokens into the repo's `./data` bind mount.
- `ROBOROCK_AUTH` env var — if the cache file is missing, its contents can be injected via env (written through to the file). Alternative to the bind mount for env-only deployments.
- **Never delete the cached tokens on connect failure.** Interactive re-login costs a human a verification code. `connect()` only overwrites the cache after a *successful* fresh login. An early version wiped the cache on any connect exception — that bug cost two logins during development.

### Rate limits (server-side, per account)

- login: 1/sec, 3/min, 10/hour, 20/day
- home data: 1/sec, 3/min, 5/hour, 40/day (`RoborockRateLimit` raised locally by the library's limiter too)

This is why both caches exist and why cron jobs must never use `--no-cache`.

## Device cache and the FileCache pickle bug

`create_device_manager(cache=...)` accepts the library's `FileCache` to persist home data between runs (avoiding the 5/hour home-data limit). But the library's `FileCache.flush()` truncates the target file *before* pickling, and after devices connect, `CacheData.device_info[].trait_data` can hold live channel callbacks (`V1Channel.rpc_channel.<locals>.rpc_strategies_cb`) that are **not picklable**. Result: a 0-byte cache file that crashes the next run with `EOFError: Ran out of input`.

`SafeFileCache` in roborock.py fixes both ends:
- `flush()` pickles in memory first; on failure it rebuilds a `CacheData` keeping only picklable fields (home_data is the critical one), then writes via temp file + `Path.replace` (atomic)
- `get()` discards a corrupt cache file and starts fresh instead of crashing

Cache file: `$ROBOROCK_CACHE_DIR/.roborock.cache` (default `~/.roborock.cache`). `flush()` is called from `stop()` in the `finally` of `run()`.

## V1 protocol notes (Q7 Max / roborock.vacuum.a38)

Despite the "Q7" name, the Q7 Max/Max+ (2022, model `a38`) is a **V1-protocol** device (`device.pv == "1.0"`), not the B01 protocol of the 2025 "Q7 L5/M5" (`sc*` models). All commands below are V1.

### Commands used (RoborockCommand enum → wire name)

- `APP_START` / `APP_PAUSE` / `APP_STOP` / `APP_CHARGE` — whole-home start, pause, stop, return to dock
- `APP_SEGMENT_CLEAN` with `params=[{"segments": [16, 19], "repeat": 2}]` — room clean; `repeat` is the pass count (1-3)
- `RESUME_SEGMENT_CLEAN` / `RESUME_ZONED_CLEAN` — resume must match the interrupted job type; `status.in_cleaning` tells you which (1=global → `APP_START`, 2=zone, 3=segment)
- `SET_CUSTOM_MODE` with `params=[code]` — fan power
- `SET_WATER_BOX_CUSTOM_MODE` with `params=[code]` — water level

### Dynamic mode discovery

Never hardcode fan/water codes. `StatusTrait` exposes per-device mappings derived from `DeviceFeaturesTrait`:
- `status.fan_speed_mapping: dict[int, str]` — Q7 Max: 101 quiet, 102 balanced, 103 turbo, 104 max, 105 gentle
- `status.water_mode_mapping: dict[int, str]` — Q7 Max: 200 off, 201 low, 202 medium, 203 high, 207 custom_water_flow

`_resolve_mode()` matches user input against mapping values case-insensitively (also accepts raw codes).

### Rooms

`device.v1_properties.rooms.refresh()` sends `get_room_mapping` and backfills human names from the web API. Each entry is a `NamedRoomMapping` with `.segment_id` (int used in `APP_SEGMENT_CLEAN`) and `.name`.

### Behavioral quirks observed on real hardware

- Water level reverts to the device's own default when a job ends (we set `off` for a test clean; after stop+dock the device reported `high` again). Fan power persists.
- `clean_area` is reported in mm²; divide by 1e6 for m².
- Status refresh right after `app_segment_clean` reflects the new state within ~5 seconds.
- The Q7 Max reports state `charging` when docked, `charging_complete` never observed (100% still shows `charging`).

## Test device

- `Q7 Max` — model `roborock.vacuum.a38`, duid `455ktGpJyMugzLfjUnNOkV`
- Rooms: Kitchen (16), Passage (17), Dining (18), Hallway (19), Master (21), Living (22)

## IDE import warning

The IDE warns that `roborock.*` imports can't be resolved. Expected — the package is installed at runtime by `uv run --script`, not in the local environment.
