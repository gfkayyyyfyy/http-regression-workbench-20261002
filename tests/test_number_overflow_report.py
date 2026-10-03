"""响应数值溢出（如 1e400）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 响应中任何数值按现有解析规则产生正/负无穷（1e400、-1E+400 等）时，
   整份正文判为 invalid_response——覆盖目标字段、其他字段、
   嵌套对象与数组元素，目标字段相符也不能通过；
2. 此时退出码为 1，stdout 只有一个标准 JSON 报告（严格解析，
   不接受 Infinity/NaN 字面量），stderr 为空且无 Traceback，
   状态码检查结果按原有规则保留；
3. 有限数字的原有处理不变：1e308 不因指数写法被拒绝，
   作为实际值仍与字符串期望不匹配（assertion_failed），
   field_check.actual 保留数值；
4. 字符串 "1e400"、"Infinity" 与含这些字样的字段名仍是普通文本。

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


def _reject_constant(value: str):
    """严格校验报告本身：stdout 中不得出现 Infinity/NaN 等非标准字面量。"""
    raise ValueError(f"报告包含非标准 JSON 常量: {value}")


_STRICT_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


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


def _expected_report(
    *,
    name: str,
    field: str,
    expected_value: str,
    expected_status: int,
    status_actual: int | None,
    status_passed: bool,
    field_actual,
    field_passed: bool,
    error: str | None,
    field_present: bool | None = True,
) -> dict:
    """按 runner 的报告结构构造期望报告（整体内容比较，不依赖键顺序/缩进）。

    field_present 默认为 True（合法对象且键存在）；缺键传 False，
    invalid_response 传 None。
    """
    return {
        "name": name,
        "passed": bool(status_passed and field_passed),
        "error": error,
        "status_check": {
            "expected": expected_status,
            "actual": status_actual,
            "passed": status_passed,
        },
        "field_check": {
            "field": field,
            "expected": expected_value,
            "actual": field_actual,
            "passed": field_passed,
            "present": field_present,
        },
    }


class NumberOverflowTestCase(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与严格单报告解析。"""

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
        expected_status: int,
        field: str,
        expected_value: str,
        response_status: int,
        response_body: bytes,
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例、经公开入口执行，返回 (严格解析后的报告, 进程结果)。"""
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
        # 严格解析：报告自身必须是标准 JSON，不得含 Infinity/NaN 字面量
        report, end = _STRICT_DECODER.raw_decode(stdout_text)
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
        expected = _expected_report(
            name=name,
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=status_actual,
            status_passed=status_passed,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
            field_present=None,
        )
        self.assertEqual(report, expected)
        # 用例名称、字段名与期望值必须保留
        self.assertEqual(report["name"], name)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        # 状态码检查结果按原有匹配规则保留，不随错误分类改变
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"], "溢出正文的字段 actual 必须为 null")
        self.assertIsNone(report["field_check"]["present"], "溢出正文无法检查字段，present 为 null")
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")


class OverflowNumberInvalidResponseTests(NumberOverflowTestCase):
    """任何位置的数值溢出为 ±inf：整份正文一律 invalid_response。"""

    def test_positive_overflow_in_target_field_is_invalid(self) -> None:
        report, completed = self._execute(
            name="overflow-target",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":1e400}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-target", status_actual=200, status_passed=True
        )

    def test_negative_overflow_in_target_field_is_invalid(self) -> None:
        report, completed = self._execute(
            name="neg-overflow-target",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":-1e400}',
        )
        self._assert_invalid_response(
            report,
            completed,
            name="neg-overflow-target",
            status_actual=200,
            status_passed=True,
        )

    def test_overflow_in_other_field_array_element_fails_despite_matching_target(
        self,
    ) -> None:
        # 目标字段 "status" 相符，但其他字段的数组元素溢出：不能因目标相符而通过
        report, completed = self._execute(
            name="overflow-in-array",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":[-1E+400]}',
        )
        self._assert_invalid_response(
            report, completed, name="overflow-in-array", status_actual=200, status_passed=True
        )

    def test_overflow_inside_nested_object_is_invalid(self) -> None:
        bodies = [
            b'{"status":"ok","nested":{"extra":1e400}}',
            b'{"status":"ok","items":[1,2,3e500]}',
            b'{"status":"ok","items":[{"x":-2E+309}]}',
            b'{"status":"ok","deep":[[9e999]]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed = self._execute(
                    name="nested-overflow",
                    expected_status=200,
                    field="status",
                    expected_value="ok",
                    response_status=200,
                    response_body=body,
                )
                self._assert_invalid_response(
                    report,
                    completed,
                    name="nested-overflow",
                    status_actual=200,
                    status_passed=True,
                )

    def test_overflow_with_mismatched_status_keeps_status_result(self) -> None:
        # 状态 500 ≠ 期望 200：错误分类仍为 invalid_response，状态码不符不改变分类
        report, completed = self._execute(
            name="overflow-status-mismatch",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=500,
            response_body=b'{"status":1e400}',
        )
        self._assert_invalid_response(
            report,
            completed,
            name="overflow-status-mismatch",
            status_actual=500,
            status_passed=False,
        )


class FiniteNumberCompatibilityTests(NumberOverflowTestCase):
    """有限数字与文本的原有语义不变。"""

    def test_finite_large_exponent_keeps_numeric_actual(self) -> None:
        # 1e308 是有限值：不因指数写法被拒绝，作为实际值仍与字符串 "ok" 不匹配
        report, completed = self._execute(
            name="finite-large-exponent",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":1e308}',
        )
        expected = _expected_report(
            name="finite-large-exponent",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=1e308,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(report, expected)
        actual = report["field_check"]["actual"]
        # actual 必须保留数值（浮点），而非字符串或 null
        self.assertIs(type(actual), float)
        self.assertEqual(actual, 1e308)
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_finite_numbers_elsewhere_still_pass(self) -> None:
        # 正常数据兼容：有限的大指数、负小数与整数不影响原有通过路径
        report, completed = self._execute(
            name="finite-numbers-pass",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":[1e308,-2.5,0,1.25e-10]}',
        )
        expected = _expected_report(
            name="finite-numbers-pass",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual="ok",
            field_passed=True,
            error=None,
        )
        self.assertEqual(report, expected)
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_overflow_like_strings_remain_plain_text(self) -> None:
        # 字符串 "1e400"、"Infinity" 是普通文本，不触发溢出判定
        report, completed = self._execute(
            name="overflow-like-strings",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","note":"1e400 and Infinity are text"}',
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_field_names_containing_overflow_words_still_pass(self) -> None:
        # 字段名含这些字样同样合法
        report, completed = self._execute(
            name="overflow-like-field-names",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"1e400":1,"Infinity":2,"status":"ok"}',
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
