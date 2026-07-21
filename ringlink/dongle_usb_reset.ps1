# Restart the Nordic nRF52840 connectivity dongle at the USB level.
# Equivalent to unplug/replug — clears the known pc-ble-driver H5 transport
# wedge (NRF_ERROR_SD_RPC_H5_TRANSPORT_STATE) without touching the hardware.
# Runs elevated via the RingDongleReset scheduled task (see install_dongle_reset_task.sh).
$dev = Get-PnpDevice | Where-Object {
    $_.InstanceId -match 'VID_1915&PID_C00A' -and $_.InstanceId -notmatch '&MI_'
} | Select-Object -First 1
if (-not $dev) {
    Write-Output 'RingDongleReset: no connectivity dongle (VID_1915 PID_C00A) found'
    exit 1
}
Write-Output "RingDongleReset: restarting $($dev.InstanceId)"
pnputil /restart-device $dev.InstanceId
exit $LASTEXITCODE
