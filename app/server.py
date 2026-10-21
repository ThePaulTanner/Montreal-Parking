"""Placeholder app: /healthz and a data-freshness page. Login, map and API come later.

Also starts the daily data refresh (etl.py) in a background thread.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import etl

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
            # healthy once data exists and the last successful refresh is under 36h old
            try:
                age = time.time() - os.path.getmtime(os.path.join(DATA, "current", ".ok"))
            except OSError:
                return self._send(503, "no data yet", "text/plain")
            return self._send(200 if age < 36 * 3600 else 503, "ok" if age < 36 * 3600 else "stale data", "text/plain")
        try:
            with open(os.path.join(DATA, "current", "manifest.json")) as f:
                m = json.load(f)
        except (OSError, ValueError):
            return self._send(503, "No data yet. The etl container has not finished its first download.", "text/plain")
        self._send(200, json.dumps(m, indent=2), "application/json")

    def log_message(self, *a):
        pass


threading.Thread(target=etl.daemon, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
