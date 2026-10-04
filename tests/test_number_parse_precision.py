"""小数舍入边界的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证数字断言按 JSON 解析结果
精确比较的舍入边界行为：

1. 期望值与响应值都以原始 JSON 文本提供：超出 float64 精度的小数写法
   按解析结果比较——期望 1 匹配响应 {"count":1.0000000000000001}
   （解析后同为 1.0），交换两边的原始数字写法同样匹配；
2. 解析结果不同则失败：期望 1 不匹配 {"count":1.0000000000000002}
   （解析为 1.0000000000000002），actual 保留解析后的数字，
   field_check.passed 与总 passed 为 false，error 为 assertion_failed，
   退出码 1；
3. 下溢到零的小数仍是合法有限数字：期望 0 匹配 {"count":1e-400}
   （解析为 0.0），不得归为 invalid_response；
4. 报告中的 expected/actual 保持 JSON 数值类型（int/float 各自保留），
   field_check.present 为 true，状态码检查通过；每个场景只发送一次 GET，
   stdout 只有一个可按 UTF-8 解析的 JSON 报告，stderr 为空。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定端口、不需要手工启动服务或访问公网。
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

from api_workbench.runner import ASSERTION_FAILED

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _PrecisionServer(ThreadingHTTPServer):
    """按测试设定返回固定原始正文的受控服务，记录 GET 次数。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PrecisionHandler)
        self.port = self.server_address[1]
        self.body = b"{}"
        self.get_count = 0
        self._lock = threading.Lock()

    def set_body(self, body: bytes) -> None:
        with self._lock:
            self.body = body
            self.get_count = 0


class _PrecisionHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _PrecisionServer = self.server
        with server._lock:
            body = server.body
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class NumberParsePrecisionTests(unittest.TestCase):
    """小数舍入边界：按 JSON 解析结果精确比较的公开行为。"""

    def setUp(self) -> None:
        self.server = _PrecisionServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_case(self, raw_expected_value: str) -> str:
        """按原始 JSON 文本写 expected_value，精确保留小数写法。

        用例名固定为 number-precision，字段为 count，expected_status 为
        200，url 指向受控服务的 /count；文件以 UTF-8 写入临时目录。
        """
        lines = [
            '  "name": "number-precision"',
            f'  "url": "http://127.0.0.1:{self.server.port}/count"',
            '  "expected_status": 200',
            '  "field": "count"',
            f'  "expected_value": {raw_expected_value}',
        ]
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def _execute(self, raw_expected_value: str, raw_body: bytes) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务原始响应、写原始期望值用例、经公开入口执行并解析唯一报告。"""
        self.server.set_body(raw_body)
        case_path = self._write_case(raw_expected_value)
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

    def _assert_common_shape(self, report: dict, expected_value) -> None:
        """各场景共有结构：用例名/字段名/期望值对应输入，状态码检查通过，
        字段存在，且只发送一次 GET。"""
        self.assertEqual(report["name"], "number-precision")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "count")
        self.assertEqual(report["field_check"]["expected"], expected_value)
        self.assertIs(report["field_check"]["present"], True)
        self.assertEqual(self.server.get_count, 1, "每个场景只应发送一次 GET")

    def test_float_rounds_up_to_integer_expected(self) -> None:
        # 期望 1，响应 {"count":1.0000000000000001}：解析后同为 1.0，通过
        report, completed = self._execute("1", b'{"count":1.0000000000000001}')
        self._assert_common_shape(report, 1)
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 1.0)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)

    def test_integer_actual_matches_rounded_float_expected(self) -> None:
        # 交换两边：期望 1.0000000000000001（解析为 1.0），响应 {"count":1}
        report, completed = self._execute("1.0000000000000001", b'{"count":1}')
        self._assert_common_shape(report, 1.0)
        self.assertIs(type(report["field_check"]["expected"]), float)
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)

    def test_float_rounds_to_distinct_value_fails(self) -> None:
        # 期望 1，响应 {"count":1.0000000000000002}：解析为不同的 float，失败
        report, completed = self._execute("1", b'{"count":1.0000000000000002}')
        self._assert_common_shape(report, 1)
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 1.0000000000000002)
        self.assertNotEqual(report["field_check"]["actual"], 1.0)
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_underflow_to_zero_matches_zero_expected(self) -> None:
        # 期望 0，响应 {"count":1e-400}：下溢为有限 0.0，通过而非 invalid_response
        report, completed = self._execute("0", b'{"count":1e-400}')
        self._assert_common_shape(report, 0)
        self.assertIs(type(report["field_check"]["expected"]), int)
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 0.0)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
