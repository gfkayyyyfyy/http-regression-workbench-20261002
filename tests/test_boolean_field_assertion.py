"""布尔字段相等断言的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 接受 JSON 布尔值 true/false：布尔期望只匹配布尔实际值
   （true 不匹配数字 1 或字符串 "true"；false 不匹配数字 0、空字符串或 null），
   字符串期望仍只匹配完全相等的字符串；
2. 报告的 field_check.expected/actual 保留各自的 JSON 类型，布尔不转成文字；
   字段缺失时 actual 为 null；其他类型的实际值原样保留并判失败；
3. expected_value 缺失或为字符串、布尔值、null 与有限数字之外的类型
   （数组、对象，含未加引号的 NaN/Infinity/-Infinity 及 1e400 这类
   解析后溢出的数字）时 load_case 抛出 CaseError：不发送请求，
   stdout 为空，stderr 以 api_workbench: 开头并指出 expected_value，
   退出码 2，无 Traceback（显式 null 期望的回归见
   test_null_field_assertion.py，数字期望见 test_number_field_assertion.py）；
4. 布尔期望不改变既有分类：非 JSON 对象或含非标准常量的响应仍为
   invalid_response，连接失败仍为 request_failed，退出码 1；
5. 每次有效执行仅发送一次 GET。

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

from api_workbench.runner import (
    ASSERTION_FAILED,
    INVALID_RESPONSE,
    REQUEST_FAILED,
    CaseError,
    load_case,
)

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

    def _execute(
        self,
        *,
        expected_value,
        response_status: int = 200,
        response_body: bytes = b'{"enabled":true}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例（name=boolean, field=enabled）、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": "boolean",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "enabled",
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
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed


class BooleanExpectedValueMatchingTests(_BooleanFlowTestCase):
    """布尔期望：类型严格的相等匹配，报告保留 JSON 布尔类型。"""

    def test_true_expected_matches_true_actual(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":true}'
        )
        self.assertIs(report["field_check"]["expected"], True)
        self.assertIs(report["field_check"]["actual"], True)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_false_expected_matches_false_actual(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":false}'
        )
        self.assertIs(report["field_check"]["expected"], False)
        self.assertIs(report["field_check"]["actual"], False)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_true_expected_fails_against_false_actual(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":false}'
        )
        self.assertIs(report["field_check"]["actual"], False)
        self.assertIs(type(report["field_check"]["actual"]), bool)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_false_expected_fails_against_true_actual(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":true}'
        )
        self.assertIs(report["field_check"]["actual"], True)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_true_expected_does_not_match_numeric_one(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":1}'
        )
        actual = report["field_check"]["actual"]
        # 数字 1 原样保留且保持整数类型（同时排除被当作布尔 True）
        self.assertIs(type(actual), int)
        self.assertIsNot(actual, True)
        self.assertEqual(actual, 1)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_false_expected_does_not_match_numeric_zero(self) -> None:
        report, completed = self._execute(
            expected_value=False, response_body=b'{"enabled":0}'
        )
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), int)
        self.assertIsNot(actual, False)
        self.assertEqual(actual, 0)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_true_expected_does_not_match_string_true(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"enabled":"true"}'
        )
        self.assertEqual(report["field_check"]["actual"], "true")
        self.assertIs(type(report["field_check"]["actual"]), str)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_false_expected_does_not_match_empty_string_or_null(self) -> None:
        for body in (b'{"enabled":""}', b'{"enabled":null}'):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=False, response_body=body
                )
                self.assertFalse(
                    report["field_check"]["passed"],
                    f"false 期望不应匹配: {body!r}",
                )
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_string_expected_does_not_match_boolean_actual(self) -> None:
        # 字符串期望保持原有语义：布尔实际值即使字面同名也不匹配
        report, completed = self._execute(
            expected_value="true", response_body=b'{"enabled":true}'
        )
        self.assertIsInstance(report["field_check"]["expected"], str)
        self.assertEqual(report["field_check"]["expected"], "true")
        self.assertIs(report["field_check"]["actual"], True)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_missing_field_is_null_actual_with_boolean_expected(self) -> None:
        report, completed = self._execute(
            expected_value=True, response_body=b'{"other":true}'
        )
        self.assertIsNone(report["field_check"]["actual"], "字段缺失时 actual 为 null")
        self.assertIs(report["field_check"]["expected"], True)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_other_typed_actuals_are_preserved_and_fail(self) -> None:
        # 对象、数组、浮点等其他类型：原样保留 JSON 类型，字段检查失败
        for body, expected_actual in [
            (b'{"enabled":{}}', {}),
            (b'{"enabled":[false]}', [False]),
            (b'{"enabled":1.5}', 1.5),
            (b'{"enabled":"yes"}', "yes"),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=True, response_body=body
                )
                self.assertEqual(
                    report["field_check"]["actual"],
                    expected_actual,
                )
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_checks_boolean_field(self) -> None:
        # 状态码 500 ≠ 200，但布尔字段相符：字段检查必须保持通过
        report, completed = self._execute(
            expected_value=True,
            response_status=500,
            response_body=b'{"enabled":true}',
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIs(report["field_check"]["actual"], True)
        self.assertTrue(report["field_check"]["passed"], "字段值相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class BooleanExpectedErrorClassificationTests(_BooleanFlowTestCase):
    """布尔期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_non_object_body_is_invalid_response_with_boolean_expected(self) -> None:
        for body in (b"not json", b"[true, false]"):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=True, response_body=body
                )
                self.assertIs(report["field_check"]["expected"], True)
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_nonstandard_constant_body_is_invalid_response(self) -> None:
        # 目标字段本身是合法布尔值，但其他字段含未加引号的 NaN：整份正文无效
        report, completed = self._execute(
            expected_value=True,
            response_body=b'{"enabled":true,"extra":NaN}',
        )
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)

    def test_connection_failure_is_request_failed_with_boolean_expected(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        case = {
            "name": "boolean",
            "url": f"http://127.0.0.1:{refused_port}/resource",
            "expected_status": 200,
            "field": "enabled",
            "expected_value": True,
        }
        case_path = os.path.join(self.tmpdir.name, "refused.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIs(report["field_check"]["expected"], True)
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.stderr, b"")


class ExpectedValueTypeValidationTests(unittest.TestCase):
    """expected_value 接受字符串、布尔值、null 与有限数字：其他类型一律拒绝。"""

    # 原始 JSON 文本 → 期望出现在诊断中的字样
    # （有限数字 1、1.5 自数值断言引入后成为合法期望，
    # 其匹配规则见 test_number_field_assertion.py）
    INVALID_RAW_VALUES = [
        ("missing", None),  # None 占位表示不写入该字段
        ("array", "[]"),
        ("object", "{}"),
        ("nan", "NaN"),
        ("infinity", "Infinity"),
        ("negative_infinity", "-Infinity"),
        ("overflow", "1e400"),
    ]

    def setUp(self) -> None:
        # 本组通过 get_count 断言非法用例不发送请求，因此仍启动受控服务
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.server = _ScenarioServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_case(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 null/NaN/[] 等类型。"""
        fields = {
            "name": "boolean",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "enabled",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def test_load_case_raises_case_error_for_invalid_types(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                case_path = self._write_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_invalid_types_without_sending_request(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                self.server.set_scenario(200, b'{"enabled":true}')
                case_path = self._write_case(raw_value)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                stderr = completed.stderr.decode("utf-8")
                self.assertEqual(completed.returncode, 2, stderr)
                self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
                self.assertTrue(
                    stderr.startswith(DIAGNOSTIC_PREFIX),
                    f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr!r}",
                )
                self.assertIn("expected_value", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(
                    self.server.get_count,
                    0,
                    f"expected_value={raw_value!r} 非法时不得发送 GET",
                )

    def test_boolean_and_string_values_are_accepted(self) -> None:
        # true/false/字符串均为合法期望值：load_case 不抛错，且报告保留类型
        for raw_value, expected in [
            ("true", True),
            ("false", False),
            ('"true"', "true"),
        ]:
            with self.subTest(raw_value=raw_value):
                self.server.set_scenario(200, b'{"enabled":true}')
                case_path = self._write_case(raw_value)
                loaded = load_case(case_path)
                if isinstance(expected, bool):
                    self.assertIs(loaded["expected_value"], expected)
                else:
                    self.assertEqual(loaded["expected_value"], expected)
                    self.assertIsInstance(loaded["expected_value"], str)

                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(completed.stderr, b"")
                report = json.loads(completed.stdout.decode("utf-8"))
                self.assertEqual(report["field_check"]["expected"], expected)
                self.assertIs(
                    type(report["field_check"]["expected"]), type(expected)
                )
                self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
