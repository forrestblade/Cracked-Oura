#!/usr/bin/env bash
# One-time ADMIN setup: register the RingDongleReset scheduled task.
#
# The task runs dongle_reset_watch.ps1 every 5 minutes (elevated). The
# watcher exits instantly unless ringlink/dongle_reset.request exists, in
# which case it deletes the flag and restarts the dongle at the USB level
# (pnputil — replug-equivalent, clears the pc-ble-driver H5 transport wedge).
#
# WHY POLLING, NOT ON-DEMAND: a task created from an elevated context cannot
# be started — or even queried — by a non-elevated process (`schtasks /Run`
# → Access is denied), regardless of /RU or /RL. So the daemon requests a
# reset by touching the flag file; worst-case healing latency is 5 minutes.
#
# Run from an elevated Git Bash, or let it self-elevate via the UAC prompt.
set -e
cd "$(dirname "$0")"
HERE_WIN="$(cygpath -w "$(pwd)")"

TR_CMD="powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"${HERE_WIN}\\dongle_reset_watch.ps1\""

create_task() {
    MSYS_NO_PATHCONV=1 schtasks /Create /TN "RingDongleReset" \
        /SC MINUTE /MO 5 /RU "SYSTEM" /RL HIGHEST /F /TR "$TR_CMD"
}

# Try direct create (works if already elevated) …
if create_task 2>/dev/null; then
    echo "Installed: RingDongleReset (SYSTEM, every 5 min, flag-file watcher)"
else
    # … otherwise self-elevate with a single UAC prompt.
    echo "Requesting admin (UAC prompt)…"
    MSYS_NO_PATHCONV=1 powershell.exe -NoProfile -Command \
      "Start-Process schtasks -ArgumentList '/Create','/TN','RingDongleReset','/SC','MINUTE','/MO','5','/RU','SYSTEM','/RL','HIGHEST','/F','/TR','\"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\"${HERE_WIN}\\dongle_reset_watch.ps1\"\"\"' -Verb RunAs -Wait"
    echo "Installed: RingDongleReset (via UAC)"
fi

echo "Test: touch dongle_reset.request — the dongle should re-enumerate within 5 min."
