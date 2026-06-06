import asyncio, sys
from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else __import__("config").BANKS[0][1]
CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"

DEV_INFO = {
    "00002a29": "Manufacturer", "00002a24": "Model", "00002a25": "Serial",
    "00002a27": "HW Rev", "00002a26": "FW Rev", "00002a28": "SW Rev",
    "00002a23": "System ID",
}

def jk_cmd(cmd, value=0):
    f = bytearray(20)
    f[0:4] = bytes([0xAA, 0x55, 0x90, 0xEB])
    f[4] = cmd
    f[5] = 0x00
    f[6] = value & 0xFF
    f[19] = sum(f[0:19]) & 0xFF
    return bytes(f)

async def main():
    chunks = []
    def on_notify(_, data: bytearray):
        chunks.append(bytes(data))

    async with BleakClient(ADDR, timeout=20.0) as c:
        print(f"Connected: {c.is_connected}\n--- Device Information ---")
        for uuid_short, label in DEV_INFO.items():
            full = f"0000{uuid_short[4:]}-0000-1000-8000-00805f9b34fb"
            try:
                raw = await c.read_gatt_char(full)
                try: val = raw.decode("utf-8").strip("\x00")
                except: val = raw.hex()
                print(f"  {label:14}: {val}")
            except Exception as e:
                print(f"  {label:14}: <err {e}>")

        # standard Battery Level (SoC %)
        try:
            soc = await c.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
            print(f"  {'SoC (0x2a19)':14}: {soc[0]} %")
        except Exception as e:
            print(f"  SoC: <err {e}>")

        print("\n--- JK frame capture ---")
        await c.start_notify(CHAR, on_notify)
        await c.write_gatt_char(CHAR, jk_cmd(0x97), response=False)  # device info
        await asyncio.sleep(1.5)
        await c.write_gatt_char(CHAR, jk_cmd(0x96), response=False)  # cell info
        await asyncio.sleep(3.0)
        await c.stop_notify(CHAR)

    buf = b"".join(chunks)
    print(f"Captured {len(chunks)} notifications, {len(buf)} bytes total")
    # locate JK response header 0x55AAEB90
    hdr = bytes([0x55, 0xAA, 0xEB, 0x90])
    i = buf.find(hdr)
    print(f"Header 0x55AAEB90 at offset: {i}")
    if i >= 0:
        frame = buf[i:i+300]
        print(f"Frame type byte[4] = 0x{frame[4]:02x}  (0x02=cell_info, 0x03=device_info, 0x01=settings)")
        print(f"Frame len from header: {len(frame)} bytes")
        print(frame.hex())

asyncio.run(main())
