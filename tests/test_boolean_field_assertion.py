"""布尔字段相等断言的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 为 JSON true/false 时，仅与同类型的布尔实际值相等：
   true 匹配 true、false 匹配 false；数字 1/0、字符串 "true"/"false"、
   空字符串、null 均不匹配；
2. 布尔期望下字段缺失时 actual 为 null、字段检查失败；
3. field_check 的 expected/actual 保持各自的 JSON 类型（true/false
   原样输出，不转成文字）；
4. 状态码不符仍继续字段检查；非 JSON 对象与非标准常量响应仍为
   invalid_response，超时或连接失败仍为 request_failed，退出码均为 1；
5. expected_value 缺失或为字符串与布尔之外的类型（null、数字、数组、
   对象、未加引号的 NaN/Infinity/-Infinity）时加载即拒绝：退出码 2、
   stdout 为空、stderr 以 ``api_workbench:`` 开头并指出 expected_value、
   无 Traceback，且不发送任何请求。

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

DIAGNOSTIC_PREFIX = "api_workbench:"


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


class _BooleanFlowTestCase(unittest.TestCase):
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

    def _write_case(self, case: dict) -> str:
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)
        return case_path

    def _write_case_raw_expected(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value（None 表示省略该字段）。"""
        lines = [
            f'  "name": "boolean"',
            f'  "url": "http://127.0.0.1:{self.server.port}/resource"',
            f'  "expected_status": 200',
            f'  "field": "enabled"',
        ]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return case_path

    def _execute(
        self,
        *,
        expected_value,
        response_status: int = 200,
        response_body: bytes = b"{}",
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例、经公开入口执行，返回 (解析后的报告, 进程结果)。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": "boolean",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "enabled",
            "expected_value": expected_value,
        }
        case_path = self._write_case(case)
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
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _assert_field_report(
        self,
        report: dict,
        *,
        expected_value,
        field_actual,
        field_passed: bool,
        status_actual: int = 200,
        status_passed: bool = True,
        error,
    ) -> None:
        expected = {
            "name": "boolean",
            "passed": bool(status_passed and field_passed),
            "error": error,
            "status_check": {
                "expected": 200,
                "actual": status_actual,
                "passed": status_passed,
            },
            "field_check": {
                "field": "enabled",
                "expected": expected_value,
                "actual": field_actual,
                "passed": field_passed,
            },
        }
        self.assertEqual(report, expected)


