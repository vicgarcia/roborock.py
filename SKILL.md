---
name: roborock
description: Control Roborock robotic vacuums via cloud API. Start room or whole-home cleaning with control of passes, fan power, and water level. Check status, list rooms, pause/resume/stop/dock. Use when the user wants to control their robot vacuum.
compatibility: Requires 'roborock.py' script in PATH with ROBOROCK_EMAIL set and cached auth tokens (~/.roborock.json, created by 'roborock.py login').
---

# Roborock Vacuum Control

Control Roborock robotic vacuums via cloud API using the `roborock.py` CLI. Works with V1-protocol devices (S-series, Q-series including Q7 Max, and most others).

## Authentication

The account email comes from the environment:
```bash
export ROBOROCK_EMAIL="you@example.com"
```

Auth tokens are cached at `~/.roborock.json` and are long-lived. If no cache exists, a one-time interactive login is required (`roborock.py login` - prompts for an emailed verification code). Cached tokens can also be injected via the `ROBOROCK_AUTH` environment variable (contents of the json file).

Home/device data is cached at `~/.roborock.cache`. The Roborock cloud API is heavily rate limited - do not use `--no-cache` unless something is broken.

## Commands Reference

### List Devices
```bash
roborock.py devices
```
Lists all vacuums on the account with model, duid, and online status.

### Check Status
```bash
roborock.py status
```
Shows state (charging, cleaning, segment_cleaning, paused, returning_home, idle, ...), battery, fan power, water level, error state, and clean progress (time and area) when active.

### List Rooms
```bash
roborock.py rooms
```
Returns room names with their segment ids, e.g. `Kitchen (segment: 16)`. Room names come from the Roborock app.

### List Supported Modes
```bash
roborock.py modes
```
Shows the fan power and water level options supported by this specific device, with the current setting marked. Example from a Q7 Max: fan `quiet`, `balanced`, `turbo`, `max`, `gentle` / water `off`, `low`, `medium`, `high`.

### Start Cleaning
```bash
# Whole-home clean with current settings
roborock.py clean

# Clean specific rooms by name (case-insensitive, segment ids also accepted)
roborock.py clean --rooms Kitchen Dining

# Full control: 2 passes, max suction, no water
roborock.py clean --rooms Kitchen --passes 2 --fan max --water off

# Quiet evening clean with light mopping
roborock.py clean --rooms Hallway Living --fan quiet --water low
```

Options:
- `--rooms` - room names or segment ids, space-separated. Omit for whole-home clean.
- `--passes` - 1-3 cleaning passes per room (default 1). Room cleans only.
- `--fan` - fan power mode from `modes` output.
- `--water` - water level mode from `modes` output.

The command refuses to start if the device is already cleaning - stop or dock it first.

### Lifecycle Control
```bash
roborock.py pause    # pause the current job
roborock.py resume   # resume a paused job (picks the right resume type automatically)
roborock.py stop     # stop/cancel the current job
roborock.py dock     # return to the charging dock
```

### Set Modes Without Cleaning
```bash
roborock.py set --fan balanced --water medium
```
Changes fan/water without starting a job. Note: the device restores its own defaults for water level when a job completes.

## Multi-Device Accounts

All commands accept `--device NAME`. With a single vacuum on the account, `--device` can be omitted.

## Example Workflows

### Daily high-traffic clean
```bash
roborock.py clean --rooms Kitchen Hallway --passes 2 --fan balanced --water off
```

### Weekly deep clean
```bash
roborock.py clean --passes 1 --fan max --water medium   # whole home
```

### Check before starting (recommended for automation)
```bash
# only start if docked/idle - clean refuses if already active, so just chain
roborock.py status && roborock.py clean --rooms Kitchen --passes 2 --fan turbo
```

### Stop everything and recall
```bash
roborock.py stop && sleep 3 && roborock.py dock
```

## Tips

- Use `rooms` and `modes` first to learn names before composing clean commands
- Room name matching is case-insensitive ("kitchen" = "Kitchen")
- `--passes 2` is the sweet spot for dirty rooms; 3 passes takes a long time
- Fan/water set during `clean` persist for that job; water level reverts to the device default after the job ends
- The vacuum states meaning "busy": `cleaning`, `segment_cleaning`, `zoned_cleaning`, `spot_cleaning`, `returning_home`
- First run may take ~10 seconds for uv to install dependencies
- Cloud MQTT means commands work from anywhere - no LAN access to the vacuum required
