"""PowerMon collector (streaming): hold a persistent BLE connection to each
JK-BMS bank and log the continuous cell-info stream to SQLite.

Persistent connection wins vs reconnect-per-poll:
  - ~1-2s resolution per bank (vs 11-31s) — catches load transients
  - BMS beeps only ONCE per connect (vs every poll)

Adaptive storage keeps the DB small: frequent under load, sparse when idle,
and the load on/off edge is always captured.
"""
import asyncio, argparse, json, signal, sqlite3, time
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from jkbms import (parse_cell_info, _cmd, CHAR, RESP_HEADER, FRAME_LEN,
                   CMD_CELL_INFO, CMD_DEVICE_INFO)
from readings import paused

try:
    from config import DB, BANKS
except ImportError:
    raise SystemExit("PowerMon: no config.py found.\n"
                     "    cp config.example.py config.py   # then edit it")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts          REAL NOT NULL,
    iso         TEXT NOT NULL,
    bank        TEXT NOT NULL,
    pack_v      REAL, current_a REAL, power_w REAL,
    soc_pct     INTEGER, remain_ah REAL, nominal_ah REAL,
    cell_avg_v  REAL, cell_delta_v REAL,
    cell_min_v  REAL, cell_max_v REAL,
    mos_temp_c  REAL, temp1_c REAL, temp2_c REAL,
    cycles      INTEGER, cells_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_bank_ts ON readings(bank, ts);
"""

def init_db(db):
    con = sqlite3.connect(db, check_same_thread=False)
    con.executescript(SCHEMA)
    con.commit()
    return con

def store(con, bank, d):
    cells = d["cells_v"]
    now = time.time()
    con.execute(
        "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), bank,
         d["pack_v"], d["current_a"], d["power_w"],
         d["soc_pct"], d["remain_ah"], d["nominal_ah"],
         d["cell_avg_v"], d["cell_delta_v"], min(cells), max(cells),
         d["mos_temp_c"], d["temp1_c"], d["temp2_c"],
         d["cycles"], json.dumps(cells)),
    )
    con.commit()

# The adapter has ONE discovery session: two banks scanning or connecting at once
# collide. So finding + connecting takes turns; holding the links side by side is fine.
SCAN_LOCK = asyncio.Lock()


def remembered(addr, adapter="hci0"):
    """The BMS as BlueZ's own device object - connecting to it needs no scan.
    BlueZ keeps the object only for a trusted device (`bluetoothctl trust <MAC>`,
    a host-side flag - nothing is sent to the BMS); otherwise it drops it ~30 s
    after a disconnect and the connect fails with "device ... not found"."""
    return BLEDevice(addr, None, {"path": f"/org/bluez/{adapter}/dev_{addr.replace(':', '_')}"})


async def _sleep_or_stop(stop, timeout):
    """Sleep up to `timeout` seconds, returning early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout)
    except asyncio.TimeoutError:
        pass

