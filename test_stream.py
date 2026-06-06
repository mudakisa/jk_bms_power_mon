import asyncio, time
from bleak import BleakClient
from jkbms import _cmd, CHAR, RESP_HEADER, FRAME_LEN, CMD_CELL_INFO, CMD_DEVICE_INFO

ADDR = __import__("config").BANKS[0][1]

async def main():
    buf = bytearray()
    counts = {}  # 15s bucket -> number of 0x02 frames
    t0 = time.time()
    def handle(_, data):
        buf.extend(data)
        while True:
            i = buf.find(RESP_HEADER)
            if i < 0:
                if len(buf) > 3: del buf[:-3]
                break
            if i > 0: del buf[:i]
            if len(buf) < FRAME_LEN: break
            frame = bytes(buf[:FRAME_LEN]); del buf[:FRAME_LEN]
            if frame[4] == 0x02:
                bucket = int((time.time()-t0)//15)
                counts[bucket] = counts.get(bucket,0)+1

    async with BleakClient(ADDR, timeout=20.0) as c:
        print("connected — sending cell_info command ONCE, then silent listen 90s...")
        await c.start_notify(CHAR, handle)
        await c.write_gatt_char(CHAR, _cmd(CMD_DEVICE_INFO), response=False)
        await asyncio.sleep(0.3)
        await c.write_gatt_char(CHAR, _cmd(CMD_CELL_INFO), response=False)
        for _ in range(6):
            await asyncio.sleep(15)
            b = int((time.time()-t0)//15)-1
            print(f"  window {b*15:>2}-{(b+1)*15:>2}s: {counts.get(b,0)} cell-info frames")
    print("RESULT:", "STREAM SELF-SUSTAINS (keepalive removable)" if sum(counts.values())>5 and counts.get(4,0)>0 else "stream stalled — keepalive needed")

asyncio.run(main())
