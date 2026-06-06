import asyncio, sys
from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else __import__("config").BANKS[0][1]

async def main():
    print(f"Connecting to {ADDR} ...")
    async with BleakClient(ADDR, timeout=20.0) as c:
        print(f"Connected: {c.is_connected}\n")
        for svc in c.services:
            print(f"[service] {svc.uuid}  {svc.description}")
            for ch in svc.characteristics:
                print(f"   [char] {ch.uuid}  props={','.join(ch.properties)}  handle={ch.handle}")
                for d in ch.descriptors:
                    print(f"      [desc] {d.uuid}  handle={d.handle}")

asyncio.run(main())
