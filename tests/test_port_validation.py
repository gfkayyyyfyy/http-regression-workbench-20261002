"""端口校验回归测试。

覆盖：
- 字母端口、负数端口、越界端口：连接前拒绝，退出码 2、stdout 为空、
  stderr 以 api_workbench: 前缀说明端口无效、无 Python 调用堆栈、不生成报告；
- 有效边界端口（0、1、65535）及未填写/空端口通过校验；
- 非法端口不会产生任何连接（HTTPConnection 不被实例化）；
- 对照受控本地服务的正常健康检查：报告结构与既有示例一致、退出码 0。

只使用 Python 标准库与受控本地服务。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import HTTPServer
from unittest import mock

from api_workbench.runner import REQUEST_FAILED, run_case
from api_workbench.server import ExampleHandler

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASE_TEMPLATE = {
    "name": "health",
    "expected_status": 200,
    "field": "status",
    "expected_value": "ok",
}


def write_case(url: str) -> str:
    _, path = tempfile.mkstemp(suffix=".json", text=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({**CASE_TEMPLATE, "url": url}, handle)
    return path


class _BinaryStdout:
    """模拟 sys.stdout 的最小包装，run_case 会写入 .buffer。"""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class InvalidPortRejectionTests(unittest.TestCase):
    INVALID_PORTS = ["abc", "-1", "65536", "99999", "12x"]

    def _run_cli(self, port: str) -> subprocess.CompletedProcess:
        case_path = write_case(f"http://127.0.0.1:{port}/health")
        try:
            return subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
            )
        finally:
            os.unlink(case_path)

    def test_invalid_ports_rejected_with_exit_2(self) -> None:
        for port in self.INVALID_PORTS:
            with self.subTest(port=port):
                result = self._run_cli(port)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"", "不得输出报告")
                stderr = result.stderr.decode("utf-8")
                self.assertTrue(
                    stderr.startswith("api_workbench:"),
                    f"stderr 应沿用 api_workbench: 前缀: {stderr!r}",
                )
                self.assertIn("端口无效", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertNotIn("File \"", stderr)

    def test_letter_port_exact_messages(self) -> None:
        result = self._run_cli("abc")
        self.assertIn("http://127.0.0.1:abc/health", result.stderr.decode("utf-8"))

    def test_out_of_range_port_exact_messages(self) -> None:
        result = self._run_cli("65536")
        self.assertIn("65536", result.stderr.decode("utf-8"))

    def test_invalid_port_makes_no_connection(self) -> None:
        """连接发生前拒绝：HTTPConnection 不应被实例化。"""
        case_path = write_case("http://127.0.0.1:abc/health")
        fake_stdout = _BinaryStdout()
        stderr = io.StringIO()
        try:
            with mock.patch("api_workbench.runner.HTTPConnection") as connection_cls:
                with redirect_stdout(fake_stdout), redirect_stderr(stderr):
                    exit_code = run_case(case_path)
        finally:
            os.unlink(case_path)
        self.assertEqual(exit_code, 2)
        self.assertEqual(fake_stdout.buffer.getvalue(), b"")
        connection_cls.assert_not_called()
        self.assertIn("端口无效", stderr.getvalue())


class ValidPortBoundaryTests(unittest.TestCase):
    """有效端口通过校验；无服务时表现为 request_failed（退出码 1）。"""

    def _execute_report(self, url: str) -> tuple[int, bytes, str]:
        case_path = write_case(url)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
            )
        finally:
            os.unlink(case_path)
        return result.returncode, result.stdout, result.stderr.decode("utf-8")

    def test_boundary_ports_pass_validation(self) -> None:
        # 0 按现状回落到默认 80；1 与 65535 通过校验但不承诺有服务。
        for url in (
            "http://127.0.0.1:0/health",
            "http://127.0.0.1:1/health",
            "http://127.0.0.1:65535/health",
        ):
            with self.subTest(url=url):
                code, stdout, stderr = self._execute_report(url)
                self.assertEqual(stderr, "")
                self.assertEqual(code, 1)
                report = json.loads(stdout)
                self.assertEqual(report["error"], REQUEST_FAILED)
                self.assertIsNone(report["status_check"]["actual"])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["status_check"]["passed"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertFalse(report["passed"])

    def test_missing_or_empty_port_uses_default(self) -> None:
        for url in ("http://127.0.0.1/health", "http://127.0.0.1:/health"):
            with self.subTest(url=url):
                code, stdout, stderr = self._execute_report(url)
                self.assertEqual(stderr, "")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout)["error"], REQUEST_FAILED)


class HealthCheckAgainstLocalServiceTests(unittest.TestCase):
    """对照受控本地服务：原有健康用例报告结构与示例结果一致。"""

    def setUp(self) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), ExampleHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def test_health_case_passes(self) -> None:
        case_path = write_case(f"http://127.0.0.1:{self.port}/health")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
            )
        finally:
            os.unlink(case_path)

        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)

        self.assertEqual(
            set(report.keys()),
            {"name", "passed", "error", "status_check", "field_check"},
        )
        self.assertEqual(report["name"], "health")
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])

        self.assertEqual(
            set(report["status_check"].keys()), {"expected", "actual", "passed"}
        )
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])

        self.assertEqual(
            set(report["field_check"].keys()),
            {"field", "expected", "actual", "passed"},
        )
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])

    def test_invalid_port_rejected_even_with_service_running(self) -> None:
        """服务是否启动不影响非法端口的拒绝结果。"""
        case_path = write_case("http://127.0.0.1:65536/health")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=REPO_ROOT,
                capture_output=True,
                timeout=10,
            )
        finally:
            os.unlink(case_path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn("端口无效", result.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
