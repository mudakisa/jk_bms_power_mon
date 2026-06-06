import asyncio, sys
from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else __import__("config").BANKS[0][1]
CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"

def jk_cmd(cmd, value=0):
    f = bytearray(20)
    f[0:4] = bytes([0xAA, 0x55, 0x90, 0xEB]); f[4] = cmd; f[6] = value & 0xFF
    f[19] = sum(f[0:19]) & 0xFF
    return bytes(f)

async def main():
    chunks = []
    async with BleakClient(ADDR, timeout=20.0) as c:
        await c.start_notify(CHAR, lambda _, d: chunks.append(bytes(d)))
        await c.write_gatt_char(CHAR, jk_cmd(0x97), response=False)
        await asyncio.sleep(1.5)
        await c.write_gatt_char(CHAR, jk_cmd(0x96), response=False)
        await asyncio.sleep(4.0)
        await c.stop_notify(CHAR)

    buf = b"".join(chunks)
    hdr = bytes([0x55, 0xAA, 0xEB, 0x90])
    print(f"Total {len(buf)} bytes. Frames found:")
    idx, found = 0, []
    while True:
        i = buf.find(hdr, idx)
        if i < 0: break
        found.append(i); idx = i + 4
    for i in found:
        frame = buf[i:i+300]
        ftype = frame[4] if len(frame) > 4 else None
        tname = {0x01:"settings",0x02:"CELL_INFO",0x03:"device_info"}.get(ftype, "?")
        print(f"  offset {i:5}  type 0x{ftype:02x} ({tname})  bytes_avail={len(frame)}")
        if ftype == 0x02:
            with open("cellinfo_bank1.hex","w") as fh: fh.write(frame.hex())
            print("    -> saved cell_info frame to cellinfo_bank1.hex")
            print("   ", frame.hex())

asyncio.run(main())
