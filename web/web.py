"""PowerMon web dashboard — zero-dependency stdlib server.

Reads the same SSD SQLite DB the collector writes (read-only, via readings.py) and
serves a single HTML page + a JSON data endpoint. Runs alongside Grafana; pick
whichever you like.
"""
import http.server, socketserver, json, os, sys, urllib.parse

# readings.py and config.py live in the project root (one level up from web/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from readings import query

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8080


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
