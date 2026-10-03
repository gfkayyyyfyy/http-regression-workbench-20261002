"""超出浮点范围的整数期望（如 1 后接 400 个 0）的端到端回归测试。

JSON 整数字面量按任意精度解析为 Python int，本身不存在无穷概念；
此前用例加载用 ``math.isfinite`` 统一校验 expected_value，对超出
float 范围的大整数会抛 OverflowError，使用例无法进入请求与报告流程。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 与响应字段均为完整十进制大整数（不加引号、不写
   1e400）时沿用既有数字断言：N==N、-N==-N 通过，error 为 null，
   退出码 0，field_check.expected/actual 保留完整整数（int），
   不转字符串、不截断或舍入；
2. 期望 N、实际 N+1 时字段检查失败（assertion_failed，退出码 1），
   报告 stdout 中保留两个完整整数；实际为同样数字文本的字符串或
   true 时同样失败并原样保留实际值与类型；
3. 状态码不符时仍检查字段；每次有效执行只发送一次 GET，stdout
   仅有一份可解析的 JSON 报告，stderr 为空且无 Traceback；
4. 非有限数字的拒绝规则保持不变：expected_value 为 1e400、NaN、
   Infinity 时 load_case 抛 CaseError；超过 Python 数字文本长度
   限制（默认 4300 位）的整数同样作为用例错误拒绝（不突破限制）。

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

from api_workbench.runner import ASSERTION_FAILED, CaseError, load_case

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIAGNOSTIC_PREFIX = "api_workbench:"

N = 10 ** 400
N_TEXT = str(N)
N_PLUS_ONE_TEXT = str(N + 1)


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


class BigIntegerTestCase(unittest.TestCase):
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

    def _write_raw_case(self, raw_expected_value: str) -> str:
        """按原始 JSON 文本写 expected_value，精确保留大整数的十进制写法。"""
        fields = {
            "name": "bigint",
            "url": f"http://127.0.0.1:{self.server.port}/count",
            "expected_status": 200,
            "field": "count",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def _run_raw(
        self,
        raw_expected_value: str,
        *,
        response_status: int = 200,
        response_body: bytes = b'{"count":1}',
    ) -> subprocess.CompletedProcess:
        """配置服务响应、写原始用例文本并经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case_path = self._write_raw_case(raw_expected_value)
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=15,
        )

    def _single_report(self, completed: subprocess.CompletedProcess) -> dict:
        """断言 stderr 为空且无 Traceback，stdout 有且仅有一个 JSON 报告。"""
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

        stdout_text = completed.stdout.decode("utf-8")

        def _reject_constant(value: str):
            raise AssertionError(f"报告包含非标准 JSON 常量: {value}")

        decoder = json.JSONDecoder(parse_constant=_reject_constant)
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text[:80]!r}",
        )
        self.assertIsInstance(report, dict)
        return report


