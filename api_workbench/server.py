"""Example HTTP service bound to 127.0.0.1:8765."""

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "127.0.0.1"
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            status, body = 200, b'{"status":"ok"}'
        else:
            status, body = 404, b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - keep stdlib signature
        pass


def serve():
    try:
        httpd = HTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"failed to bind {HOST}:{PORT}: {exc}", file=sys.stderr)
        return 2
    print(f"serving on http://{HOST}:{PORT}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
