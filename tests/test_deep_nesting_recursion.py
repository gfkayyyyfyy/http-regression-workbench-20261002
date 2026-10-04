"""响应正文嵌套深度超过 Python 运行时递归上限时的回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 完整收到、但嵌套过深（2000 层单元素数组包裹数字 0）的正文在
   Python 默认递归设置下触发 json 解析的 RecursionError，必须统一
   归入 invalid_response：退出码为 1，stderr 为空（无 Traceback），
   stdout 仍只有一个可按 UTF-8 解码的 JSON 报告；字段检查的 actual
   与 present 为 null，状态码检查保留实际收到的状态码与比较结果；
2. 状态码不符（404）时错误类别仍为 invalid_response，状态码检查
   保留失败结果；
3. 同一字段断言在浅层嵌套（两层数组）且状态码符合时照常通过，
   退出码为 0——即使目标字段位于正文开头且值已符合期望，深层内容
   也不被跳过解析，而浅层内容不受本修复影响。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不修改运行时递归上限，每个用例只发送一次 GET。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_workbench.runner import INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2000 层单元素数组包裹数字 0：远超 Python 默认递归上限（1000），
# json 解析必然抛出 RecursionError；两层数组则远在上限之内。
DEEP_BODY = (
    b'{"status":"ok","extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
)
SHALLOW_BODY = b'{"status":"ok","extra":[[0]]}'


class _ScenarioServer(ThreadingHTTPServer):
    """按测试设定返回固定状态码与原始正文的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.server_address[1]
        self.status_code = 200
        self.body = b"{}"
        self.get_count = 0
        self._lock = threading.Lock()

    def set_scenario(self, status_code: int, body: bytes) -> None:
        with self._lock:
            self.status_code = status_code
            self.body = body
            self.get_count = 0


class _ScenarioHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _ScenarioServer = self.server
        with server._lock:
            status_code = server.status_code
            body = server.body
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class DeepNestingRecursionTests(unittest.TestCase):
    """深层嵌套正文：RecursionError 统一归入 invalid_response 报告。"""

    def setUp(self) -> None:
        self.server = _ScenarioServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _execute(
        self, *, name: str, response_status: int, response_body: bytes
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例、经公开入口执行，返回 (解析后的报告, 进程结果)。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _assert_invalid_response(
        self,
        report: dict,
        completed: subprocess.CompletedProcess,
        *,
        name: str,
        status_actual: int,
        status_passed: bool,
    ) -> None:
        expected = {
            "name": name,
            "passed": False,
            "error": INVALID_RESPONSE,
            "status_check": {
                "expected": 200,
                "actual": status_actual,
                "passed": status_passed,
            },
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": None,
                "passed": False,
                "present": None,
            },
        }
        self.assertEqual(report, expected)
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_deep_nesting_with_matching_status_is_invalid_response(self) -> None:
        # 目标字段位于正文开头且值已符合期望，也不能跳过整份正文解析：
        # 深层嵌套触发 RecursionError 时整份正文判为 invalid_response
        report, completed = self._execute(
            name="deep-nesting-status-match",
            response_status=200,
            response_body=DEEP_BODY,
        )
        self._assert_invalid_response(
            report,
            completed,
            name="deep-nesting-status-match",
            status_actual=200,
            status_passed=True,
        )

    def test_deep_nesting_with_mismatched_status_keeps_status_result(self) -> None:
        # 状态 404 ≠ 期望 200：错误类别仍为 invalid_response，
        # 状态码检查保留实际值与失败结果
        report, completed = self._execute(
            name="deep-nesting-status-mismatch",
            response_status=404,
            response_body=DEEP_BODY,
        )
        self._assert_invalid_response(
            report,
            completed,
            name="deep-nesting-status-mismatch",
            status_actual=404,
            status_passed=False,
        )

    def test_shallow_nesting_still_passes(self) -> None:
        # 两层嵌套数组远在递归上限之内：同一断言照常通过，退出码为 0
        report, completed = self._execute(
            name="shallow-nesting-pass",
            response_status=200,
            response_body=SHALLOW_BODY,
        )
        expected = {
            "name": "shallow-nesting-pass",
            "passed": True,
            "error": None,
            "status_check": {"expected": 200, "actual": 200, "passed": True},
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": "ok",
                "passed": True,
                "present": True,
            },
        }
        self.assertEqual(report, expected)
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")


if __name__ == "__main__":
    unittest.main()
