"""expected_status 输入校验的端到端回归测试。

固定 run 入口对期望状态码的公开规则：必须是 100 至 599 之间的整数，
布尔值、字符串、浮点数、null、缺失均按用例错误拒绝（退出码 2），
且拒绝发生在发送请求之前。同时覆盖 100/200/599 三个有效边界不被误拒绝。

以 cases/health.json 为有效基础，仅变更 expected_status；地址指向临时端口上
固定返回 200 与 {"status":"ok"} 的受控 127.0.0.1 服务，其他输入保持有效，
避免 URL 或字段错误遮蔽本次校验。仅使用 Python 标准库 unittest，
不依赖固定 8765 端口或公网，可由
``python -m unittest discover -s tests`` 收集执行。
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与 cases/health.json 同构的有效基础：仅 expected_status 由各用例变更
BASE_CASE = {
    "name": "health",
    "url": None,  # setUp 后填入受控服务地址
    "expected_status": 200,
    "field": "status",
    "expected_value": "ok",
}

RESPONSE_BODY = b'{"status":"ok"}'

# 类型错误：缺失、null、布尔值、字符串、浮点数均不得隐式转换为整数
TYPE_ERROR_CASES = [
    ("missing", None, True),
    ("null", None, False),
    ("true", True, False),
    ("false", False, False),
    ('"200"', "200", False),
    ("200.0", 200.0, False),
]

# 范围错误：整数但不在 100–599 闭区间内
OUT_OF_RANGE_CASES = [99, 600]


class _FixedHealthServer(ThreadingHTTPServer):
    """固定对所有 GET 返回 200 与 {"status":"ok"}，并统计 GET 次数。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixedHealthHandler)
        self.port = self.server_address[1]
        self.get_count = 0
        self._lock = threading.Lock()

    def reset_count(self) -> None:
        with self._lock:
            self.get_count = 0


class _FixedHealthHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(RESPONSE_BODY)))
        self.end_headers()
        self.wfile.write(RESPONSE_BODY)
        server: _FixedHealthServer = self.server
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class ExpectedStatusValidationTests(unittest.TestCase):
    """通过公开入口 python -m api_workbench run case.json 验证校验规则。"""

    def setUp(self) -> None:
        self.server = _FixedHealthServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_case(self, expected_status, *, omit: bool = False) -> str:
        """生成与 health 用例同构的临时用例，可整体省略 expected_status 键。"""
        case = dict(BASE_CASE)
        case["url"] = f"http://127.0.0.1:{self.server.port}/health"
        if omit:
            del case["expected_status"]
        else:
            case["expected_status"] = expected_status
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)
        return case_path

    def _run(self, case_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _assert_rejected_without_request(
        self, completed: subprocess.CompletedProcess, label: str
    ) -> None:
        """无效输入的统一命令行结果：退出码 2、空 stdout、单条类型/范围诊断、无 Traceback。"""
        self.assertEqual(
            completed.returncode, 2, f"[{label}] 应以退出码 2 拒绝: {completed.stderr!r}"
        )
        self.assertEqual(completed.stdout, b"", f"[{label}] 拒绝时不得产生 JSON 报告")
        stderr = completed.stderr.decode("utf-8")
        self.assertTrue(
            stderr.startswith("api_workbench:"),
            f"[{label}] stderr 应以 api_workbench: 开头: {stderr!r}",
        )
        self.assertIn("expected_status", stderr, f"[{label}] 诊断必须指明字段名")
        self.assertNotIn("Traceback", stderr, f"[{label}] 不得泄漏 Traceback")
        self.assertEqual(stderr.count("\n"), 1, f"[{label}] 应只输出单条诊断")

    def test_non_integer_values_rejected_by_type(self) -> None:
        for label, value, omit in TYPE_ERROR_CASES:
            with self.subTest(expected_status=label):
                case_path = self._write_case(value, omit=omit)
                self.server.reset_count()
                completed = self._run(case_path)

                self._assert_rejected_without_request(completed, label)
                self.assertIn(
                    "整数",
                    completed.stderr.decode("utf-8"),
                    f"[{label}] 应按类型错误说明原因",
                )
                if label in ("true", "false"):
                    self.assertIn("布尔", completed.stderr.decode("utf-8"))
                # 不能仅凭退出码判断：服务必须确认一个 GET 都没收到
                self.assertEqual(
                    self.server.get_count,
                    0,
                    f"[{label}] 类型无效时必须在连接前拒绝，不得发送 GET",
                )

    def test_out_of_range_integers_rejected(self) -> None:
        for value in OUT_OF_RANGE_CASES:
            with self.subTest(expected_status=value):
                case_path = self._write_case(value)
                self.server.reset_count()
                completed = self._run(case_path)

                self._assert_rejected_without_request(completed, str(value))
                stderr = completed.stderr.decode("utf-8")
                self.assertIn("100", stderr)
                self.assertIn("599", stderr)
                self.assertIn(
                    "之间", stderr, f"[{value}] 应按超出范围说明原因"
                )
                self.assertEqual(
                    self.server.get_count,
                    0,
                    f"[{value}] 超出范围时必须在连接前拒绝，不得发送 GET",
                )

    def _assert_valid_boundary(
        self, expected_status: int, *, passes: bool
    ) -> subprocess.CompletedProcess:
        case_path = self._write_case(expected_status)
        self.server.reset_count()
        completed = self._run(case_path)

        self.assertEqual(completed.stderr, b"", f"[{expected_status}] stderr 必须为空")
        self.assertNotIn(b"Traceback", completed.stderr)

        stdout_text = completed.stdout.decode("utf-8")
        report = json.loads(stdout_text)
        # stdout 有且仅有一个 JSON 报告（不允许多余内容）
        decoder = json.JSONDecoder()
        _, end = decoder.raw_decode(stdout_text)
        self.assertEqual(stdout_text[end:].strip(), "")
        self.assertIsInstance(report, dict)

        self.assertEqual(
            self.server.get_count, 1, f"[{expected_status}] 每次执行恰好发送一次 GET"
        )
        # 报告保留原始期望状态码与用例名称
        self.assertEqual(report["name"], BASE_CASE["name"])
        self.assertEqual(report["status_check"]["expected"], expected_status)
        self.assertEqual(report["status_check"]["actual"], 200)
        # 服务固定返回 {"status":"ok"}，字段检查始终通过
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        return completed, report

    def test_lower_boundary_100_runs_and_fails_status_only(self) -> None:
        completed, report = self._assert_valid_boundary(100, passes=False)
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], "assertion_failed")
        self.assertEqual(completed.returncode, 1)

    def test_upper_boundary_599_runs_and_fails_status_only(self) -> None:
        completed, report = self._assert_valid_boundary(599, passes=False)
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], "assertion_failed")
        self.assertEqual(completed.returncode, 1)

    def test_matching_status_200_passes(self) -> None:
        completed, report = self._assert_valid_boundary(200, passes=True)
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
