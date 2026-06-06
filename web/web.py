"""PowerMon web dashboard — zero-dependency stdlib server.

Reads the same SSD SQLite DB the collector writes (read-only) and serves a single
HTML page + a JSON data endpoint. Runs alongside Grafana; pick whichever you like.
"""
import http.server, socketserver, sqlite3, json, time, os, sys, urllib.parse

# config.py lives in the project root (one level up from web/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from config import DB, BANKS
except ImportError:
    raise SystemExit("PowerMon: no config.py in the project root.\n"
                     "    cp config.example.py config.py   # then edit it")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8080
BANK = BANKS[0][0] if BANKS else "bank1"   # dashboard shows the first configured bank

COLS = ["pack_v", "current_a", "power_w", "soc_pct", "mos_temp_c", "temp1_c",
        "temp2_c", "cell_min_v", "cell_avg_v", "cell_max_v", "cell_delta_v", "cycles"]


def query(minutes):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=3000")
    cutoff = time.time() - minutes * 60
    rows = con.execute(
        f"SELECT ts,{','.join(COLS)},cells_json FROM readings WHERE bank=? AND ts>=? ORDER BY ts",
        (BANK, cutoff)).fetchall()
    con.close()
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
        latest["age_s"] = round(time.time() - last[0], 1)
        try:                                   # per-cell voltages from cells_json
            latest["cells"] = json.loads(last[-1]) if last[-1] else []
        except Exception:
            latest["cells"] = []
        out["latest"] = latest
    else:
        out["latest"] = None
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        try:
            with open(os.path.join(STATIC, name), "rb") as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, b"not found", "text/plain")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self._file("index.html", "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            name = os.path.basename(u.path)
            ctype = "application/javascript" if name.endswith(".js") else "text/plain"
            return self._file(name, ctype)
        if u.path == "/api/data":
            q = urllib.parse.parse_qs(u.query)
            try:
                minutes = max(1, min(int(q.get("minutes", ["360"])[0]), 525600))
                self._send(200, json.dumps(query(minutes)).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as srv:
        print(f"PowerMon web on http://127.0.0.1:{PORT}")
        srv.serve_forever()
