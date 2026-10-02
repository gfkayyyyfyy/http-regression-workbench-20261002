"""示例服务：127.0.0.1:8765，GET /health 返回 200，其余路径 404。"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 8765


class ExampleHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench/0.1"

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        if urlsplit(self.path).path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {})

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


def serve() -> int:
    try:
        httpd = HTTPServer((HOST, PORT), ExampleHandler)
    except OSError as exc:
        print(
            f"api_workbench: 无法绑定 {HOST}:{PORT}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
