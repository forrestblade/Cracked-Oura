#!/usr/bin/env bash
# WATCHDOG entry point (Task Scheduler: RingLocalSync, every 15 min).
# The ring is handled by a PERSISTENT connection daemon (ring_daemon.py) —
# the architecture the ring firmware expects (the official app holds the
# link 24/7). This script only makes sure the daemon is alive; the daemon's
# own PID lock makes duplicate starts a no-op.
cd "$(dirname "$0")"

# crude log rotation: keep daemon.log under ~5 MB
if [ -f daemon.log ] && [ "$(stat -c%s daemon.log 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 1048576 daemon.log > daemon.log.tmp && mv daemon.log.tmp daemon.log
fi

nohup ./venv310/Scripts/python.exe -u ring_daemon.py >> daemon.log 2>&1 &
disown
exit 0
