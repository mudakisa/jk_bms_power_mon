"""Minimal JK-BMS (JIKONG) BLE reader for the JK02_32S protocol.
Tested against JK-B2A8S30P (300A) and JK-B1A8S10P (100A), firmware 15.26.
"""
import asyncio, struct
from bleak import BleakClient

CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"
RESP_HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])
FRAME_LEN = 300
CMD_DEVICE_INFO = 0x97
CMD_CELL_INFO = 0x96


def _cmd(command, value=0):
    f = bytearray(20)
    f[0:4] = bytes([0xAA, 0x55, 0x90, 0xEB])
    f[4] = command
    f[6] = value & 0xFF
    f[19] = sum(f[0:19]) & 0xFF
    return bytes(f)


def parse_cell_info(f: bytes) -> dict:
    """Parse a 300-byte JK02_32S cell-info frame (type 0x02)."""
    if len(f) < FRAME_LEN or f[0:4] != RESP_HEADER or f[4] != 0x02:
        raise ValueError("not a cell-info frame")
    if (sum(f[0:FRAME_LEN - 1]) & 0xFF) != f[FRAME_LEN - 1]:
        raise ValueError("CRC mismatch")

    u16 = lambda o: struct.unpack_from("<H", f, o)[0]
    i16 = lambda o: struct.unpack_from("<h", f, o)[0]
    u32 = lambda o: struct.unpack_from("<I", f, o)[0]
    i32 = lambda o: struct.unpack_from("<i", f, o)[0]

    enabled = u32(70)
    n = bin(enabled).count("1")
    cells = [u16(6 + i * 2) / 1000 for i in range(n)]

    return {
        "cells_v": cells,
        "cell_count": n,
        "cell_avg_v": u16(74) / 1000,
        "cell_delta_v": u16(76) / 1000,
        "mos_temp_c": i16(144) / 10,
        "pack_v": u32(150) / 1000,
        "power_w": i32(154) / 1000,
        "current_a": i32(158) / 1000,
        "temp1_c": i16(162) / 10,
        "temp2_c": i16(164) / 10,
        "soc_pct": f[173],
        "remain_ah": u32(174) / 1000,
        "nominal_ah": u32(178) / 1000,
        "cycles": u32(182),
        "cycle_ah": u32(186) / 1000,
    }


async def read_cell_info(address: str, timeout: float = 20.0) -> dict:
    chunks = []
    done = asyncio.Event()

    def on_notify(_, data: bytearray):
        chunks.append(bytes(data))
        buf = b"".join(chunks)
        i = buf.find(RESP_HEADER)
        while i >= 0:
            if len(buf) - i >= FRAME_LEN and buf[i + 4] == 0x02:
                done.set()
                return
            i = buf.find(RESP_HEADER, i + 4)

    async with BleakClient(address, timeout=timeout) as c:
        await c.start_notify(CHAR, on_notify)
        await c.write_gatt_char(CHAR, _cmd(CMD_DEVICE_INFO), response=False)
        await asyncio.sleep(1.5)
        await c.write_gatt_char(CHAR, _cmd(CMD_CELL_INFO), response=False)
        try:
            await asyncio.wait_for(done.wait(), timeout=8.0)
        finally:
            await c.stop_notify(CHAR)

    buf = b"".join(chunks)
    i = buf.find(RESP_HEADER)
    while i >= 0:
        if len(buf) - i >= FRAME_LEN and buf[i + 4] == 0x02:
            return parse_cell_info(buf[i:i + FRAME_LEN])
        i = buf.find(RESP_HEADER, i + 4)
    raise RuntimeError("no cell-info frame received")
