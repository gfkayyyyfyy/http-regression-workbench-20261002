"""超大整数期望（如 1 后接 400 个 0）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 合法 JSON 整数无论位数多少（超出 float 范围的 10**400 及其负数），
   load_case 均正常接受，沿用既有数字断言：期望与实际同为 N 或同为 -N
   时两项检查通过、error 为 null、退出码 0；期望 N、实际 N+1 时字段检查
   失败（assertion_failed，退出码 1）；
2. field_check.expected/actual 保留完整整数数值（int 类型），不转字符串、
   不截断或舍入；实际是同样数字文本的字符串或布尔值时仍失败并原样保留；
3. 非有限数字的拒绝规则保持不变：expected_value 为 1e400、NaN、Infinity、
   -Infinity 时 load_case 抛 CaseError，不发送请求，stdout 为空，
   stderr 以 api_workbench: 开头并指出 expected_value，退出码 2；
4. 响应中出现 1e400 仍为 invalid_response，保留状态码检查，字段 actual 为
   null，退出码 1；状态码不符时仍检查字段；每次有效执行仅发送一次 GET，
   stdout 仅有一份可解析的 JSON 报告，stderr 为空且无 Traceback。

不测试突破 Python 数字文本长度限制（sys.set_int_max_str_digits）的情形。

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

from api_workbench.runner import ASSERTION_FAILED, INVALID_RESPONSE, load_case

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


def _reject_constant(value: str):
    """严格校验报告本身：stdout 中不得出现 Infinity/NaN 等非标准字面量。"""
    raise ValueError(f"报告包含非标准 JSON 常量: {value}")


_STRICT_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


class HugeIntegerTestCase(unittest.TestCase):
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
        """按原始 JSON 文本写 expected_value，精确保留 1000…0、1e400 等写法。"""
        lines = [
            f'  "name": "bigint"',
            f'  "url": "http://127.0.0.1:{self.server.port}/count"',
            '  "expected_status": 200',
            '  "field": "count"',
            f'  "expected_value": {raw_expected_value}',
        ]
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def _run(self, raw_expected_value: str) -> tuple[dict, subprocess.CompletedProcess]:
        """配置好场景后经公开入口执行，严格解析唯一一份 stdout 报告。"""
        case_path = self._write_raw_case(raw_expected_value)
        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=15,
        )
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

        stdout_text = completed.stdout.decode("utf-8")
        report, end = _STRICT_DECODER.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text[:80]!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _execute(
        self,
        raw_expected_value: str,
        *,
        response_status: int = 200,
        response_body: bytes,
    ) -> tuple[dict, subprocess.CompletedProcess]:
        self.server.set_scenario(response_status, response_body)
        return self._run(raw_expected_value)


class HugeIntegerLoadingTests(HugeIntegerTestCase):
    """load_case 接受任意位数的合法整数。"""

    def test_load_case_accepts_huge_integer(self) -> None:
        for raw, expected in [
            (N_TEXT, N),
            ("-" + N_TEXT, -N),
            (N_PLUS_ONE_TEXT, N + 1),
        ]:
            with self.subTest(raw=raw[:10]):
                loaded = load_case(self._write_raw_case(raw))
                self.assertIs(type(loaded["expected_value"]), int)
                self.assertEqual(loaded["expected_value"], expected)

    def test_load_case_still_rejects_non_finite_numbers(self) -> None:
        # 整数放行不得放宽既有非有限数字拒绝规则
        for raw in ["1e400", "-1E+400", "NaN", "Infinity", "-Infinity"]:
            with self.subTest(raw=raw):
                from api_workbench.runner import CaseError

                with self.assertRaises(CaseError) as context:
                    load_case(self._write_raw_case(raw))
                self.assertIn("expected_value", str(context.exception))


class HugeIntegerMatchingTests(HugeIntegerTestCase):
    """超大整数期望沿用既有数字断言，报告保留完整整数。"""

    def test_equal_huge_integers_pass(self) -> None:
        for raw_expected, body, expected in [
            (N_TEXT, ('{"count":' + N_TEXT + "}").encode(), N),
            ("-" + N_TEXT, ('{"count":-' + N_TEXT + "}").encode(), -N),
        ]:
            with self.subTest(sign="-" if expected < 0 else "+"):
                report, completed = self._execute(raw_expected, response_body=body)
                self.assertIsNone(report["error"])
                self.assertEqual(completed.returncode, 0)
                self.assertTrue(report["status_check"]["passed"])
                self.assertTrue(report["field_check"]["passed"])
                self.assertTrue(report["passed"])
                self.assertIs(type(report["field_check"]["expected"]), int)
                self.assertIs(type(report["field_check"]["actual"]), int)
                self.assertEqual(report["field_check"]["expected"], expected)
                self.assertEqual(report["field_check"]["actual"], expected)
                self.assertEqual(self.server.get_count, 1)

    def test_huge_integer_off_by_one_fails_with_full_values(self) -> None:
        report, completed = self._execute(
            N_TEXT,
            response_body=('{"count":' + N_PLUS_ONE_TEXT + "}").encode(),
        )
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertEqual(report["field_check"]["expected"], N)
        self.assertEqual(report["field_check"]["actual"], N + 1)
        # 完整十进制文本：不截断、不舍入、不转字符串
        self.assertEqual(str(report["field_check"]["expected"]), N_TEXT)
        self.assertEqual(str(report["field_check"]["actual"]), N_PLUS_ONE_TEXT)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(self.server.get_count, 1)

    def test_same_digits_as_string_still_fails_and_kept_verbatim(self) -> None:
        report, completed = self._execute(
            N_TEXT,
            response_body=('{"count":"' + N_TEXT + '"}').encode(),
        )
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertIs(type(report["field_check"]["actual"]), str)
        self.assertEqual(report["field_check"]["actual"], N_TEXT)
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertFalse(report["field_check"]["passed"])

    def test_boolean_actual_still_fails_and_kept_as_boolean(self) -> None:
        report, completed = self._execute(N_TEXT, response_body=b'{"count":true}')
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertIs(report["field_check"]["actual"], True)
        self.assertIs(type(report["field_check"]["actual"]), bool)
        self.assertFalse(report["field_check"]["passed"])

    def test_finite_float_actual_does_not_match_huge_integer(self) -> None:
        # N 与任何有限 float 都不可能相等：不匹配，且不得抛 OverflowError
        report, completed = self._execute(N_TEXT, response_body=b'{"count":1.0}')
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 1.0)
        self.assertFalse(report["field_check"]["passed"])

    def test_status_mismatch_still_checks_huge_integer_field(self) -> None:
        report, completed = self._execute(
            N_TEXT,
            response_status=500,
            response_body=('{"count":' + N_TEXT + "}").encode(),
        )
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"], "字段相符时即使状态码不符也通过")
        self.assertEqual(report["field_check"]["actual"], N)
        self.assertEqual(self.server.get_count, 1)


class HugeIntegerErrorClassificationTests(HugeIntegerTestCase):
    """超大整数期望下既有非有限数字拒绝与 invalid_response 分类不变。"""

    def test_non_finite_expected_rejected_without_request(self) -> None:
        self.server.set_scenario(200, ('{"count":' + N_TEXT + "}").encode())
        for raw in ["1e400", "NaN", "Infinity", "-Infinity"]:
            with self.subTest(raw=raw):
                case_path = self._write_raw_case(raw)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=15,
                )
                stderr = completed.stderr.decode("utf-8")
                self.assertEqual(completed.returncode, 2, stderr)
                self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
                self.assertTrue(stderr.startswith(DIAGNOSTIC_PREFIX), stderr)
                self.assertIn("expected_value", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(self.server.get_count, 0, f"{raw} 非法时不得发送 GET")

    def test_overflow_response_is_invalid_with_huge_integer_expected(self) -> None:
        report, completed = self._execute(N_TEXT, response_body=b'{"count":1e400}')
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertEqual(report["field_check"]["expected"], N)
        self.assertEqual(self.server.get_count, 1)

    def test_overflow_response_with_status_mismatch_keeps_status_result(self) -> None:
        report, completed = self._execute(
            N_TEXT,
            response_status=500,
            response_body=b'{"count":1e400}',
        )
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
