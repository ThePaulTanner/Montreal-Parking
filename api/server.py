"""Placeholder app: /healthz and a data-freshness page. Login, map and API come later."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("DATA_DIR", "/data")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        try:
            with open(os.path.join(DATA, "current", "manifest.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return self._send(503, "No data yet. The etl container has not finished its first download.", "text/plain")
        self._send(200, json.dumps(m, indent=2), "application/json")

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
