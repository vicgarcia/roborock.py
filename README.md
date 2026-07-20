# roborock.py

A python CLI tool for controlling your Roborock robotic vacuum built using the [python-roborock](https://github.com/Python-roborock/python-roborock) library, plus a containerized cron runner for scheduled cleaning tasks beyond what the Roborock app supports.

## Features

- **Single-file executable** - Uses `uv run --script` with inline dependencies ([PEP 723](https://peps.python.org/pep-0723/))
- **Cloud control** - Connect to your vacuum from anywhere via the Roborock cloud MQTT API
- **Room cleaning** - Clean specific rooms with control of passes, fan power, and water level
- **Device management** - List devices, check status, view rooms and supported modes
- **Token caching** - Interactive login once, cached tokens forever (survives the two-step email verification requirement)
- **Cron container** - Docker + [Supercronic](https://github.com/aptible/supercronic) for specialized cleaning schedules

## Prerequisites

- Python 3.12+ and [uv](https://github.com/astral-sh/uv) (CLI)
- Docker and Docker Compose (scheduled cleaning container)
- A Roborock account with registered devices (the Roborock app, not Mi Home)

## Installation

```bash
# Clone and make executable
git clone https://github.com/vicgarcia/roborock.py.git
cd roborock.py
chmod +x roborock.py

# Run
./roborock.py --help

# Install
cp roborock.py ~/.local/bin
chmod +x ~/.local/bin/roborock.py
```

## Authentication

Roborock accounts commonly require two-step email verification when logging in from a new client, so start with the interactive login (once per machine):

```bash
export ROBOROCK_EMAIL="you@example.com"
./roborock.py login
# enter the verification code emailed to you
```

Tokens are cached to `~/.roborock.json` by default and are long-lived — every subsequent command uses the cache and never touches the login endpoints. Set `ROBOROCK_CACHE_DIR` to cache somewhere else (this is how the cron container gets its tokens):

```bash
ROBOROCK_CACHE_DIR=./data ./roborock.py login
```

Note: the Roborock cloud API is aggressively rate limited (logins: ~10/hour, home data: ~5/hour). The tool caches both tokens (`.roborock.json`) and home/device data (`.roborock.cache`) to stay well clear of the limits.

## Commands

| Command | Description |
|---------|-------------|
| `login` | Interactive login, caches tokens (run once) |
| `devices` | List all devices on your account |
| `status` | Show device status (state, battery, fan, water) |
| `rooms` | List rooms with their segment ids |
| `modes` | List fan power / water level modes your device supports |
| `clean` | Start a cleaning task (whole home or specific rooms) |
| `set` | Set fan power / water level without starting a clean |
| `pause` | Pause the current cleaning job |
| `resume` | Resume a paused job |
| `stop` | Stop the current job |
| `dock` | Return to the charging dock |

All device commands accept `--device NAME`, optional when your account has a single vacuum.

## Usage

### Check status, rooms, and modes
```bash
./roborock.py status
./roborock.py rooms
./roborock.py modes
```

### Start cleaning
```bash
# Whole-home clean
./roborock.py clean

# Clean specific rooms (names from `rooms`, case-insensitive; segment ids also work)
./roborock.py clean --rooms Kitchen Dining

# Two passes with max suction and no mopping
./roborock.py clean --rooms Kitchen --passes 2 --fan max --water off

# Gentle nighttime clean
./roborock.py clean --rooms Hallway --fan quiet --water low
```

### Control commands
```bash
./roborock.py pause
./roborock.py resume
./roborock.py stop
./roborock.py dock
```

### Set modes without cleaning
```bash
./roborock.py set --fan balanced --water medium
```

## Clean Command Options

| Option | Default | Description |
|--------|---------|-------------|
| `--rooms` | none (whole home) | Room names or segment ids, space-separated |
| `--passes` | 1 | Cleaning passes per room (1-3, room cleans only) |
| `--fan` | current | Fan power mode (see `modes` for your device's options) |
| `--water` | current | Water level mode (see `modes` for your device's options) |

Fan and water options are discovered dynamically from your device. A Q7 Max reports fan: `quiet`, `balanced`, `turbo`, `max`, `gentle` and water: `off`, `low`, `medium`, `high`.

## Scheduled Cleaning (Docker)

The repo includes a cron container that runs `roborock.py` commands on a schedule via [Supercronic](https://github.com/aptible/supercronic). The Roborock app only supports simple schedules — this is where specialized tasks live (multi-pass room cleans, different fan/water per job, weekday vs weekend routines).

```
Supercronic (cron) → roborock.py CLI → Roborock Cloud MQTT → Vacuum
```

The container installs `roborock.py` from the repo at build time (COPY, no downloads) and reads auth tokens from a bind-mounted directory. Setup:

```bash
# 1. generate tokens into ./data (one-time interactive login)
export ROBOROCK_EMAIL="you@example.com"
ROBOROCK_CACHE_DIR=./data ./roborock.py login

# 2. configure environment
cp .env.example .env
# edit .env: TZ, ROBOROCK_EMAIL, and HOST_UID/HOST_GID (id -u / id -g)

# 3. define the schedule
# edit crontab (examples included, all commented out)

# 4. build and run
docker compose up -d --build
```

The compose file binds `./data` to `/data` in the container (`ROBOROCK_CACHE_DIR=/data` is set in the image), so the container uses the tokens you generated locally and persists its home-data cache alongside them. To use a different directory, change the bind in `docker-compose.yml`.

At startup the entrypoint remaps the container's `app` user to the `HOST_UID`/`HOST_GID` from `.env`, then drops privileges via `gosu` — this keeps the bind-mounted `./data` readable/writable from both sides regardless of your host uid.

### Container Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Debian slim + uv + Supercronic + roborock.py (copied from repo) |
| `docker-compose.yml` | Service definition, binds `./data:/data` |
| `entrypoint.sh` | Remaps the container user to `HOST_UID`/`HOST_GID` for bind mount permissions |
| `crontab` | Schedule — edit times and tasks here |
| `.env` | `TZ`, `ROBOROCK_EMAIL`, `HOST_UID`, `HOST_GID` |
| `data/` | Token + device cache (gitignored, created by login) |

### Managing the Container

```bash
# build + start
docker compose up -d --build

# view logs (supercronic logs every job run)
docker compose logs -f

# run a command manually inside the container
docker compose exec roborock roborock.py status

# stop
docker compose down
```

After editing `crontab`, rebuild: `docker compose up -d --build`.

### Troubleshooting

**"no cached auth and no password"**: no tokens in the bound directory — run step 1 above and confirm `data/.roborock.json` exists.

**"Permission denied: '/data/.roborock.json'"**: the container user's uid doesn't match the file owner — set `HOST_UID`/`HOST_GID` in `.env` to your host user's values (`id -u` / `id -g`) and `docker compose up -d --force-recreate`.

**Rate limit errors**: the API allows ~5 home-data fetches/hour. The cache in `data/` avoids this — don't delete it or hammer manual commands with a cold cache.

**Tokens expired/invalid**: re-run `ROBOROCK_CACHE_DIR=./data ./roborock.py login` and restart the container.

**Clean didn't start**: `clean` refuses if the vacuum is already cleaning/returning — check `docker compose logs` for the state message.

## Agent Skill

This project includes a `SKILL.md` file for use with AI coding agents (Claude Code, etc.). The skill enables natural language control of your vacuum.

### Installation

```bash
# Create skills directory
mkdir -p /path/to/agent/skills

# Copy SKILL.md
cp /path/to/roborock.py/SKILL.md /path/to/agent/skills/roborock/SKILL.md

# Ensure roborock.py is executable and in PATH
chmod +x ~/.local/bin/roborock.py

# Set credentials (in bashrc, zshrc, ...)
export ROBOROCK_EMAIL="you@example.com"
```

### Usage with Claude Code

Add the skills directory to your agent configuration, then interact naturally:

> "Check the status of the vacuum"
> "Clean the kitchen twice with max suction"
> "Pause the vacuum and send it back to the dock"
> "What rooms can the vacuum clean?"

The agent reads `SKILL.md` to understand available commands, parameters, and how to interpret responses.

## References

- [python-roborock on GitHub](https://github.com/Python-roborock/python-roborock)
- [python-roborock API documentation](https://python-roborock.github.io/python-roborock/roborock.html)
- [python-roborock api commands reference](https://python-roborock.readthedocs.io/en/latest/api_commands.html)
- [PEP 723 - inline script metadata](https://peps.python.org/pep-0723/)
- [uv](https://github.com/astral-sh/uv)
- [Supercronic](https://github.com/aptible/supercronic)
