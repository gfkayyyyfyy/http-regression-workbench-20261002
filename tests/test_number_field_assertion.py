"""数值字段相等断言的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 接受解析后有限的 JSON 数字（整数与小数均可）：
   数字期望只匹配数字实际值，按解析结果精确比较（无误差容限）——
   1、1.0 与 1e0 互相匹配，0 与 -0.0 相等；
2. 数字期望不匹配布尔、字符串、null、数组或对象：1 不匹配 true、
   0 不匹配 false；失败时 actual 保留实际 JSON 值及类型；
   字段缺失时 actual 为 null 且字段检查失败；
3. 报告的 field_check.expected/actual 保留各自的 JSON 类型
   （整数仍是整数、小数仍是小数、布尔不转成数字）；
4. expected_value 缺失、为数组或对象，或为未加引号的
   NaN/Infinity/-Infinity 及 1e400 这类解析后非有限的数字时，
   load_case 抛出 CaseError：不发送请求，stdout 为空，stderr 以
   api_workbench: 开头并指出 expected_value，退出码 2，无 Traceback；
   引号内的 "Infinity" 仍按既有字符串规则处理；
5. 数字期望不改变既有分类：状态码不符仍检查字段；非 JSON 对象或
   含非有限数字的响应仍为 invalid_response 并保留状态码检查结果；
   连接失败仍为 request_failed，两项 actual 为 null、所有 passed
   为 false，退出码 1；
6. 每次有效执行仅发送一次 GET。

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


class _NumberFlowTestCase(unittest.TestCase):
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

    def _write_raw_case(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 1e0/-0.0/NaN 等写法。"""
        fields = {
            "name": "number",
            "url": f"http://127.0.0.1:{self.server.port}/count",
            "expected_status": 200,
            "field": "count",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def _execute(
        self,
        *,
        expected_value,
        response_status: int = 200,
        response_body: bytes = b'{"count":1}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例（name=number, field=count）、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": "number",
            "url": f"http://127.0.0.1:{self.server.port}/count",
            "expected_status": 200,
            "field": "count",
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


class NumberExpectedValueMatchingTests(_NumberFlowTestCase):
    """数字期望：按解析结果精确比较，报告保留 JSON 数字类型。"""

    def test_integer_expected_matches_integer_actual(self) -> None:
        report, completed = self._execute(
            expected_value=1, response_body=b'{"count":1}'
        )
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertEqual(report["field_check"]["expected"], 1)
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_integer_expected_matches_float_and_exponent_actuals(self) -> None:
        # 验收场景：GET /count 返回 {"count":1.0}，expected_value 为 1 时通过；
        # 1、1.0 与 1e0 解析后相等，无误差容限但按数值比较
        for body in (b'{"count":1.0}', b'{"count":1e0}', b'{"count":1.00}'):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=1, response_body=body
                )
                self.assertTrue(
                    report["field_check"]["passed"],
                    f"期望 1 应匹配实际值: {body!r}",
                )
                self.assertEqual(report["field_check"]["actual"], 1)
                self.assertIs(type(report["field_check"]["actual"]), float)
                self.assertTrue(report["passed"])
                self.assertIsNone(report["error"])
                self.assertEqual(completed.returncode, 0)

    def test_float_expected_matches_integer_actual(self) -> None:
        report, completed = self._execute(
            expected_value=1.0, response_body=b'{"count":1}'
        )
        self.assertIs(type(report["field_check"]["expected"]), float)
        self.assertEqual(report["field_check"]["expected"], 1.0)
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(completed.returncode, 0)

    def test_zero_expected_matches_negative_zero_actual(self) -> None:
        # 0 与 -0.0 按解析结果相等（两个方向都验证）
        report, completed = self._execute(
            expected_value=0, response_body=b'{"count":-0.0}'
        )
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], 0)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)

        report, completed = self._execute(
            expected_value=-0.0, response_body=b'{"count":0}'
        )
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

    def test_fraction_and_large_finite_exponent_match(self) -> None:
        # 有限小数与大指数写法照常精确匹配
        for expected, body in [
            (1.5, b'{"count":1.5}'),
            (-2.5, b'{"count":-2.5}'),
            (1e308, b'{"count":1e308}'),
            (0.1, b'{"count":0.1}'),
        ]:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertTrue(report["field_check"]["passed"])
                self.assertEqual(report["field_check"]["actual"], expected)
                self.assertIsNone(report["error"])
                self.assertEqual(completed.returncode, 0)

    def test_number_expected_does_not_match_boolean_actual(self) -> None:
        # 验收场景：响应改为 {"count":true} 后同一用例失败，actual 保留布尔值；
        # 1 不匹配 true、0 不匹配 false（True/False 是 int 子类，必须排除）
        for expected, body, actual in [
            (1, b'{"count":true}', True),
            (0, b'{"count":false}', False),
        ]:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertIs(report["field_check"]["actual"], actual)
                self.assertIs(type(report["field_check"]["actual"]), bool)
                self.assertFalse(report["field_check"]["passed"])
                self.assertFalse(report["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_number_expected_does_not_match_other_types(self) -> None:
        # 字符串、null、数组、对象均不匹配数字期望；actual 原样保留
        for body, expected_actual in [
            (b'{"count":"1"}', "1"),
            (b'{"count":null}', None),
            (b'{"count":[1]}', [1]),
            (b'{"count":{"v":1}}', {"v": 1}),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=1, response_body=body
                )
                self.assertEqual(report["field_check"]["actual"], expected_actual)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_number_expected_does_not_match_different_number(self) -> None:
        # 精确比较：不相等的数字判失败，actual 保留数值及类型
        for body, expected_actual in [
            (b'{"count":2}', 2),
            (b'{"count":1.5}', 1.5),
            (b'{"count":-1}', -1),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=1, response_body=body
                )
                self.assertEqual(report["field_check"]["actual"], expected_actual)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_missing_field_is_null_actual_with_number_expected(self) -> None:
        report, completed = self._execute(
            expected_value=1, response_body=b'{"other":1}'
        )
        self.assertIsNone(report["field_check"]["actual"], "字段缺失时 actual 为 null")
        self.assertEqual(report["field_check"]["expected"], 1)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_checks_number_field(self) -> None:
        # 状态码 500 ≠ 200，但数字字段相符：字段检查必须保持通过
        report, completed = self._execute(
            expected_value=1,
            response_status=500,
            response_body=b'{"count":1}',
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertTrue(report["field_check"]["passed"], "字段值相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class NumberExpectedErrorClassificationTests(_NumberFlowTestCase):
    """数字期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_non_object_body_is_invalid_response_with_number_expected(self) -> None:
        for body in (b"not json", b"[1, 2]"):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=1, response_body=body
                )
                self.assertEqual(report["field_check"]["expected"], 1)
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_non_finite_number_body_is_invalid_response(self) -> None:
        # 目标字段相符，但其他字段含 1e400 这类解析后非有限的数字：整份正文无效
        report, completed = self._execute(
            expected_value=1,
            response_body=b'{"count":1,"extra":1e400}',
        )
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)

    def test_connection_failure_is_request_failed_with_number_expected(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        case = {
            "name": "number",
            "url": f"http://127.0.0.1:{refused_port}/count",
            "expected_status": 200,
            "field": "count",
            "expected_value": 1,
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
        self.assertEqual(report["field_check"]["expected"], 1)
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.stderr, b"")


class NumberExpectedValueValidationTests(_NumberFlowTestCase):
    """expected_value 的加载校验：有限数字合法，非有限数字与容器类型拒绝。"""

    # 原始 JSON 文本：缺失、数组、对象及解析后非有限的数字
    INVALID_RAW_VALUES = [
        ("missing", None),  # None 占位表示不写入该字段
        ("array", "[]"),
        ("object", "{}"),
        ("nan", "NaN"),
        ("infinity", "Infinity"),
        ("negative_infinity", "-Infinity"),
        ("overflow", "1e400"),
        ("negative_overflow", "-1e400"),
    ]

    def test_load_case_raises_case_error_for_invalid_values(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                case_path = self._write_raw_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_invalid_values_without_sending_request(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                self.server.set_scenario(200, b'{"count":1}')
                case_path = self._write_raw_case(raw_value)
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

    def test_load_case_accepts_finite_numbers_and_preserves_types(self) -> None:
        # 整数、小数、负零、大指数写法均为合法期望；解析后的类型原样保留
        for raw_value, expected, expected_type in [
            ("0", 0, int),
            ("1", 1, int),
            ("-3", -3, int),
            ("1.5", 1.5, float),
            ("1e0", 1.0, float),
            ("-0.0", -0.0, float),
            ("1e308", 1e308, float),
        ]:
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(loaded["expected_value"], expected)
                self.assertIs(type(loaded["expected_value"]), expected_type)

    def test_quoted_infinity_remains_a_string_expectation(self) -> None:
        # 引号内的 "Infinity" 是普通字符串期望，按既有字符串规则处理
        case_path = self._write_raw_case('"Infinity"')
        loaded = load_case(case_path)
        self.assertEqual(loaded["expected_value"], "Infinity")
        self.assertIs(type(loaded["expected_value"]), str)

        # 字符串 "Infinity" 期望匹配字符串实际值，不匹配数字或其他内容
        self.server.set_scenario(200, b'{"count":"Infinity"}')
        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(report["field_check"]["expected"], "Infinity")
        self.assertEqual(report["field_check"]["actual"], "Infinity")
        self.assertTrue(report["field_check"]["passed"])
        self.assertIsNone(report["error"])

        self.server.set_scenario(200, b'{"count":1}')
        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)


if __name__ == "__main__":
    unittest.main()