class BigIntegerLoadingTests(BigIntegerTestCase):
    """load_case：任意精度整数合法，非有限小数与超长数字文本拒绝。"""

    def test_load_case_accepts_huge_integers(self) -> None:
        for raw, expected in [(N_TEXT, N), ("-" + N_TEXT, -N)]:
            with self.subTest(raw=raw[:8]):
                loaded = load_case(self._write_raw_case(raw))
                self.assertIs(type(loaded["expected_value"]), int)
                self.assertEqual(loaded["expected_value"], expected)
                self.assertTrue(loaded["expected_value"] == expected)

    def test_load_case_rejects_non_finite_spellings(self) -> None:
        # 非有限数字的拒绝规则不因大整数修复而放宽
        for raw in ("1e400", "-1E+400", "NaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw):
                with self.assertRaises(CaseError) as context:
                    load_case(self._write_raw_case(raw))
                self.assertIn("expected_value", str(context.exception))

    def test_integer_over_python_digit_limit_is_case_error(self) -> None:
        # 不突破 Python 对数字文本长度的现有限制：超长整数按用例错误拒绝，
        # 不逃逸成 Traceback
        case_path = self._write_raw_case("1" * 5000)
        with self.assertRaises(CaseError):
            load_case(case_path)

        completed = self._run_raw("1" * 5000)
        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
        self.assertTrue(stderr.startswith(DIAGNOSTIC_PREFIX), stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.server.get_count, 0, "非法用例不得发送 GET")


class BigIntegerAssertionTests(BigIntegerTestCase):
    """大整数期望沿用数字断言：精确相等、完整保留、失败语义不变。"""

    def test_equal_huge_integers_pass_with_full_values(self) -> None:
        completed = self._run_raw(
            N_TEXT, response_body=('{"count":' + N_TEXT + "}").encode()
        )
        report = self._single_report(completed)
        expected = report["field_check"]["expected"]
        actual = report["field_check"]["actual"]
        self.assertIs(type(expected), int)
        self.assertIs(type(actual), int)
        self.assertEqual(expected, N)
        self.assertEqual(actual, N)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        # 完整十进制整数必须出现在 stdout：不截断、不舍入、不转科学记数法
        stdout_text = completed.stdout.decode("utf-8")
        self.assertIn(N_TEXT, stdout_text)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_equal_negative_huge_integers_pass(self) -> None:
        completed = self._run_raw(
            "-" + N_TEXT, response_body=('{"count":-' + N_TEXT + "}").encode()
        )
        report = self._single_report(completed)
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertEqual(report["field_check"]["expected"], -N)
        self.assertEqual(report["field_check"]["actual"], -N)
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_huge_integer_differs_by_one_fails(self) -> None:
        completed = self._run_raw(
            N_TEXT,
            response_body=('{"count":' + N_PLUS_ONE_TEXT + "}").encode(),
        )
        report = self._single_report(completed)
        field_check = report["field_check"]
        self.assertEqual(field_check["expected"], N)
        self.assertEqual(field_check["actual"], N + 1)
        self.assertIs(type(field_check["actual"]), int)
        self.assertFalse(field_check["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)
        stdout_text = completed.stdout.decode("utf-8")
        self.assertIn(N_TEXT, stdout_text)
        self.assertIn(N_PLUS_ONE_TEXT, stdout_text)
        self.assertEqual(self.server.get_count, 1)

    def test_decimal_spellings_semantics_unchanged(self) -> None:
        # 小数与指数写法语义不变：普通有限值仍按 float 与整数精确比较
        for raw_expected, body, passed in [
            ("1", b'{"count":1.0}', True),
            ("1.0", b'{"count":1}', True),
            ("1e0", b'{"count":1}', True),
            (N_TEXT, b'{"count":1.0}', False),  # 大整数不可能等于有限小数
        ]:
            with self.subTest(raw_expected=raw_expected):
                completed = self._run_raw(raw_expected, response_body=body)
                report = self._single_report(completed)
                self.assertEqual(
                    report["field_check"]["passed"], passed
                )

    def test_string_with_same_digits_fails_and_is_kept_verbatim(self) -> None:
        completed = self._run_raw(
            N_TEXT,
            response_body=('{"count":"' + N_TEXT + '"}').encode(),
        )
        report = self._single_report(completed)
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), str)
        self.assertEqual(actual, N_TEXT)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_boolean_actual_fails_and_is_kept_verbatim(self) -> None:
        completed = self._run_raw(N_TEXT, response_body=b'{"count":true}')
        report = self._single_report(completed)
        self.assertIs(report["field_check"]["actual"], True)
        self.assertIs(type(report["field_check"]["actual"]), bool)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_checks_huge_integer_field(self) -> None:
        completed = self._run_raw(
            N_TEXT,
            response_status=500,
            response_body=('{"count":' + N_TEXT + "}").encode(),
        )
        report = self._single_report(completed)
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], N)
        self.assertTrue(
            report["field_check"]["passed"], "字段值相符时字段检查仍通过"
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
