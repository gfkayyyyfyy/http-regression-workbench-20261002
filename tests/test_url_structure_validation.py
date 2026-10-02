"""URL 结构无效用例的回归测试：括号不成对等地址必须在连接前按用例错误拒绝。"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from api_workbench.runner import CaseError, load_case, run_case

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(PROJECT_ROOT, "cases")

# 以 cases/health.json 为基础、仅替换 url 的两个已提交用例
BRACKET_OPEN_CASE = os.path.join(CASES_DIR, "health_bracket_open.json")
BRACKET_CLOSE_CASE = os.path.join(CASES_DIR, "health_bracket_close.json")
BRACKET_URLS = [
    "http://[127.0.0.1:8765/health",
    "http://127.0.0.1]:8765/health",
]

# 其他在 URL 解析阶段即被判定为结构无效的地址，结果必须一致
OTHER_INVALID_URLS = ["http://[::1", "http://[zz]/"]


def _write_case(url: str) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。"""
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(case, handle, ensure_ascii=False)
    return path


class InvalidUrlStructureTests(unittest.TestCase):
    def test_load_case_raises_case_error_for_case_files(self) -> None:
        for case_path, url in [
            (BRACKET_OPEN_CASE, BRACKET_URLS[0]),
            (BRACKET_CLOSE_CASE, BRACKET_URLS[1]),
        ]:
            with self.subTest(case=os.path.basename(case_path)):
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("结构无效", message)
                self.assertIn(url, message)

    def test_load_case_raises_case_error_for_other_invalid_urls(self) -> None:
        for url in OTHER_INVALID_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("结构无效", message)
                self.assertIn(url, message)

    def test_run_case_rejects_before_connection(self) -> None:
        for url in BRACKET_URLS + OTHER_INVALID_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                stdout, stderr = io.BytesIO(), io.StringIO()
                with patch("api_workbench.runner.HTTPConnection") as connection_cls:
                    with (
                        patch("sys.stdout", stdout),
                        patch("sys.stderr", stderr),
                    ):
                        exit_code = run_case(case_path)

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), b"", "非法用例不得输出报告")
                message = stderr.getvalue()
                self.assertTrue(
                    message.startswith("api_workbench:"),
                    f"stderr 应沿用 api_workbench: 前缀: {message!r}",
                )
                self.assertEqual(message.count("\n"), 1, "应只输出单条诊断")
                self.assertIn("结构无效", message)
                self.assertIn(url, message, "诊断必须包含原始地址")
                self.assertNotIn("Traceback", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"结构无效时不得建立连接: {url}",
                )

    def test_committed_case_files_rejected_as_subprocess(self) -> None:
        for case_path, url in [
            (BRACKET_OPEN_CASE, BRACKET_URLS[0]),
            (BRACKET_CLOSE_CASE, BRACKET_URLS[1]),
        ]:
            with self.subTest(case=os.path.basename(case_path)):
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, b"")
                stderr = completed.stderr.decode("utf-8")
                self.assertTrue(stderr.startswith("api_workbench:"))
                self.assertEqual(stderr.count("\n"), 1)
                self.assertIn("结构无效", stderr)
                self.assertIn(url, stderr)
                self.assertNotIn("Traceback", stderr)


class ExampleServiceControlTests(unittest.TestCase):
    """对照：在既有示例服务上，两个已提交用例的行为保持不变。"""

    def setUp(self) -> None:
        self.server = subprocess.Popen(
            [sys.executable, "-m", "api_workbench", "serve"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.skipTest("示例服务未能绑定 127.0.0.1:8765，跳过对照用例")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", 8765)) == 0:
                    return
            time.sleep(0.05)
        self.tearDown()
        self.skipTest("示例服务在 127.0.0.1:8765 上未就绪，跳过对照用例")

    def tearDown(self) -> None:
        if self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=5)

    def _run(self, case_name: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "api_workbench",
                "run",
                os.path.join(CASES_DIR, case_name),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def test_health_case_passes(self) -> None:
        completed = self._run("health.json")
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])

    def test_wrong_value_case_fails_assertion(self) -> None:
        completed = self._run("health_wrong_value.json")
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], "assertion_failed")


if __name__ == "__main__":
    unittest.main()
