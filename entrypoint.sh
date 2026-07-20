#!/bin/bash
# entrypoint.sh - remap app user to host uid/gid and run CMD as app user
set -e

# remap app user UID and primary GID to match host so bind mount permissions
# work. `usermod -g` needs a group with that GID to already exist -- HOST_GID
# may collide with an existing group (e.g. GID 20 is "dialout" here and
# "staff" on macOS hosts) or may not exist at all yet, so create it first if
# no group claims that number.
if [ -n "$HOST_UID" ] && [ "$HOST_UID" != "$(id -u app)" ]; then
    usermod -u "$HOST_UID" app
fi
if [ -n "$HOST_GID" ] && [ "$HOST_GID" != "$(id -g app)" ]; then
    getent group "$HOST_GID" > /dev/null || groupadd -g "$HOST_GID" hostgroup
    usermod -g "$HOST_GID" app
fi

# re-own the home directory (uv install + dependency cache from build time)
# for the remapped uid so uv can reuse its cache
chown -R app: /home/app

# execute CMD as the app user (gosu preserves exported env vars)
exec gosu app "$@"
