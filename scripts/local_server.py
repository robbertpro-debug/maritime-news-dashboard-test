#!/usr/bin/env python3

import json
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "articles.json"
FETCH_SCRIPT = ROOT / "scripts" / "fetch_feeds.py"
HOST = "127.0.0.1"
PORT = 8000


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            self.serve_data()
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            self.refresh_data()
            return
        self.send_error(404, "Unknown endpoint")

    def serve_data(self):
        if not DATA_PATH.exists():
            payload = {"generatedAt": None, "articles": [], "sources": [], "topics": [], "errors": []}
        else:
            payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        self.send_json(200, payload)

    def refresh_data(self):
        result = subprocess.run(
            [sys.executable, str(FETCH_SCRIPT)],
            cwd=str(ROOT),
            text=True,
            capture_output=True,
        )
        payload = {
          "ok": result.returncode == 0,
          "stdout": result.stdout,
          "stderr": result.stderr,
          "returncode": result.returncode,
        }
        self.send_json(200 if result.returncode == 0 else 500, payload)

    def send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer((HOST, PORT), DashboardHandler)
    print(f"Serving dashboard on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
