#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "python-roborock>=5.31.0",
# ]
# ///

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# this script is named roborock.py, which shadows the roborock package on
# sys.path - drop the script's own directory so the real package imports
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _script_dir]
sys.modules.pop('roborock', None)

import pickle

from roborock.data import UserData
from roborock.devices.cache import CacheData, DeviceCacheData
from roborock.devices.device_manager import DeviceManager, UserParams, create_device_manager
from roborock.devices.file_cache import FileCache
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

# setup logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# silence noisy mqtt/asyncio loggers
for _noisy in ["paho", "paho.mqtt", "asyncio", "aiohttp", "roborock"]:
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# auth cache (login tokens) and device cache (home data, device features)
# roborock's cloud api is aggressively rate limited (login: 10/hour,
# home data: 5/hour) so both caches are essential for cron use.
# ROBOROCK_CACHE_DIR overrides the location (e.g. a container volume)
CACHE_DIR = Path(os.environ.get('ROBOROCK_CACHE_DIR') or Path.home())
AUTH_CACHE_FILE = CACHE_DIR / '.roborock.json'
DEVICE_CACHE_FILE = CACHE_DIR / '.roborock.cache'

# clean_area is reported in mm^2
SQM_PER_SQMM = 1e-6
SQFT_PER_SQM = 10.764

# states where starting a new cleaning task makes sense
ACTIVE_STATES = {
    'cleaning', 'returning_home', 'spot_cleaning', 'going_to_target',
    'zoned_cleaning', 'segment_cleaning', 'manual_mode', 'remote_control_active',
}


def _pickle_or_none(value):
    """return value if picklable, else None."""
    try:
        pickle.dumps(value)
        return value
    except Exception:
        return None


class SafeFileCache(FileCache):
    """FileCache with atomic writes and corruption recovery.

    The library's FileCache truncates the target file before pickling. After
    devices connect, CacheData can hold live channel callbacks that are not
    picklable, so the flush raises mid-write and leaves a 0-byte file that
    crashes the next run with EOFError. Here we serialize in memory first,
    salvage the picklable fields (home_data is the critical one - it is
    heavily rate limited server-side), and write via temp file + rename.
    """

    def __init__(self, file_path: Path) -> None:
        super().__init__(file_path)
        self._path = file_path

    async def get(self) -> CacheData:
        try:
            return await super().get()
        except Exception as e:
            logger.debug("discarding corrupt device cache: %s", e)
            self._path.unlink(missing_ok=True)
            self._cache_data = None
            return await super().get()

    async def flush(self) -> None:
        if self._cache_data is None:
            return
        try:
            data = pickle.dumps(self._cache_data)
        except Exception:
            cache = self._cache_data
            clean = CacheData(home_data=_pickle_or_none(cache.home_data))
            for duid, dev in (cache.device_info or {}).items():
                clean.device_info[duid] = DeviceCacheData(
                    network_info=_pickle_or_none(dev.network_info),
                    home_map_info=_pickle_or_none(dev.home_map_info),
                    home_map_content_base64=_pickle_or_none(dev.home_map_content_base64),
                    device_features=_pickle_or_none(dev.device_features),
                    trait_data=_pickle_or_none(dev.trait_data),
                )
            try:
                data = pickle.dumps(clean)
            except Exception as e:
                logger.debug("device cache not picklable, skipping flush: %s", e)
                return
        tmp = self._path.with_suffix('.tmp')
        tmp.write_bytes(data)
        tmp.replace(self._path)


