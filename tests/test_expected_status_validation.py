"""expected_status 输入校验回归测试（仅使用 Python 标准库与受控本地服务）。

固定公开规则：expected_status 必须是 100 至 599 的整数，布尔值不被接受。
无效输入经 `python -m api_workbench run case.json` 以退出码 2 拒绝，
不发送任何请求；有效边界（100、599、200）不被误拒绝。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 类型无效：缺失、null、布尔、字符串、浮点（不得自动转整数）
INVALID_TYPE_CASES = [
    ("missing", None),  # None 占位表示不写入该字段
    ("null", "null"),
    ("true", "true"),
    ("false", "false"),
    ("string", '"200"'),
    ("float", "200.0"),
]

# 整数但超出 100–599 范围
INVALID_RANGE_CASES = [("too_low", "99"), ("too_high", "600")]


def _write_case(raw_expected_status: str | None, url: str) -> str:
    """生成与 cases/health.json 同构的临时用例，仅变更 expected_status。

    raw_expected_status 为 None 时省略该字段；否则按原始 JSON 文本写入，
    以精确保留 null/true/false/"200"/200.0 等类型。
    """
    fields = {
        "name": "health",
        "url": url,
        "field": "status",
        "expected_value": "ok",
    }
    lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
    if raw_expected_status is not None:
        lines.append(f'  "expected_status": {raw_expected_status}')
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("{\n" + ",\n".join(lines) + "\n}\n")
    return path


class _CountingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数的受控服务：固定返回 200 与 {"status":"ok"}。"""

    get_count = 0
    _lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.get_count = 0

    @classmethod
    def count(cls) -> int:
        with cls._lock:
            return cls.get_count

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
        body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class ExpectedStatusValidationTests(unittest.TestCase):
    """每个测试使用独立的 127.0.0.1 临时端口服务，结束即释放。"""

    def setUp(self) -> None:
        _CountingHandler.reset()
        self.httpd = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}/health"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _run_cli(self, case_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _assert_rejected(self, raw_value: str | None, reason: str) -> None:
        case_path = _write_case(raw_value, self.url)
        self.addCleanup(os.unlink, case_path)
        completed = self._run_cli(case_path)
        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(completed.returncode, 2, stderr)
        self.assertEqual(completed.stdout, b"", "非法用例不得输出 JSON 报告")
        self.assertTrue(
            stderr.startswith("api_workbench:"),
            f"stderr 应沿用 api_workbench: 前缀: {stderr!r}",
        )
        self.assertIn("expected_status", stderr)
        self.assertIn(reason, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(
            _CountingHandler.count(),
            0,
            f"expected_status={raw_value!r} 非法时不得发送任何 GET",
        )

    def test_invalid_types_rejected(self) -> None:
        for label, raw_value in INVALID_TYPE_CASES:
            with self.subTest(label=label, raw_value=raw_value):
                _CountingHandler.reset()
                self._assert_rejected(raw_value, "必须为整数")

    def test_out_of_range_rejected(self) -> None:
        for label, raw_value in INVALID_RANGE_CASES:
            with self.subTest(label=label, raw_value=raw_value):
                _CountingHandler.reset()
                self._assert_rejected(raw_value, "100 至 599")

    def test_valid_boundaries_not_rejected(self) -> None:
        for expected_status in (100, 599, 200):
            with self.subTest(expected_status=expected_status):
                _CountingHandler.reset()
                case_path = _write_case(str(expected_status), self.url)
                self.addCleanup(os.unlink, case_path)
                completed = self._run_cli(case_path)
                self.assertEqual(completed.stderr, b"", "有效用例 stderr 应为空")
                report = json.loads(completed.stdout.decode("utf-8"))

                self.assertEqual(report["name"], "health")
                self.assertEqual(report["status_check"]["expected"], expected_status)
                self.assertEqual(report["status_check"]["actual"], 200)
                self.assertTrue(report["field_check"]["passed"])
                self.assertEqual(report["field_check"]["actual"], "ok")
                self.assertEqual(
                    _CountingHandler.count(),
                    1,
                    "有效用例每次执行应恰好发送一次 GET",
                )

                if expected_status == 200:
                    self.assertEqual(completed.returncode, 0)
                    self.assertIsNone(report["error"])
                    self.assertTrue(report["passed"])
                    self.assertTrue(report["status_check"]["passed"])
                else:
                    # 边界值合法但与实际 200 不符：状态码检查失败、字段检查通过
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(report["error"], "assertion_failed")
                    self.assertFalse(report["passed"])
                    self.assertFalse(report["status_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