class BooleanMatchTests(_BooleanFlowTestCase):
    """布尔期望与布尔实际值严格同类型相等。"""

    def test_true_matches_true(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":true}'
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=True,
            field_passed=True,
            error=None,
        )
        # expected/actual 必须保持布尔 JSON 类型，而非文字
        self.assertIs(report["field_check"]["expected"], True)
        self.assertIs(report["field_check"]["actual"], True)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_false_matches_false(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":false}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual=False,
            field_passed=True,
            error=None,
        )
        self.assertIs(report["field_check"]["expected"], False)
        self.assertIs(report["field_check"]["actual"], False)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_true_expected_with_false_actual_fails(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":true}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual=True,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(completed.returncode, 1)

    def test_false_expected_with_true_actual_fails(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":false}'
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=False,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(completed.returncode, 1)


class BooleanTypeStrictnessTests(_BooleanFlowTestCase):
    """true 不匹配 1/"true"；false 不匹配 0/""/null，actual 原样保留。"""

    def test_true_does_not_match_number_one(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":1}'
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=1,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), int, "数字 1 必须保持整数类型")
        self.assertIsNot(actual, True)
        self.assertEqual(completed.returncode, 1)

    def test_false_does_not_match_number_zero(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":0}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual=0,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), int, "数字 0 必须保持整数类型")
        self.assertIsNot(actual, False)
        self.assertEqual(completed.returncode, 1)

    def test_true_does_not_match_string_true(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":"true"}'
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual="true",
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertIsInstance(report["field_check"]["actual"], str)
        self.assertEqual(completed.returncode, 1)

    def test_false_does_not_match_string_false(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":"false"}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual="false",
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(completed.returncode, 1)

    def test_false_does_not_match_empty_string(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":""}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual="",
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertEqual(completed.returncode, 1)

    def test_false_does_not_match_null(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":null}'
        )
        self._assert_field_report(
            report,
            expected_value=False,
            field_actual=None,
            field_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(completed.returncode, 1)

    def test_other_typed_values_are_preserved_and_fail(self) -> None:
        for raw_body, expected_actual in (
            (b'{"enabled":1.5}', 1.5),
            (b'{"enabled":[true]}', [True]),
            (b'{"enabled":{"on":true}}', {"on": True}),
        ):
            with self.subTest(raw_body=raw_body):
                report, completed = self._execute(
                    expected_value=True, response_body=raw_body
                )
                self.assertEqual(
                    report["field_check"]["actual"], expected_actual
                )
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)


class BooleanMissingFieldTests(_BooleanFlowTestCase):
    """布尔期望下字段缺失：actual 为 null、字段检查失败。"""

    def test_missing_field_fails_with_null_actual(self) -> None:
        for expected_value in (True, False):
            with self.subTest(expected_value=expected_value):
                report, completed = self._execute(
                    expected_value=expected_value,
                    response_body=b'{"other":true}',
                )
                self._assert_field_report(
                    report,
                    expected_value=expected_value,
                    field_actual=None,
                    field_passed=False,
                    error=ASSERTION_FAILED,
                )
                self.assertIsNone(report["field_check"]["actual"])
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)


class BooleanWithStatusMismatchTests(_BooleanFlowTestCase):
    """状态码不符仍继续字段检查：布尔字段相符时 field_check 保持通过。"""

    def test_status_mismatch_keeps_boolean_field_check_result(self) -> None:
        report, completed = self._execute(
            expected_value=True,
            response_status=500,
            response_body=b'{"enabled":true}',
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=True,
            field_passed=True,
            status_actual=500,
            status_passed=False,
            error=ASSERTION_FAILED,
        )
        self.assertFalse(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"], "字段相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)


class BooleanInvalidResponseTests(_BooleanFlowTestCase):
    """布尔期望不改变既有 invalid_response 分类与退出码。"""

    def test_non_object_body_is_invalid_response(self) -> None:
        report, completed = self._execute(
            expected_value=True,
            response_status=200,
            response_body=b'[true]',
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_unquoted_constant_in_body_is_invalid_response(self) -> None:
        report, completed = self._execute(
            expected_value=True,
            response_status=200,
            response_body=b'{"enabled":true,"extra":NaN}',
        )
        self._assert_field_report(
            report,
            expected_value=True,
            field_actual=None,
            field_passed=False,
            error=INVALID_RESPONSE,
        )
        self.assertEqual(completed.returncode, 1)


class InvalidExpectedValueRejectionTests(_BooleanFlowTestCase):
    """expected_value 必须是字符串或布尔值，否则加载阶段以退出码 2 拒绝。"""

    # 原始 JSON 文本：缺失、null、数字、数组、对象及未加引号的非标准常量
    INVALID_RAW_CASES = [
        ("missing", None),
        ("null", "null"),
        ("integer", "1"),
        ("float", "1.0"),
        ("array", '["true"]'),
        ("object", '{"value": true}'),
        ("nan", "NaN"),
        ("infinity", "Infinity"),
        ("negative-infinity", "-Infinity"),
    ]

    def test_invalid_expected_value_is_rejected_before_request(self) -> None:
        for label, raw in self.INVALID_RAW_CASES:
            with self.subTest(label=label):
                case_path = self._write_case_raw_expected(raw)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )

                self.assertEqual(
                    completed.returncode, 2, f"stderr={completed.stderr!r}"
                )
                self.assertEqual(completed.stdout, b"", "拒绝时不得输出任何报告")
                stderr_text = completed.stderr.decode("utf-8")
                self.assertTrue(
                    stderr_text.startswith(DIAGNOSTIC_PREFIX),
                    f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr_text!r}",
                )
                self.assertIn("expected_value", stderr_text)
                self.assertNotIn("Traceback", stderr_text)
                self.assertEqual(
                    self.server.get_count, 0, "加载失败时不得发送任何请求"
                )

    def test_string_expected_value_still_accepted(self) -> None:
        # 字符串期望的既有行为保持不变：与字符串实际值完全相等才通过
        report, completed = self._execute(
            expected_value="true", response_body=b'{"enabled":"true"}'
        )
        self._assert_field_report(
            report,
            expected_value="true",
            field_actual="true",
            field_passed=True,
            error=None,
        )
        self.assertIsInstance(report["field_check"]["expected"], str)
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
