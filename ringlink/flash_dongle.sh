#!/usr/bin/env bash
# Flash the nRF52840 USB dongle (PCA10059) with the SD API v5 connectivity firmware
# that pc-ble-driver-py 0.17.0 requires.
#
# BEFORE RUNNING: press the dongle's RESET (SW1, sideways button) so it enters the
# Open DFU bootloader — the red LED pulses and USB re-enumerates as PID 0x521F.
# The COM port may CHANGE in bootloader mode; this script auto-detects it.
set -euo pipefail
cd "$(dirname "$0")"

NRFUTIL="nrf310/Scripts/nrfutil.exe"  # 6.1.7 (py3-compatible); nrfenv's 5.2.0 is py2-era and broken
PKG="firmware/connectivity_4.1.4_usb_with_s132_5.1.0_dfu_pkg.zip"

# Find the bootloader COM port (Open DFU bootloader = VID 1915, PID 521F)
PORT=$(powershell -NoProfile -Command \
  "(Get-PnpDevice -Class Ports -Status OK | Where-Object { \$_.InstanceId -match 'VID_1915&PID_521F' } | Select-Object -First 1 -ExpandProperty FriendlyName)" \
  | grep -oE 'COM[0-9]+' || true)

if [ -z "$PORT" ]; then
  echo "!! Open DFU bootloader not found (no VID_1915&PID_521F serial port)."
  echo "   Press the dongle's RESET (SW1) button — red LED should pulse — then re-run."
  powershell -NoProfile -Command "Get-PnpDevice -Class Ports -Status OK | Format-Table FriendlyName,InstanceId -AutoSize"
  exit 1
fi

echo ">> Bootloader on $PORT — flashing $PKG"
"$NRFUTIL" dfu usb-serial -pkg "$PKG" -p "$PORT"
echo ">> Done. Dongle should re-enumerate as connectivity firmware (PID 0xC00A)."
powershell -NoProfile -Command "Start-Sleep 3; Get-PnpDevice -Class Ports -Status OK | Format-Table FriendlyName,InstanceId -AutoSize"