async def run_bank(con, db, name, addr, cfg, stop, start_delay=0.0):
    """Hold a persistent connection; store frames adaptively. Reconnect on drop.
    Exits cleanly (disconnecting) when `stop` is set - never via task cancellation,
    which would interrupt the disconnect inside `async with`."""
    if start_delay:                     # stagger banks to avoid a connect race
        await _sleep_or_stop(stop, start_delay)
    buf = bytearray()
    st = {"last_store": 0.0, "last_load": None, "last_frame": 0.0}

    def maybe_store(d):
        now = time.time()
        under_load = abs(d["current_a"]) >= cfg["load_a"]
        gap = now - st["last_store"]
        edge = st["last_load"] is not None and under_load != st["last_load"]
        due = (gap >= cfg["min_load_s"]) if under_load else (gap >= cfg["idle_s"])
        if edge or due:
            store(con, name, d)
            st["last_store"] = now
        st["last_load"] = under_load

    def handle(_, data):
        buf.extend(data)
        while True:
            i = buf.find(RESP_HEADER)
            if i < 0:
                if len(buf) > 3:        # keep last 3 bytes (possible partial header)
                    del buf[:-3]
                break
            if i > 0:
                del buf[:i]
            if len(buf) < FRAME_LEN:
                break
            frame = bytes(buf[:FRAME_LEN]); del buf[:FRAME_LEN]
            if frame[4] == 0x02:
                st["last_frame"] = time.time()
                try: maybe_store(parse_cell_info(frame))
                except Exception: pass

    known = True                        # try BlueZ's remembered device first
    was_paused = False
    while not stop.is_set():
        # Paused from the dashboard: keep the link free (e.g. for the phone app).
        if name in paused(db):
            if not was_paused:
                print(f"{name}: paused - BLE link is free")
                was_paused = True
            await _sleep_or_stop(stop, 1)
            continue
        was_paused = False
        try:
            async with SCAN_LOCK:
                if known:
                    device = remembered(addr)
                else:
                    # BlueZ does not know the device: find it by a scan.
                    # Scanning does NOT make the BMS beep.
                    device = await BleakScanner.find_device_by_address(addr, timeout=15.0)
                if device is not None:
                    client = BleakClient(device, timeout=20.0)
                    await client.connect()
            if device is None:
                print(f"{name}: not found in scan; retry in 5s")
                await _sleep_or_stop(stop, 5)
                continue
            known = True
            # We leave the inner loop only via `stop` or a pause (a normal break),
            # never by cancellation, so the disconnect in `finally` always completes -
            # no stale "Connected: yes" link.
            try:
                print(f"{name}: connected ({addr})")
                st["last_frame"] = time.time()
                await client.start_notify(CHAR, handle)
                # Init handshake: device-info THEN cell-info. The JK needs both to
                # START streaming (cell-info alone does NOT start it). That is 2
                # connect beeps, kept on purpose as a welcome "connected" confirmation
                # (rare op). After this the JK streams ~1 Hz on its own - no periodic
                # keepalive, so no per-tick beeps (the phone app behaves identically).
                await client.write_gatt_char(CHAR, _cmd(CMD_DEVICE_INFO), response=False)
                await asyncio.sleep(0.3)
                await client.write_gatt_char(CHAR, _cmd(CMD_CELL_INFO), response=False)
                while client.is_connected and not stop.is_set():
                    await _sleep_or_stop(stop, 1)
                    if name in paused(db):
                        break
                    # watchdog: re-request ONLY if the stream truly stalls (rare beep)
                    if not stop.is_set() and time.time() - st["last_frame"] > 60:
                        print(f"{name}: stream stalled >60s - re-requesting")
                        await client.write_gatt_char(CHAR, _cmd(CMD_CELL_INFO), response=False)
                        st["last_frame"] = time.time()
            finally:
                await client.disconnect()
            if not stop.is_set():
                if name not in paused(db):
                    print(f"{name}: link lost - reconnecting")
                continue                # paused: the top of the loop reports it
        except BleakError as e:
            if "not found" in str(e):   # BlueZ forgot it: scan on the next try
                known = False
            print(f"{name}: disconnected ({e!r}); retry in 5s")
        except Exception as e:
            print(f"{name}: disconnected ({e!r}); retry in 5s")
        await _sleep_or_stop(stop, 5)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--load-a", type=float, default=0.5, help="|current| >= this is 'under load' (A)")
    ap.add_argument("--min-load-s", type=float, default=1.0, help="min seconds between stores under load")
    ap.add_argument("--idle-s", type=float, default=30.0, help="seconds between stores when idle")
    ap.add_argument("--bank", action="append", help="run only this bank (repeatable); default: all")
    ap.add_argument("--db", default=DB, help="SQLite file (default: DB from config.py)")
    args = ap.parse_args()
    cfg = {"load_a": args.load_a, "min_load_s": args.min_load_s, "idle_s": args.idle_s}
    con = init_db(args.db)
    banks = [b for b in BANKS if not args.bank or b[0] in args.bank]

    # Graceful shutdown: on SIGTERM/SIGINT we SET an event; the bank tasks see it,
    # break out of their loops, and let their `finally` disconnect cleanly.
    # We do NOT cancel the tasks - cancellation interrupts that disconnect and leaves
    # a stale BLE link for the next start / the phone app to trip over.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    tasks = [asyncio.create_task(run_bank(con, args.db, n, a, cfg, stop, i * 2.0))
             for i, (n, a, *_) in enumerate(banks)]
    await asyncio.gather(*tasks)   # each task returns on its own once `stop` is set

asyncio.run(main())
