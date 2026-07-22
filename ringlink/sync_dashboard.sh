#!/usr/bin/env bash
# WATCHDOG entry point (Task Scheduler: RingLocalSync, every 15 min).
# The ring is handled by a PERSISTENT connection daemon (ring_daemon.py) —
# the architecture the ring firmware expects (the official app holds the
# link 24/7). This script only makes sure the daemon is alive; the daemon's
# own PID lock makes duplicate starts a no-op.
cd "$(dirname "$0")"

# ZOMBIE RECONCILIATION: the uv-venv python.exe is a trampoline that spawns
# the real interpreter as a SEPARATE process; killing one member of the pair
# leaves the other running the daemon loop with STALE in-memory code (seen
# 2026-07-22: 8 concurrent daemons, dongle contention + old-code ingests).
# Kill every ring_daemon process that is NOT the current lock holder.
LOCK_PID="$(cat .daemon.lock 2>/dev/null | tr -d '[:space:]')"
powershell -NoProfile -Command "
  \$lock = '${LOCK_PID:-0}'
  Get-CimInstance Win32_Process |
    Where-Object { \$_.Name -like 'python*' -and \$_.CommandLine -match 'ring_daemon' } |
    ForEach-Object {
      \$keep = (\$_.ProcessId -eq \$lock) -or (\$_.ParentProcessId -eq \$lock)
      # also keep the lock holder's trampoline parent
      if (\$lock -ne '0') {
        \$lp = Get-CimInstance Win32_Process -Filter \"ProcessId = \$lock\" -ErrorAction SilentlyContinue
        if (\$lp -and \$_.ProcessId -eq \$lp.ParentProcessId) { \$keep = \$true }
      }
      if (-not \$keep) { Stop-Process -Id \$_.ProcessId -Force; Write-Output ('reaped zombie daemon ' + \$_.ProcessId) }
    }" 2>/dev/null
# If the lock holder itself is gone, clear the stale lock so we can start.
if [ -n "$LOCK_PID" ] && ! kill -0 "$LOCK_PID" 2>/dev/null; then
    rm -f .daemon.lock
fi

# crude log rotation: keep daemon.log under ~5 MB
if [ -f daemon.log ] && [ "$(stat -c%s daemon.log 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 1048576 daemon.log > daemon.log.tmp && mv daemon.log.tmp daemon.log
fi

nohup ./venv310/Scripts/python.exe -u ring_daemon.py >> daemon.log 2>&1 &
disown
exit 0
