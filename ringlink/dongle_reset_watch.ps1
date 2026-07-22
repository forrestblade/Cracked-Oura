# Polled watcher for unattended dongle self-healing (no elevation needed at
# trigger time). The RingDongleReset scheduled task runs THIS script every
# 5 minutes elevated; any non-elevated process (the ring daemon) requests a
# USB reset simply by touching dongle_reset.request next to this script.
#
# Why: a task created from an elevated context cannot be started or even
# queried by a non-elevated process (`schtasks /Run` -> Access is denied),
# so direct on-demand triggering silently never worked. A time-triggered
# task + flag file has the same effect with <=5 min latency.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$flag = Join-Path $here 'dongle_reset.request'
if (-not (Test-Path $flag)) { exit 0 }
Remove-Item $flag -Force
& (Join-Path $here 'dongle_usb_reset.ps1')
exit $LASTEXITCODE
