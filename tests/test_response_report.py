"""响应内容决定报告结果的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 合法 JSON 对象的字段断言（通过、值不符、字段缺失、类型严格、
   null 值、状态码不符但字段通过）；
2. 响应不是合法 JSON 对象时的分类（无法解析的正文、合法 JSON 数组），
   且无论状态码是否符合期望均为 invalid_response；
3. 未加引号的 NaN/Infinity/-Infinity 字面量（其他字段、目标字段、
   嵌套对象与数组元素）一律 invalid_response，而引号内同名字符串、
   含这些字样的字段名与较长字符串仍合法通过。

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

    field_present 默认为 True：合法对象且键存在的场景最多；缺键场景传
    False，invalid_response 场景传 None。
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


class _ReportFlowTestCase(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与单报告解析。"""

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
        """配置服务响应、写用例、经公开入口执行，返回 (解析后的报告, 进程结果)。"""
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


class ValidObjectFieldAssertionTests(_ReportFlowTestCase):
    """合法 JSON 对象响应：字段断言决定字段检查与总结果。"""

    def test_both_checks_pass_when_status_and_string_field_match(self) -> None:
        report, completed = self._execute(
            name="object-pass",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok"}',
        )
        expected = _expected_report(
            name="object-pass",
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
        self.assertIsNone(report["error"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_string_value_mismatch_fails_field_with_raw_actual(self) -> None:
        report, completed = self._execute(
            name="value-mismatch",
            expected_status=200,
            field="status",
            expected_value="wrong",
            response_status=200,
            response_body=b'{"status":"ok"}',
        )
        expected = _expected_report(
            name="value-mismatch",
            field="status",
            expected_value="wrong",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual="ok",
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(report, expected)
        # 不符的原值必须原样出现在 actual，而非期望值或 null
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertIsInstance(report["field_check"]["actual"], str)
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_missing_field_fails_with_null_actual(self) -> None:
        report, completed = self._execute(
            name="field-missing",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"other":"ok"}',
        )
        expected = _expected_report(
            name="field-missing",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=None,
            field_passed=False,
            error=ASSERTION_FAILED,
            field_present=False,
        )
        self.assertEqual(report, expected)
        self.assertIsNone(report["field_check"]["actual"], "字段缺失时 actual 为 null")
        self.assertIs(report["field_check"]["present"], False, "字段缺失时 present 为 false")
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_numeric_value_does_not_match_string_expected(self) -> None:
        # 响应中是数字 1，用例期望字符串 "1"：类型不同必须失败
        report, completed = self._execute(
            name="numeric-vs-string",
            expected_status=200,
            field="status",
            expected_value="1",
            response_status=200,
            response_body=b'{"status":1}',
        )
        expected = _expected_report(
            name="numeric-vs-string",
            field="status",
            expected_value="1",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=1,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(report, expected)
        actual = report["field_check"]["actual"]
        # actual 必须保持数字类型（同时排除被当作布尔的 True）
        self.assertIs(type(actual), int)
        self.assertIsNot(actual, True)
        self.assertEqual(actual, 1)
        self.assertNotEqual(actual, "1")
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_null_field_value_does_not_pass(self) -> None:
        report, completed = self._execute(
            name="null-value",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":null}',
        )
        expected = _expected_report(
            name="null-value",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=None,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(report, expected)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_fails_overall_but_field_check_still_passes(self) -> None:
        # 状态码 500 ≠ 期望 200，但字段值相符：字段检查必须保持通过，
        # 不能只凭总 passed 反推单项结论
        report, completed = self._execute(
            name="status-mismatch",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=500,
            response_body=b'{"status":"ok"}',
        )
        expected = _expected_report(
            name="status-mismatch",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=500,
            status_passed=False,
            field_actual="ok",
            field_passed=True,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(report, expected)
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"], "字段值相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class InvalidResponseClassificationTests(_ReportFlowTestCase):
    """响应不是合法 JSON 对象：一律 invalid_response，同时保留状态码检查结果。"""

    def test_unparseable_body_with_matching_status(self) -> None:
        report, completed = self._execute(
            name="garbage-status-match",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b"this is {not valid json",
        )
        expected = _expected_report(
            name="garbage-status-match",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
            field_present=None,
        )
        self.assertEqual(report, expected)
        self.assertTrue(report["status_check"]["passed"], "收到的状态码检查结果必须保留")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)

    def test_unparseable_body_with_mismatched_status(self) -> None:
        report, completed = self._execute(
            name="garbage-status-mismatch",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=500,
            response_body=b"<<<still not json>>>",
        )
        expected = _expected_report(
            name="garbage-status-mismatch",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=500,
            status_passed=False,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
            field_present=None,
        )
        self.assertEqual(report, expected)
        # 状态码与字段检查必须分别记录，不能被 invalid_response 覆盖
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_json_array_with_matching_status_is_invalid_response(self) -> None:
        # 合法 JSON，但顶层是数组而非对象
        report, completed = self._execute(
            name="array-status-match",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'["status","ok"]',
        )
        expected = _expected_report(
            name="array-status-match",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=200,
            status_passed=True,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
            field_present=None,
        )
        self.assertEqual(report, expected)
        self.assertTrue(report["status_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_json_array_with_mismatched_status_is_invalid_response(self) -> None:
        report, completed = self._execute(
            name="array-status-mismatch",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=404,
            response_body=b"[]",
        )
        expected = _expected_report(
            name="array-status-mismatch",
            field="status",
            expected_value="ok",
            expected_status=200,
            status_actual=404,
            status_passed=False,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
            field_present=None,
        )
        self.assertEqual(report, expected)
        self.assertEqual(report["status_check"]["actual"], 404)
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)


class NonStandardJsonConstantTests(_ReportFlowTestCase):
    """未加引号的 NaN / Infinity / -Infinity：无论位于正文何处均 invalid_response。

    Python 的 json 解析器默认接受这些非标准常量，这里要求严格 JSON：
    目标字段、其他字段、嵌套对象与数组元素中的字面量常量都必须拒绝；
    而引号内的同名字符串、较长字符串以及含这些字样的字段名仍合法。
    """

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
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"], "非法正文的字段 actual 必须为 null")
        self.assertIsNone(report["field_check"]["present"], "非法正文无法检查字段，present 为 null")
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_nan_in_other_field_with_matching_status_is_invalid(self) -> None:
        report, completed = self._execute(
            name="nan-other-field",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":NaN}',
        )
        self._assert_invalid_response(
            report, completed, name="nan-other-field", status_actual=200, status_passed=True
        )

    def test_infinity_in_other_field_is_invalid(self) -> None:
        report, completed = self._execute(
            name="infinity-other-field",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":Infinity}',
        )
        self._assert_invalid_response(
            report, completed, name="infinity-other-field", status_actual=200, status_passed=True
        )

    def test_negative_infinity_in_other_field_is_invalid(self) -> None:
        report, completed = self._execute(
            name="neg-infinity-other-field",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":-Infinity}',
        )
        self._assert_invalid_response(
            report,
            completed,
            name="neg-infinity-other-field",
            status_actual=200,
            status_passed=True,
        )

    def test_constants_inside_nested_objects_and_arrays_are_invalid(self) -> None:
        # 覆盖嵌套对象与数组元素：常量不出现在目标字段，断言原本可以匹配
        bodies = [
            b'{"status":"ok","nested":{"extra":NaN}}',
            b'{"status":"ok","items":[1,2,NaN]}',
            b'{"status":"ok","items":[{"x":Infinity}]}',
            b'{"status":"ok","deep":[[-Infinity]]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed = self._execute(
                    name="nested-constant",
                    expected_status=200,
                    field="status",
                    expected_value="ok",
                    response_status=200,
                    response_body=body,
                )
                self._assert_invalid_response(
                    report,
                    completed,
                    name="nested-constant",
                    status_actual=200,
                    status_passed=True,
                )

    def test_constant_as_target_field_value_is_invalid(self) -> None:
        report, completed = self._execute(
            name="target-constant",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":NaN}',
        )
        self._assert_invalid_response(
            report, completed, name="target-constant", status_actual=200, status_passed=True
        )

    def test_constant_with_mismatched_status_keeps_status_result(self) -> None:
        # 状态 404 ≠ 期望 200：错误类别仍为 invalid_response，仅状态码检查变化
        report, completed = self._execute(
            name="constant-status-mismatch",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=404,
            response_body=b'{"status":"ok","extra":NaN}',
        )
        self._assert_invalid_response(
            report,
            completed,
            name="constant-status-mismatch",
            status_actual=404,
            status_passed=False,
        )

    def test_quoted_nan_string_in_other_field_still_passes(self) -> None:
        report, completed = self._execute(
            name="quoted-nan-ok",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":"NaN"}',
        )
        expected = _expected_report(
            name="quoted-nan-ok",
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

    def test_quoted_constant_words_in_longer_strings_still_pass(self) -> None:
        # 文本中出现相同字样不得导致拒绝
        for body in (
            b'{"status":"ok","note":"the limit is Infinity and NaN appears"}',
            b'{"status":"ok","note":"-Infinity is just text here"}',
        ):
            with self.subTest(body=body):
                report, completed = self._execute(
                    name="constant-words-in-text",
                    expected_status=200,
                    field="status",
                    expected_value="ok",
                    response_status=200,
                    response_body=body,
                )
                self.assertTrue(report["status_check"]["passed"])
                self.assertTrue(report["field_check"]["passed"])
                self.assertTrue(report["passed"])
                self.assertIsNone(report["error"])
                self.assertEqual(completed.returncode, 0)

    def test_field_names_containing_constant_words_still_pass(self) -> None:
        # 字段名中包含这些字样是合法的
        report, completed = self._execute(
            name="constant-like-field-name",
            expected_status=200,
            field="status",
            expected_value="ok",
            response_status=200,
            response_body=b'{"NaN":1,"Infinity":2,"-Infinity":3,"status":"ok"}',
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
