"""PowerMon readings - the data side shared by the web dashboard and the desktop app.

Reads the SQLite DB the collector writes (read-only) and returns the series for a time
range, the latest reading, and the time-to-empty / time-to-full estimate.
"""
import json, sqlite3, time

try:
    from config import DB, BANKS
except ImportError:
    raise SystemExit("PowerMon: no config.py in the project root.\n"
                     "    cp config.example.py config.py   # then edit it")

BANK = BANKS[0][0] if BANKS else "bank1"   # dashboards show the first configured bank

COLS = ["pack_v", "current_a", "power_w", "soc_pct", "mos_temp_c", "temp1_c",
        "temp2_c", "cell_min_v", "cell_avg_v", "cell_max_v", "cell_delta_v", "cycles"]

ETA_WINDOW_S = 300   # "current load" = time-weighted mean current over this window
IDLE_A = 0.5         # |mean current| below this is idle, no ETA (the collector's --load-a)
GAP_S = 60           # one sample stands for at most this long (a longer gap = no data)
STALE_S = 120        # no ETA when the newest reading is older than this


def eta(con, now):
    """Time to empty (discharging) or to full (charging) at the current load.

    The mean is weighted by time because storage is adaptive (1 s under load, 30 s
    idle): a plain mean of samples would over-weight the load. It starts over when
    the pack flips between charge and discharge, so an old regime does not linger.
    Charge left comes from the BMS coulomb counter (remain_ah of nominal_ah)."""
    rows = con.execute(
        "SELECT ts, current_a, remain_ah, nominal_ah FROM readings "
        "WHERE bank=? AND ts>=? AND current_a IS NOT NULL ORDER BY ts",
        (BANK, now - ETA_WINDOW_S)).fetchall()
    if not rows or now - rows[-1][0] > STALE_S:
        return None
    last_a = rows[-1][1]
    if abs(last_a) >= IDLE_A:        # charge <-> discharge flip restarts the mean
        for k in range(len(rows) - 1, -1, -1):
            if rows[k][1] * last_a < 0 and abs(rows[k][1]) >= IDLE_A:
                rows = rows[k + 1:]
                break
    ends = [r[0] for r in rows[1:]] + [now]
    w = [min(e - r[0], GAP_S) for r, e in zip(rows, ends)]
    amps = (sum(r[1] * x for r, x in zip(rows, w)) / sum(w)) if sum(w) > 0 else rows[-1][1]
    remain, nominal = rows[-1][2], rows[-1][3]
    out = {"state": "idle", "amps": round(amps, 2), "window_s": ETA_WINDOW_S,
           "hours": None, "at": None}
    if remain is None or nominal is None or abs(amps) < IDLE_A:
        return out
    if amps < 0:
        out["state"], ah = "discharge", remain
    else:
        out["state"], ah = "charge", nominal - remain
    out["hours"] = max(ah, 0.0) / abs(amps)
    out["at"] = int((now + out["hours"] * 3600) * 1000)
    return out


def query(minutes):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=3000")
    now = time.time()
    cutoff = now - minutes * 60
    rows = con.execute(
        f"SELECT ts,{','.join(COLS)},cells_json FROM readings WHERE bank=? AND ts>=? ORDER BY ts",
        (BANK, cutoff)).fetchall()
    out = {"t": [int(r[0] * 1000) for r in rows]}
    for i, c in enumerate(COLS):
        out[c] = [r[i + 1] for r in rows]
    out["delta_mv"] = [(v * 1000 if v is not None else None) for v in out["cell_delta_v"]]
    if rows:
        last = rows[-1]
        latest = {"ts": int(last[0] * 1000)}
        for i, c in enumerate(COLS):
            latest[c] = last[i + 1]
        latest["delta_mv"] = (last[COLS.index("cell_delta_v") + 1] or 0) * 1000
        latest["age_s"] = round(now - last[0], 1)
        try:                                   # per-cell voltages from cells_json
            latest["cells"] = json.loads(last[-1]) if last[-1] else []
        except Exception:
            latest["cells"] = []
        latest["eta"] = eta(con, now)
        out["latest"] = latest
    else:
        out["latest"] = None
    con.close()
    return out
