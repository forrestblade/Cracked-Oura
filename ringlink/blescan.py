import asyncio
from bleak import BleakScanner

OURA_SVC = "98ed0001-a541-11e4-b6a0-0002a5d5c51b"

async def main(seconds=15):
    print(f"Scanning {seconds}s for ALL BLE advertisers...\n")
    seen = {}

    def cb(dev, adv):
        seen[dev.address] = (adv.local_name or dev.name or "(no name)",
                             adv.rssi, list(adv.service_uuids or []))

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not seen:
        print("No BLE devices seen at all.")
        return
    rows = sorted(seen.items(), key=lambda kv: kv[1][1], reverse=True)
    for addr, (name, rssi, svcs) in rows:
        oura = "  <-- OURA SERVICE" if any(OURA_SVC.lower() == s.lower() for s in svcs) else ""
        looks = "  <-- name looks like Oura" if "oura" in name.lower() else ""
        print(f"{addr}  {rssi:>4} dBm  {name!r}{oura}{looks}")
        if svcs:
            print(f"        services: {svcs}")
    print(f"\nTotal: {len(seen)} devices")

asyncio.run(main())
