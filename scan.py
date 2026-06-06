"""Scan for nearby BLE devices and flag likely JK-BMS units.

Close the JK BMS phone app first - the BMS allows only one BLE client. Then:
    python scan.py
Copy the address of your BMS into config.py (the BANKS list).
"""
import asyncio
from bleak import BleakScanner


def looks_like_jk(name, uuids):
    n = (name or "").lower()
    # JK device names are user-set (e.g. "314 A/h - PC"), so name matching is weak;
    # the reliable tell is the 0xffe0 UART-like service the JK BMS advertises.
    return ("jk" in n or "bms" in n or "a/h" in n
            or any(u.lower().startswith("0000ffe0") for u in uuids))


async def main():
    print("Scanning 12s for BLE devices (close the JK phone app first)...\n")
    devices = await BleakScanner.discover(timeout=12.0, return_adv=True)
    rows = sorted(
        ((adv.rssi, addr, dev.name or adv.local_name or "?", adv.service_uuids or [])
         for addr, (dev, adv) in devices.items()),
        reverse=True,
    )

    print(f"{'RSSI':>5}  {'ADDRESS':17}  NAME")
    candidates = []
    for rssi, addr, name, uuids in rows:
        jk = looks_like_jk(name, uuids)
        print(f"{rssi:>5}  {addr:17}  {name[:28]}{'   <-- likely JK-BMS' if jk else ''}")
        if jk:
            candidates.append((addr, name))

    if candidates:
        print("\nLikely JK-BMS found. Paste into config.py (BANKS):")
        for i, (addr, name) in enumerate(candidates, 1):
            print(f'    ("bank{i}", "{addr}"),   # {name}')
    else:
        print("\nNo obvious JK-BMS in range. Check the phone app is closed and the BMS is awake.")


asyncio.run(main())