class RoborockCLI:
    """CLI wrapper around the python-roborock device manager."""

    def __init__(self):
        self._manager: DeviceManager | None = None
        self._device_cache: FileCache | None = None

    # === auth cache ===

    def _save_auth_cache(self, username: str, user_data: UserData, base_url: str | None) -> None:
        try:
            AUTH_CACHE_FILE.write_text(json.dumps({
                'username': username,
                'base_url': base_url,
                'user_data': user_data.as_dict(),
            }, indent=2))
            AUTH_CACHE_FILE.chmod(0o600)
        except Exception as e:
            logger.debug("failed to save auth cache: %s", e)

    def _load_auth_cache(self, username: str) -> UserParams | None:
        if not AUTH_CACHE_FILE.exists():
            # allow injecting the auth json via env (e.g. container secrets);
            # persist it to the cache file so subsequent runs work normally
            auth_env = os.environ.get('ROBOROCK_AUTH')
            if not auth_env:
                return None
            try:
                AUTH_CACHE_FILE.write_text(auth_env)
                AUTH_CACHE_FILE.chmod(0o600)
            except Exception as e:
                logger.warning("failed to persist ROBOROCK_AUTH: %s", e)
                return None
        try:
            data = json.loads(AUTH_CACHE_FILE.read_text())
            if data.get('username') != username:
                return None
            return UserParams(
                username=username,
                user_data=UserData.from_dict(data['user_data']),
                base_url=data.get('base_url'),
            )
        except Exception as e:
            logger.warning("failed to load auth cache: %s", e)
            return None

    # === login / connect ===

    async def _fresh_login(self, email: str, password: str) -> UserParams:
        """login with email/password and cache the resulting tokens.

        roborock accounts frequently require two-step email verification
        (response code 2031) when logging in from a new client - that flow is
        interactive, so it lives in cmd_login. here we surface a helpful error.
        """
        if not password:
            raise RuntimeError("cached tokens are missing or invalid and no password is set - run 'roborock.py login' to re-authenticate")
        api = RoborockApiClient(username=email)
        try:
            user_data = await api.pass_login(password)
        except Exception as e:
            if '2031' in str(e) or 'two-step' in str(e):
                raise RuntimeError("account requires two-step email verification - run 'roborock.py login' once to authenticate interactively") from e
            raise
        base_url = await api.base_url
        self._save_auth_cache(email, user_data, base_url)
        return UserParams(username=email, user_data=user_data, base_url=base_url)

    async def cmd_login(self, email: str, password: str | None) -> int:
        """interactive login - handles the two-step email verification flow.

        run this once on a new machine; tokens are cached to ~/.roborock.json
        and every other command (including cron use) rides the cache.
        """
        api = RoborockApiClient(username=email)

        user_data = None
        if password:
            try:
                user_data = await api.pass_login(password)
                print("password login succeeded")
            except Exception as e:
                if '2031' not in str(e) and 'two-step' not in str(e):
                    print(f"login failed: {e}")
                    return 1
                print("account requires two-step email verification")

        if user_data is None:
            # v4 endpoints submit the user agreement version with the login,
            # avoiding "user agreement must be accepted again" (code 3006).
            # request + exchange must happen on this same client instance -
            # the code is bound to the client's random device id server-side.
            try:
                await api.request_code_v4()
                print(f"verification code sent to {email}")
                code = input("enter code: ").strip()
                user_data = await api.code_login_v4(code)
            except Exception as e:
                print(f"login failed: {e}")
                return 1

        base_url = await api.base_url
        self._save_auth_cache(email, user_data, base_url)
        print(f"login successful - tokens cached to {AUTH_CACHE_FILE}")
        return 0

    async def connect(self, email: str, password: str, use_cache: bool = True) -> bool:
        """login (cached tokens preferred) and connect to devices via mqtt."""
        user_params = self._load_auth_cache(email) if use_cache else None
        from_cache = user_params is not None

        if user_params is None:
            try:
                user_params = await self._fresh_login(email, password)
            except Exception as e:
                print(f"login failed: {e}")
                return False

        self._device_cache = SafeFileCache(DEVICE_CACHE_FILE)
        try:
            self._manager = await create_device_manager(user_params, cache=self._device_cache, prefer_cache=use_cache)
            return True
        except Exception as e:
            logger.debug("connect failed", exc_info=True)
            if not from_cache or not password:
                # nothing else to try - never wipe cached tokens here, they
                # may still be good (transient network/rate-limit errors)
                print(f"failed to connect: {e}")
                return False
            # cached tokens may be stale - retry with a fresh login. only a
            # successful login overwrites the cached tokens.
            try:
                user_params = await self._fresh_login(email, password)
                DEVICE_CACHE_FILE.unlink(missing_ok=True)
                self._device_cache = SafeFileCache(DEVICE_CACHE_FILE)
                self._manager = await create_device_manager(user_params, cache=self._device_cache, prefer_cache=False)
                return True
            except Exception as e2:
                print(f"failed to connect: {e2}")
                return False

    async def stop(self) -> None:
        if self._device_cache:
            try:
                await self._device_cache.flush()
            except Exception as e:
                logger.debug("failed to flush device cache: %s", e)
        if self._manager:
            try:
                await self._manager.close()
            except Exception as e:
                logger.debug("failed to close device manager: %s", e)

    # === device lookup ===

    async def find_device(self, name: str | None):
        """find a v1 vacuum by name, or the only one if name not given."""
        devices = await self._manager.get_devices()
        vacuums = [d for d in devices if d.v1_properties is not None]

        if not vacuums:
            print("no supported vacuums found on this account")
            return None

        if name is None:
            if len(vacuums) == 1:
                return vacuums[0]
            print("multiple devices found, specify one with --device:")
            for dev in vacuums:
                print(f"  {dev.name}")
            return None

        for dev in vacuums:
            if dev.name.lower() == name.lower():
                return dev

        print(f"device not found: {name}")
        print(f"available devices: {', '.join(d.name for d in vacuums)}")
        return None

    # === mode resolution ===

    @staticmethod
    def _resolve_mode(value: str, mapping: dict[int, str], label: str) -> int | None:
        """resolve a friendly mode name (or raw code) to its device code."""
        for code, mode_name in mapping.items():
            if mode_name.lower().replace(' ', '_') == value.lower().replace(' ', '_').replace('-', '_'):
                return code
        if value.isdigit() and int(value) in mapping:
            return int(value)
        options = ', '.join(sorted(mapping.values()))
        print(f"error: unknown {label} '{value}' (options: {options})")
        return None

    # === command handlers ===

    async def cmd_devices(self, args) -> None:
        devices = await self._manager.get_devices()

        print("Roborock Devices:")
        print()

        if not devices:
            print("  No devices found.")
        else:
            for dev in devices:
                supported = "" if dev.v1_properties is not None else "  [unsupported protocol]"
                connected = "online" if dev.is_connected else "offline"
                print(f"  {dev.name}{supported}")
                print(f"    model: {dev.product.model}  duid: {dev.duid}  status: {connected}")


    async def cmd_status(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        status = device.v1_properties.status
        await status.refresh()

        print(f"Status for {device.name}:")
        print()
        print(f"  State: {status.state_name or 'unknown'}")
        if status.battery is not None:
            print(f"  Battery: {status.battery}%")
        if status.fan_power is not None:
            print(f"  Fan power: {status.fan_speed_name or status.fan_power}")
        if status.water_box_mode is not None:
            print(f"  Water level: {status.water_mode_name or status.water_box_mode}")
        if status.error_code is not None and status.error_code != 0:
            print(f"  Error: {status.error_code.name if hasattr(status.error_code, 'name') else status.error_code}")

        if status.state_name in ACTIVE_STATES or status.state_name == 'paused':
            if status.clean_time:
                mins, secs = divmod(status.clean_time, 60)
                print(f"  Clean time: {mins}m {secs}s")
            if status.clean_area:
                sqm = status.clean_area * SQM_PER_SQMM
                print(f"  Clean area: {sqm:.1f} m2 ({sqm * SQFT_PER_SQM:.0f} ft2)")

        if status.water_shortage_status:
            print("  Warning: water tank is low/empty")

    async def cmd_rooms(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        rooms_trait = device.v1_properties.rooms
        await rooms_trait.refresh()

        print(f"Rooms for {device.name}:")
        print()

        if not rooms_trait.rooms:
            print("  No rooms found. Create rooms/maps in the Roborock app first.")
        else:
            for room in sorted(rooms_trait.rooms, key=lambda r: r.name):
                print(f"  {room.name} (segment: {room.segment_id})")


    async def cmd_clean(self, args) -> None:
        if args.passes < 1 or args.passes > 3:
            print(f"error: passes must be between 1 and 3 (got {args.passes})")
            return

        device = await self.find_device(args.device)
        if not device:
            return

        props = device.v1_properties
        status = props.status
        await status.refresh()

        if status.state_name in ACTIVE_STATES:
            print(f"cannot start cleaning: device is {status.state_name}")
            print("stop or dock the current job first (roborock.py stop / dock)")
            return

        # resolve fan/water modes against this device's supported options
        fan_code = None
        if args.fan:
            fan_code = self._resolve_mode(args.fan, status.fan_speed_mapping, "fan mode")
            if fan_code is None:
                return

        water_code = None
        if args.water:
            water_code = self._resolve_mode(args.water, status.water_mode_mapping, "water level")
            if water_code is None:
                return

        # resolve rooms to segment ids
        segment_ids = []
        if args.rooms:
            rooms_trait = props.rooms
            await rooms_trait.refresh()
            available = rooms_trait.rooms or []

            for room_input in args.rooms:
                matched = None
                for room in available:
                    if room.name.lower() == room_input.lower() or str(room.segment_id) == room_input:
                        matched = room
                        break
                if not matched:
                    print(f"error: room '{room_input}' not found")
                    print(f"available rooms: {', '.join(sorted(r.name for r in available))}")
                    return
                segment_ids.append(matched.segment_id)
                print(f"  - {matched.name} (segment: {matched.segment_id})")

        try:
            if fan_code is not None:
                await props.command.send(RoborockCommand.SET_CUSTOM_MODE, params=[fan_code])
                print(f"fan power set to {status.fan_speed_mapping[fan_code]}")

            if water_code is not None:
                await props.command.send(RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, params=[water_code])
                print(f"water level set to {status.water_mode_mapping[water_code]}")

            if segment_ids:
                await props.command.send(RoborockCommand.APP_SEGMENT_CLEAN, params=[{'segments': segment_ids, 'repeat': args.passes}])
                passes_note = f" x{args.passes} passes" if args.passes > 1 else ""
                print(f"started cleaning {len(segment_ids)} room(s){passes_note} on {device.name}")
            else:
                if args.passes > 1:
                    print("note: --passes only applies to room cleaning, ignoring for whole-home clean")
                await props.command.send(RoborockCommand.APP_START)
                print(f"started whole-home clean on {device.name}")

        except Exception as e:
            logger.exception("clean command error")
            print(f"clean command failed: {e}")

    async def cmd_pause(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        status = device.v1_properties.status
        await status.refresh()

        if status.state_name not in ACTIVE_STATES:
            print(f"cannot pause: device is {status.state_name or 'unknown'}")
            return

        try:
            await device.v1_properties.command.send(RoborockCommand.APP_PAUSE)
            print(f"paused {device.name}")
        except Exception as e:
            print(f"pause failed: {e}")

    async def cmd_resume(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        status = device.v1_properties.status
        await status.refresh()

        if status.state_name != 'paused':
            print(f"cannot resume: device is {status.state_name or 'unknown'}")
            return

        # resume with the command matching the interrupted job type
        in_cleaning = int(status.in_cleaning) if status.in_cleaning is not None else 0
        if in_cleaning == 3:
            cmd = RoborockCommand.RESUME_SEGMENT_CLEAN
        elif in_cleaning == 2:
            cmd = RoborockCommand.RESUME_ZONED_CLEAN
        else:
            cmd = RoborockCommand.APP_START

        try:
            await device.v1_properties.command.send(cmd)
            print(f"resumed cleaning on {device.name}")
        except Exception as e:
            print(f"resume failed: {e}")

    async def cmd_stop(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        try:
            await device.v1_properties.command.send(RoborockCommand.APP_STOP)
            print(f"stopped {device.name}")
        except Exception as e:
            print(f"stop failed: {e}")

    async def cmd_dock(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        try:
            await device.v1_properties.command.send(RoborockCommand.APP_CHARGE)
            print(f"{device.name} returning to dock")
        except Exception as e:
            print(f"dock failed: {e}")

    async def cmd_set(self, args) -> None:
        if not args.fan and not args.water:
            print("error: specify --fan and/or --water (see: roborock.py modes)")
            return

        device = await self.find_device(args.device)
        if not device:
            return

        props = device.v1_properties
        status = props.status
        await status.refresh()

        fan_code = None
        if args.fan:
            fan_code = self._resolve_mode(args.fan, status.fan_speed_mapping, "fan mode")
            if fan_code is None:
                return

        water_code = None
        if args.water:
            water_code = self._resolve_mode(args.water, status.water_mode_mapping, "water level")
            if water_code is None:
                return

        try:
            if fan_code is not None:
                await props.command.send(RoborockCommand.SET_CUSTOM_MODE, params=[fan_code])
                print(f"fan power set to {status.fan_speed_mapping[fan_code]}")
            if water_code is not None:
                await props.command.send(RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, params=[water_code])
                print(f"water level set to {status.water_mode_mapping[water_code]}")
        except Exception as e:
            print(f"set failed: {e}")

    async def cmd_modes(self, args) -> None:
        device = await self.find_device(args.device)
        if not device:
            return

        status = device.v1_properties.status
        await status.refresh()

        print(f"Supported modes for {device.name}:")
        print()
        print("  Fan power (--fan):")
        for code, mode_name in status.fan_speed_mapping.items():
            current = "  <- current" if status.fan_power == code else ""
            print(f"    {mode_name} ({code}){current}")
        print()
        print("  Water level (--water):")
        for code, mode_name in status.water_mode_mapping.items():
            current = "  <- current" if status.water_box_mode == code else ""
            print(f"    {mode_name} ({code}){current}")

    # === run ===

    async def run(self, args) -> int:
        email = args.email or os.environ.get('ROBOROCK_EMAIL')
        password = args.password or os.environ.get('ROBOROCK_PASSWORD')

        if not email:
            print("error: email required (via --email or ROBOROCK_EMAIL env var)")
            return 1

        # login is special: interactive, password optional (email code flow)
        if args.command == 'login':
            return await self.cmd_login(email, password)

        # password only needed for fresh logins - cached tokens (file or ROBOROCK_AUTH env) suffice for normal operation
        if not password and self._load_auth_cache(email) is None:
            print("error: no cached auth and no password - run 'roborock.py login' or set ROBOROCK_PASSWORD / ROBOROCK_AUTH")
            return 1

        use_cache = not getattr(args, 'no_cache', False)
        if not await self.connect(email, password, use_cache=use_cache):
            return 1

        try:
            if hasattr(args, 'func'):
                print()  # output always starts on a fresh line after the command
                await args.func(args)
            return 0
        finally:
            await self.stop()


def main():
    parser = argparse.ArgumentParser(description='roborock vacuum control cli')
    parser.add_argument('-e', '--email', help='account email (or set ROBOROCK_EMAIL)')
    parser.add_argument('-p', '--password', help='account password (or set ROBOROCK_PASSWORD)')
    parser.add_argument('--no-cache', action='store_true', help='skip cached auth and home data, force fresh login')
    parser.add_argument('--verbose', '-v', action='store_true', help='enable debug logging')

    subparsers = parser.add_subparsers(dest='command', help='commands')

    # login command (interactive, handles two-step email verification)
    subparsers.add_parser('login', help='authenticate and cache tokens (run once, interactive)')

    # devices command
    devices_parser = subparsers.add_parser('devices', help='list all devices')
    devices_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_devices(args))

    # status command
    status_parser = subparsers.add_parser('status', help='show device status')
    status_parser.add_argument('--device', help='device name (optional if only one device)')
    status_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_status(args))

    # rooms command
    rooms_parser = subparsers.add_parser('rooms', help='list rooms with segment ids')
    rooms_parser.add_argument('--device', help='device name (optional if only one device)')
    rooms_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_rooms(args))

    # modes command
    modes_parser = subparsers.add_parser('modes', help='list supported fan/water modes')
    modes_parser.add_argument('--device', help='device name (optional if only one device)')
    modes_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_modes(args))

    # set command
    set_parser = subparsers.add_parser('set', help='set fan power / water level without cleaning')
    set_parser.add_argument('--device', help='device name (optional if only one device)')
    set_parser.add_argument('--fan', help='fan power mode (see: roborock.py modes)')
    set_parser.add_argument('--water', help='water level mode (see: roborock.py modes)')
    set_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_set(args))

    # clean command
    clean_parser = subparsers.add_parser('clean', help='start a cleaning task')
    clean_parser.add_argument('--device', help='device name (optional if only one device)')
    clean_parser.add_argument('--rooms', nargs='+', help='room names or segment ids (omit for whole-home clean)')
    clean_parser.add_argument('--passes', type=int, default=1, help='number of cleaning passes per room (1-3, default: 1)')
    clean_parser.add_argument('--fan', help='fan power mode (see: roborock.py modes)')
    clean_parser.add_argument('--water', help='water level mode (see: roborock.py modes)')
    clean_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_clean(args))

    # pause command
    pause_parser = subparsers.add_parser('pause', help='pause current cleaning job')
    pause_parser.add_argument('--device', help='device name (optional if only one device)')
    pause_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_pause(args))

    # resume command
    resume_parser = subparsers.add_parser('resume', help='resume paused cleaning job')
    resume_parser.add_argument('--device', help='device name (optional if only one device)')
    resume_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_resume(args))

    # stop command
    stop_parser = subparsers.add_parser('stop', help='stop current cleaning job')
    stop_parser.add_argument('--device', help='device name (optional if only one device)')
    stop_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_stop(args))

    # dock command
    dock_parser = subparsers.add_parser('dock', help='return to dock')
    dock_parser.add_argument('--device', help='device name (optional if only one device)')
    dock_parser.set_defaults(func=lambda ctl: lambda args: ctl.cmd_dock(args))

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        for _name in ["roborock", "paho", "paho.mqtt", "aiohttp"]:
            logging.getLogger(_name).setLevel(logging.DEBUG)

    client = RoborockCLI()

    if hasattr(args, 'func'):
        args.func = args.func(client)

    return asyncio.run(client.run(args))


if __name__ == '__main__':
    sys.exit(main())
