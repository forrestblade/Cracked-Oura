#!/usr/bin/env bash
# One-time ADMIN setup: register the RingDongleReset scheduled task.
# The task runs dongle_usb_reset.ps1 as SYSTEM (USB replug-equivalent).
# After install, sync_ring.py can trigger it WITHOUT elevation via
# `schtasks /Run /TN RingDongleReset` whenever the dongle serial transport
# wedges — turning a "walk over and replug it" failure into self-healing.
#
# Run from an elevated Git Bash, or let it self-elevate via the UAC prompt.
set -e
cd "$(dirname "$0")"
HERE_WIN="$(cygpath -w "$(pwd)")"

TR_CMD="powershell.exe -NoProfile -ExecutionPolicy Bypass -File \\\"${HERE_WIN}\\dongle_usb_reset.ps1\\\""

# IMPORTANT: register under the CURRENT USER with /RL HIGHEST — not SYSTEM.
# A SYSTEM-owned task cannot be started (`schtasks /Run`) by a non-elevated
# process (Access is denied), which defeats unattended self-healing. A task
# owned by the user with highest run level CAN be triggered by that user's
# non-elevated processes, and still runs pnputil elevated.
USER_NAME="$(powershell.exe -NoProfile -Command '[System.Security.Principal.WindowsIdentity]::GetCurrent().Name' | tr -d '\r')"

# Try direct create (works if already elevated) …
if MSYS_NO_PATHCONV=1 schtasks /Create /TN "RingDongleReset" /SC ONCE /ST 00:00 \
    /RU "$USER_NAME" /RL HIGHEST /F /TR "$TR_CMD" 2>/dev/null; then
    echo "Installed: RingDongleReset (as $USER_NAME, highest)"
else
    # … otherwise self-elevate with a single UAC prompt.
    echo "Requesting admin (UAC prompt)…"
    MSYS_NO_PATHCONV=1 powershell.exe -NoProfile -Command \
      "Start-Process schtasks -ArgumentList '/Create','/TN','RingDongleReset','/SC','ONCE','/ST','00:00','/RU','${USER_NAME}','/RL','HIGHEST','/F','/TR','\"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\"${HERE_WIN}\\dongle_usb_reset.ps1\"\"\"' -Verb RunAs -Wait"
    echo "Installed: RingDongleReset (via UAC)"
fi
MSYS_NO_PATHCONV=1 schtasks /Query /TN "RingDongleReset" 2>&1 | head -5
