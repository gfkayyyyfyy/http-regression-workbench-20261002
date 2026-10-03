"""响应数值溢出（如 1e400）破坏 JSON 报告的回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 正文中任何数值按现有解析规则产生正/负无穷（1e400、-1E+400 等）时，
   整份正文判为 invalid_response——覆盖目标字段、其他字段、
   嵌套对象与数组元素，目标字段相符也不能通过；
2. 状态码检查结果（actual 与按原规则计算的 passed）在
   invalid_response 下保留，状态码不符不改变错误分类；
3. 有限数字的原有处理不变：1e308 不因指数写法被拒绝，
   作为实际值与字符串期望不匹配时 error 为 assertion_failed，
   field_check.actual 保留数值；
4. 字符串 "1e400" / "Infinity" 及含这些字样的字段名仍是普通文本；
5. stdout 有且仅有一个标准 JSON 报告（严格解析，无 Infinity 字面量），
   stderr 为空，退出码符合预期，整个运行只发送一次 GET。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765、不需要手工启动服务或访问公网。
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

from api_workbench.runner import ASSERTION_FAILED, INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _reject_constant(value: str):
    raise ValueError(f"报告必须为标准 JSON，不应出现常量: {value}")


class NumericOverflowTests(unittest.TestCase):
    """数值溢出与有限数字兼容性的端到端回归。"""

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
        self,
        *,
        name: str,
        expected_status: int = 200,
        field: str = "status",
        expected_value: str = "ok",
        response_status: int = 200,
        response_body: bytes,
    ) -> tuple[dict, subprocess.CompletedProcess, str]:
        """配置服务响应、写用例、经公开入口执行，返回 (报告, 进程结果, stdout)。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": expected_status,
            "field": field,
            "expected_value": expected_value,
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
        # 严格解析：报告中不允许出现 Infinity/NaN 等非标准字面量
        decoder = json.JSONDecoder(parse_constant=_reject_constant)
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed, stdout_text

    def _assert_invalid_response(
        self,
        report: dict,
        completed: subprocess.CompletedProcess,
        *,
        name: str,
        status_actual: int,
        status_passed: bool,
    ) -> None:
        self.assertEqual(report["name"], name)
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertIs(report["passed"], False)
        # 状态码检查结果保留：actual 为收到的状态码，passed 按原有匹配规则
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        # 字段检查失败且 actual 为 null，字段名与期望值保留
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIs(report["field_check"]["passed"], False)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_positive_overflow_in_target_field_is_invalid(self) -> None:
        report, completed, _ = self._execute(
            name="overflow-target",
            response_body=b'{"status":1e400}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-target",
            status_actual=200, status_passed=True,
        )

    def test_negative_overflow_in_target_field_is_invalid(self) -> None:
        report, completed, _ = self._execute(
            name="neg-overflow-target",
            response_body=b'{"status":-1e400}',
        )
        self._assert_invalid_response(
            report, completed, name="neg-overflow-target",
            status_actual=200, status_passed=True,
        )

    def test_overflow_in_other_field_fails_despite_matching_target(self) -> None:
        # 目标字段相符，但其他字段溢出：不能因目标字段相符而通过
        report, completed, _ = self._execute(
            name="overflow-other-field",
            response_body=b'{"status":"ok","extra":1e400}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-other-field",
            status_actual=200, status_passed=True,
        )

    def test_overflow_in_array_element_fails_despite_matching_target(self) -> None:
        report, completed, _ = self._execute(
            name="overflow-array-element",
            response_body=b'{"status":"ok","extra":[-1E+400]}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-array-element",
            status_actual=200, status_passed=True,
        )

    def test_overflow_in_nested_object_and_array_is_invalid(self) -> None:
        bodies = [
            b'{"status":"ok","nested":{"extra":1e400}}',
            b'{"status":"ok","items":[1,2,1e400]}',
            b'{"status":"ok","items":[{"x":-1e400}]}',
            b'{"status":"ok","deep":[[1E+400]]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed, _ = self._execute(
                    name="overflow-nested",
                    response_body=body,
                )
                self._assert_invalid_response(
                    report, completed, name="overflow-nested",
                    status_actual=200, status_passed=True,
                )

    def test_overflow_with_mismatched_status_keeps_status_result(self) -> None:
        # 状态 500 ≠ 期望 200：错误分类仍为 invalid_response，仅状态码检查变化
        report, completed, _ = self._execute(
            name="overflow-status-mismatch",
            response_status=500,
            response_body=b'{"status":"ok","extra":1e400}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-status-mismatch",
            status_actual=500, status_passed=False,
        )

    def test_finite_exponent_number_keeps_original_handling(self) -> None:
        # 1e308 有限，不因指数写法被拒绝；与字符串期望 "ok" 类型不符，
        # error 为 assertion_failed，field_check.actual 保留数值
        report, completed, _ = self._execute(
            name="finite-exponent",
            response_body=b'{"status":1e308}',
        )
        self.assertEqual(report["name"], "finite-exponent")
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertIs(report["passed"], False)
        self.assertTrue(report["status_check"]["passed"])
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), float)
        self.assertEqual(actual, 1e308)
        self.assertIs(report["field_check"]["passed"], False)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_finite_exponent_in_other_field_still_passes(self) -> None:
        # 其他字段的有限指数数字不影响通过
        report, completed, _ = self._execute(
            name="finite-exponent-other",
            response_body=b'{"status":"ok","extra":1e308}',
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertIs(report["passed"], True)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_overflow_looking_strings_still_pass(self) -> None:
        # 字符串 "1e400"、"Infinity" 是普通文本，不触发数值拒绝
        for body in (
            b'{"status":"ok","extra":"1e400"}',
            b'{"status":"ok","extra":"Infinity"}',
            b'{"status":"ok","extra":"-1E+400 is just text"}',
        ):
            with self.subTest(body=body):
                report, completed, _ = self._execute(
                    name="overflow-like-string",
                    response_body=body,
                )
                self.assertTrue(report["field_check"]["passed"])
                self.assertIs(report["passed"], True)
                self.assertIsNone(report["error"])
                self.assertEqual(completed.returncode, 0)

    def test_field_names_containing_overflow_words_still_pass(self) -> None:
        report, completed, _ = self._execute(
            name="overflow-like-field-name",
            response_body=b'{"1e400":1,"Infinity":2,"status":"ok"}',
        )
        self.assertTrue(report["field_check"]["passed"])
        self.assertIs(report["passed"], True)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
