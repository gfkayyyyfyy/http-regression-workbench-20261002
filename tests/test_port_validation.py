"""端口边界与健康检查回归测试（仅使用 Python 标准库与受控本地服务）。"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from api_workbench.runner import (
    REQUEST_FAILED,
    CaseError,
    execute,
    load_case,
    run_case,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INVALID_PORTS = ["abc", "-1", "-80", "65536", "99999"]


def _write_case(url: str) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。"""
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(case, handle)
    return path


class _CountingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数的受控健康检查服务（响应与 server.ExampleHandler 一致）。"""

    get_count = 0
    _lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
        body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class LocalServer:
    """在 127.0.0.1 临时端口上运行受控服务的上下文管理。"""

    def __init__(self) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "LocalServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def get_count(self) -> int:
        return _CountingHandler.get_count


class InvalidPortTests(unittest.TestCase):
    def test_invalid_ports_rejected_before_connection(self) -> None:
        for port in INVALID_PORTS:
            with self.subTest(port=port):
                case_path = _write_case(f"http://127.0.0.1:{port}/health")
                self.addCleanup(os.unlink, case_path)
                stdout, stderr = io.BytesIO(), io.StringIO()
                with patch("api_workbench.runner.HTTPConnection") as connection_cls:
                    with (
                        patch("sys.stdout", stdout),
                        patch("sys.stderr", stderr),
                    ):
                        exit_code = run_case(case_path)

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), b"", "非法用例不得输出报告")
                message = stderr.getvalue()
                self.assertTrue(
                    message.startswith("api_workbench:"),
                    f"stderr 应沿用 api_workbench: 前缀: {message!r}",
                )
                self.assertIn("端口无效", message)
                self.assertNotIn("Traceback", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"端口 {port} 非法时不得建立连接",
                )

    def test_load_case_raises_case_error(self) -> None:
        for port in INVALID_PORTS:
            with self.subTest(port=port):
                case_path = _write_case(f"http://127.0.0.1:{port}/health")
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("端口无效", str(context.exception))


class PortBoundaryTests(unittest.TestCase):
    def test_boundary_ports_pass_validation(self) -> None:
        for port, expected in [(1, 1), (65535, 65535)]:
            with self.subTest(port=port):
                case_path = _write_case(f"http://127.0.0.1:{port}/health")
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertEqual(case["_parsed"].port, expected)

    def test_default_ports_resolve_to_eighty(self) -> None:
        for suffix in ["", ":"]:
            with self.subTest(suffix=suffix):
                case_path = _write_case(f"http://127.0.0.1{suffix}/health")
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertIsNone(case["_parsed"].port)
                self.assertEqual(case["_parsed"].port or 80, 80)

        case_path = _write_case("http://127.0.0.1:0/health")
        self.addCleanup(os.unlink, case_path)
        self.assertEqual(load_case(case_path)["_parsed"].port, 0)

    def test_execute_uses_expected_effective_port(self) -> None:
        cases = [
            ("http://127.0.0.1/health", 80),
            ("http://127.0.0.1:/health", 80),
            ("http://127.0.0.1:0/health", 80),
            ("http://127.0.0.1:1/health", 1),
            ("http://127.0.0.1:65535/health", 65535),
        ]
        for url, expected_port in cases:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                with patch("api_workbench.runner.HTTPConnection") as connection_cls:
                    # 不假设端口上有服务：请求阶段抛 OSError 应归入 request_failed
                    connection_cls.return_value.request.side_effect = (
                        ConnectionRefusedError("拒绝连接")
                    )
                    report, exit_code = execute(case)
                self.assertEqual(connection_cls.call_args.args[0], "127.0.0.1")
                self.assertEqual(connection_cls.call_args.args[1], expected_port)
                self.assertEqual(exit_code, 1)
                self.assertEqual(report["error"], REQUEST_FAILED)
                self.assertIsNone(report["status_check"]["actual"])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["passed"])
                self.assertFalse(report["status_check"]["passed"])
                self.assertFalse(report["field_check"]["passed"])


class HealthCheckEndToEndTests(unittest.TestCase):
    """以受控本地服务对照：正常健康检查与非法端口拒绝。"""

    def test_healthy_case_passes_with_single_get(self) -> None:
        with LocalServer() as server:
            case_path = _write_case(f"http://127.0.0.1:{server.port}/health")
            self.addCleanup(os.unlink, case_path)
            completed = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stderr, b"")
            report = json.loads(completed.stdout.decode("utf-8"))
            self.assertIsNone(report["error"])
            self.assertTrue(report["passed"])
            self.assertEqual(report["status_check"]["expected"], 200)
            self.assertEqual(report["status_check"]["actual"], 200)
            self.assertTrue(report["status_check"]["passed"])
            self.assertEqual(report["field_check"]["field"], "status")
            self.assertEqual(report["field_check"]["expected"], "ok")
            self.assertEqual(report["field_check"]["actual"], "ok")
            self.assertTrue(report["field_check"]["passed"])
            self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")

    def test_invalid_ports_rejected_as_subprocess(self) -> None:
        for port in ["abc", "-1", "65536"]:
            with self.subTest(port=port):
                case_path = _write_case(f"http://127.0.0.1:{port}/health")
                self.addCleanup(os.unlink, case_path)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, b"")
                stderr = completed.stderr.decode("utf-8")
                self.assertTrue(stderr.startswith("api_workbench:"))
                self.assertIn("端口无效", stderr)
                self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
