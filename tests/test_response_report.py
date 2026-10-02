"""响应内容决定报告结果的回归测试。

经由公开入口 ``python -m api_workbench run case.json`` 请求受控的
127.0.0.1 临时端口 HTTP 服务，覆盖：

- 合法 JSON 对象响应的字段断言（通过、值不符、字段缺失、类型不符、
  null 值、状态码与字段检查相互独立）；
- 响应不是合法 JSON 对象（无法解析的正文、合法 JSON 数组）时
  统一归类为 invalid_response，且保留状态码检查结果。

每个场景核对报告中的用例名称、被检查字段、期望值与实际值，
并断言 stdout 只有一个可解析的 JSON 报告、stderr 为空且无 Traceback。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _RoutingHandler(BaseHTTPRequestHandler):
    """按 server.routes 中的 {路径: (状态码, 响应字节)} 返回受控响应。"""

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        status, body = self.server.routes.get(self.path, (404, b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class ControlledServer:
    """在 127.0.0.1 临时端口上运行受控服务的上下文管理。"""

    def __init__(self, routes: dict) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _RoutingHandler)
        self.httpd.routes = routes
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ControlledServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _write_case(case: dict) -> str:
    """把用例字典写入临时 JSON 文件，返回路径（调用方负责清理）。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(case, handle)
    return path


class ResponseReportTests(unittest.TestCase):
    """以受控本地服务验证响应内容如何决定报告与退出码。"""

    def _run_case(self, case: dict, routes: dict) -> tuple[dict, subprocess.CompletedProcess]:
        """启动受控服务、经公开入口执行用例，返回 (报告, 子进程结果)。"""
        with ControlledServer(routes) as server:
            full_case = {
                "name": "response-report",
                "url": f"http://127.0.0.1:{server.port}/resource",
                "expected_status": 200,
                "field": "status",
                "expected_value": "ok",
                **case,
            }
            case_path = _write_case(full_case)
            self.addCleanup(os.unlink, case_path)
            completed = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=10,
            )

        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(stderr, "", "stderr 必须为空")
        self.assertNotIn("Traceback", stderr)
        # stdout 整体必须可解析为唯一一个 JSON 报告（与缩进、键顺序无关）
        report = json.loads(completed.stdout.decode("utf-8"))
        return report, completed

    def _assert_common(self, report: dict, case_overrides: dict) -> None:
        """核对报告携带的用例名称、被检查字段与期望值。"""
        self.assertEqual(report["name"], case_overrides.get("name", "response-report"))
        self.assertEqual(report["field_check"]["field"], case_overrides.get("field", "status"))
        self.assertEqual(
            report["field_check"]["expected"],
            case_overrides.get("expected_value", "ok"),
        )
        self.assertEqual(
            report["status_check"]["expected"],
            case_overrides.get("expected_status", 200),
        )

    # ---------- 合法 JSON 对象响应 ----------

    def test_matching_status_and_field_passes(self) -> None:
        report, completed = self._run_case({}, {"/resource": (200, b'{"status":"ok"}')})
        self.assertEqual(completed.returncode, 0)
        self._assert_common(report, {})
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])

    def test_mismatched_string_value_fails_field_check(self) -> None:
        report, completed = self._run_case({}, {"/resource": (200, b'{"status":"fail"}')})
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, {})
        self.assertEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        # 不符的值原样出现在 actual
        self.assertEqual(report["field_check"]["actual"], "fail")

    def test_missing_field_fails_with_null_actual(self) -> None:
        report, completed = self._run_case({}, {"/resource": (200, b'{"other":"ok"}')})
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, {})
        self.assertEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])

    def test_numeric_value_does_not_match_string_expectation(self) -> None:
        report, completed = self._run_case(
            {"expected_value": "1"}, {"/resource": (200, b'{"status":1}')}
        )
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, {"expected_value": "1"})
        self.assertEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertFalse(report["field_check"]["passed"])
        # actual 保持数字类型，不做字符串化
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertIsInstance(report["field_check"]["actual"], int)

    def test_null_value_does_not_pass(self) -> None:
        report, completed = self._run_case({}, {"/resource": (200, b'{"status":null}')})
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, {})
        self.assertEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])

    def test_status_mismatch_with_matching_field(self) -> None:
        # 状态码不符但字段值相符：状态码检查失败、字段检查仍通过，
        # 总结果失败且 error 为 assertion_failed（不能仅凭总 passed 判断）
        report, completed = self._run_case({}, {"/resource": (404, b'{"status":"ok"}')})
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, {})
        self.assertEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertEqual(report["status_check"]["actual"], 404)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])

    # ---------- 响应不是合法 JSON 对象 ----------

    def _assert_invalid_response(
        self, body: bytes, server_status: int, expected_status: int
    ) -> None:
        overrides = {"expected_status": expected_status}
        report, completed = self._run_case(
            overrides, {"/resource": (server_status, body)}
        )
        self.assertEqual(completed.returncode, 1)
        self._assert_common(report, overrides)
        self.assertEqual(report["error"], "invalid_response")
        self.assertFalse(report["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        # 无论状态码是否符合期望，都保留收到的状态码及其检查结果
        self.assertEqual(report["status_check"]["actual"], server_status)
        self.assertEqual(
            report["status_check"]["passed"], server_status == expected_status
        )

    def test_unparseable_body_is_invalid_response(self) -> None:
        for server_status, expected_status in [(200, 200), (500, 200)]:
            with self.subTest(server_status=server_status):
                self._assert_invalid_response(b"not json at all", server_status, expected_status)

    def test_json_array_body_is_invalid_response(self) -> None:
        for server_status, expected_status in [(200, 200), (500, 200)]:
            with self.subTest(server_status=server_status):
                self._assert_invalid_response(b'["status","ok"]', server_status, expected_status)


if __name__ == "__main__":
    unittest.main()
