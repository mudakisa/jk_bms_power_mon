"""PowerMon readings - the data side shared by the web dashboard and the desktop app.

Reads the SQLite DB the collector writes (read-only) and returns the series for a time
range, the latest reading, and the time-to-empty / time-to-full estimate.
"""
import json, os, sqlite3, time

try:
    from config import DB, BANKS
except ImportError:
    raise SystemExit("PowerMon: no config.py in the project root.\n"
                     "    cp config.example.py config.py   # then edit it")

# (id, label) per bank; the label is the optional 3rd field of a BANKS entry
LABELS = [(b[0], b[2] if len(b) > 2 else b[0]) for b in BANKS]

COLS = ["pack_v", "current_a", "power_w", "soc_pct", "mos_temp_c", "temp1_c",
        "temp2_c", "cell_min_v", "cell_avg_v", "cell_max_v", "cell_delta_v", "cycles"]

ETA_WINDOW_S = 300   # "current load" = time-weighted mean current over this window
IDLE_A = 0.5         # |mean current| below this is idle, no ETA (the collector's --load-a)
GAP_S = 60           # one sample stands for at most this long (a longer gap = no data)
STALE_S = 120        # no ETA when the newest reading is older than this


def control_path(db=DB):
    """Banks paused from a dashboard: the collector releases their BLE link (so the
    phone app can connect) until they are resumed. A JSON list next to the DB."""
    return os.path.splitext(db)[0] + ".paused.json"


def status_path(db=DB):
    """Per-bank link status the collector writes every ~10 s: {"bank": {"link", "rssi", "ts"}}."""
    return os.path.splitext(db)[0] + ".status.json"


def links(db=DB):
    """{bank: dBm or None} for the banks with a live link (a fresh, < 30 s, status).
    None = linked but no signal reading (btmgmt lacks its capability)."""
    try:
        with open(status_path(db)) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return {b: s.get("rssi") for b, s in st.items()
            if s.get("link") and time.time() - s["ts"] < 30}


def paused(db=DB):
    try:
        with open(control_path(db)) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def set_paused(bank, on, db=DB):
    p = paused(db)
    (p.add if on else p.discard)(bank)
    tmp = control_path(db) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(p), f)
    os.replace(tmp, control_path(db))


def banks():
    off, live = paused(), links()
    return [{"id": b, "label": label, "paused": b in off, "link": b in live,
             "rssi": live.get(b)} for b, label in LABELS]


def latest_soc(db=DB):
    """{bank: (soc_pct, ts)} from each bank's newest reading."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    out = {}
    for b, _ in LABELS:
        r = con.execute("SELECT soc_pct, ts FROM readings WHERE bank=? ORDER BY ts DESC LIMIT 1",
                        (b,)).fetchone()
        if r:
            out[b] = r
    con.close()
    return out


def eta(con, bank, now):
    """Time to empty (discharging) or to full (charging) at the current load.

    The mean is weighted by time because storage is adaptive (1 s under load, 30 s
    idle): a plain mean of samples would over-weight the load. It starts over when
    the pack flips between charge and discharge, so an old regime does not linger.
    Charge left comes from the BMS coulomb counter (remain_ah of nominal_ah)."""
    rows = con.execute(
        "SELECT ts, current_a, remain_ah, nominal_ah FROM readings "
        "WHERE bank=? AND ts>=? AND current_a IS NOT NULL ORDER BY ts",
        (bank, now - ETA_WINDOW_S)).fetchall()
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


def _connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _signed_power(p, a):
    """The BMS reports power unsigned (|V*I|); give it the current's sign: - = discharge."""
    return -p if p is not None and a is not None and a < 0 else p


def _latest(con, bank, now):
    """The bank's newest reading, of any age, with its ETA; None when it has none."""
    r = con.execute(f"SELECT ts,{','.join(COLS)},cells_json FROM readings "
                    "WHERE bank=? ORDER BY ts DESC LIMIT 1", (bank,)).fetchone()
    if not r:
        return None
    L = {"ts": int(r[0] * 1000)}
    for i, c in enumerate(COLS):
        L[c] = r[i + 1]
    L["power_w"] = _signed_power(L["power_w"], L["current_a"])
    L["delta_mv"] = (L["cell_delta_v"] or 0) * 1000
    L["age_s"] = round(now - r[0], 1)
    try:                                       # per-cell voltages from cells_json
        L["cells"] = json.loads(r[-1]) if r[-1] else []
    except Exception:
        L["cells"] = []
    L["eta"] = eta(con, bank, now)
    return L


def latest(bank):
    """The newest reading of a bank: one row by the (bank, ts) index - cheap enough
    for a once-a-second refresh of the cards."""
    con = _connect()
    try:
        return _latest(con, bank, time.time())
    finally:
        con.close()


def query(minutes, bank):
    """All readings of a bank over the last `minutes` (for the charts) + its latest."""
    con = _connect()
    now = time.time()
    cutoff = now - minutes * 60
    rows = con.execute(
        f"SELECT ts,{','.join(COLS)},cells_json FROM readings WHERE bank=? AND ts>=? ORDER BY ts",
        (bank, cutoff)).fetchall()
    out = {"t": [int(r[0] * 1000) for r in rows]}
    for i, c in enumerate(COLS):
        out[c] = [r[i + 1] for r in rows]
    out["power_w"] = [_signed_power(p, a) for p, a in zip(out["power_w"], out["current_a"])]
    out["delta_mv"] = [(v * 1000 if v is not None else None) for v in out["cell_delta_v"]]
    out["latest"] = _latest(con, bank, now)
    con.close()
    return out
