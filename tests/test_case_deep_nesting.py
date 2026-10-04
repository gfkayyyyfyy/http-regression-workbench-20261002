"""用例文件 JSON 嵌套过深在解析阶段触发 RecursionError 时的回归测试。

以 ``cases/health.json`` 的全部必需字段为基础追加 ``extra`` 字段验证：

1. ``extra`` 的值为足以触发当前解析器递归错误的多层单元素数组包裹
   数字 0（2000 层）时，直接调用 ``load_case`` 抛出 ``CaseError``，
   说明包含传入的文件路径，并指出 JSON 嵌套过深、无法完成解析；
2. ``extra`` 换成多层对象包裹数字时按同一约定拒绝；
3. 深层内容位于顶层数据（如顶层本身是深层嵌套数组）时结果相同，
   不能跳过深层内容、只凭已读到的合法字段继续执行；
4. 通过公开入口 ``python -m api_workbench run case.json`` 执行同一文件：
   退出码为 2，stdout 为空（不生成执行报告），stderr 以
   ``api_workbench:`` 开头、包含路径与原因、不出现 Traceback，
   且整个运行不建立 HTTP 连接、请求数为零——即使 name、url 与
   断言字段都已写成合法值；
5. 兼容对照：``extra`` 改为浅层嵌套（两层数组）时仍被忽略，本地示例
   服务返回 200 与 {"status":"ok"} 时只发送一次 GET，stdout 输出
   一份成功 JSON 报告并以 0 退出；
6. 可解析的对象或浅层嵌套数组仍不能成为 ``expected_value``：
   按既有字段校验约定以退出码 2 拒绝，与嵌套深度无关。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不修改运行时递归上限，也不依赖固定嵌套层数（2000 层只是远超当前
运行时默认递归上限 1000 的输入）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_workbench.cli import main
from api_workbench.runner import CaseError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(PROJECT_ROOT, "cases")
STATIC_DEEP_ARRAY_CASE = os.path.join(CASES_DIR, "health_deep_array.json")
STATIC_DEEP_OBJECT_CASE = os.path.join(CASES_DIR, "health_deep_object.json")

DIAGNOSTIC_PREFIX = "api_workbench:"

# 2000 层远超 Python 默认递归上限（1000）：纯 Python JSON 扫描器
# 每层嵌套都消耗 Python 递归栈，解析必然抛出 RecursionError。
DEEP_DEPTH = 2000


def _deep_array(depth: int = DEEP_DEPTH) -> str:
    """多层单元素数组包裹数字 0 的 JSON 文本。"""
    return "[" * depth + "0" + "]" * depth


def _deep_object(depth: int = DEEP_DEPTH) -> str:
    """多层单键对象包裹数字 0 的 JSON 文本。"""
    return '{"a":' * depth + "0" + "}" * depth


class _HealthServer(ThreadingHTTPServer):
    """仅在 GET /health 上返回 200 与 {"status":"ok"} 的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server_address[1]
        self.get_count = 0
        self._lock = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self.get_count += 1


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _HealthServer = self.server
        server.record()
        body = b'{"status":"ok"}' if self.path == "/health" else b"{}"
        status = 200 if self.path == "/health" else 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class LoadCaseDeepNestingTests(unittest.TestCase):
    """直接调用 load_case：深层嵌套统一抛 CaseError。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_bytes(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _write_health_with_extra(self, name: str, extra_json: str) -> str:
        """以 cases/health.json 的全部必需字段为基础，追加 extra 字段。"""
        data = (
            "{\n"
            '  "name": "health",\n'
            '  "url": "http://127.0.0.1:8765/health",\n'
            '  "expected_status": 200,\n'
            '  "field": "status",\n'
            '  "expected_value": "ok",\n'
            f'  "extra": {extra_json}\n'
            "}\n"
        ).encode("utf-8")
        return self._write_bytes(name, data)

    def _assert_case_error(self, path: str) -> None:
        with self.assertRaises(CaseError) as context:
            from api_workbench.runner import load_case

            load_case(path)
        message = str(context.exception)
        # 错误说明必须包含传入的文件路径，并指出嵌套过深、无法完成解析
        self.assertIn(path, message)
        self.assertIn("JSON 嵌套过深", message)
        self.assertIn("无法完成解析", message)

    def test_static_deep_array_case_raises_case_error(self) -> None:
        # 仓库内静态用例：多层单元素数组包裹 0，必需字段全部合法
        self.assertTrue(os.path.exists(STATIC_DEEP_ARRAY_CASE))
        self._assert_case_error(STATIC_DEEP_ARRAY_CASE)

    def test_static_deep_object_case_raises_case_error(self) -> None:
        # 换成多层对象也按相同约定拒绝
        self.assertTrue(os.path.exists(STATIC_DEEP_OBJECT_CASE))
        self._assert_case_error(STATIC_DEEP_OBJECT_CASE)

    def test_deep_array_in_extra_field_raises_case_error(self) -> None:
        path = self._write_health_with_extra("deep_extra_array.json", _deep_array())
        self._assert_case_error(path)

    def test_deep_object_in_extra_field_raises_case_error(self) -> None:
        path = self._write_health_with_extra("deep_extra_object.json", _deep_object())
        self._assert_case_error(path)

    def test_deeply_nested_top_level_value_raises_case_error(self) -> None:
        # 深层内容位于顶层数据（顶层本身是深层嵌套数组）时同一结果：
        # 解析阶段即失败，不落入“顶层必须是对象”的字段校验分支
        path = self._write_bytes("deep_top_level.json", _deep_array().encode("utf-8"))
        self._assert_case_error(path)

    def test_error_is_value_error_subclass(self) -> None:
        # CaseError 沿用既有配置错误约定（ValueError 子类）
        with self.assertRaises(ValueError):
            from api_workbench.runner import load_case

            load_case(STATIC_DEEP_ARRAY_CASE)


class RunEntryDeepNestingTests(unittest.TestCase):
    """公开入口 run：退出码 2、stdout 空、诊断稳定、请求数为零。"""

    def setUp(self) -> None:
        self.server = _HealthServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _run_subprocess(self, path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _assert_rejected_without_request(
        self, completed: subprocess.CompletedProcess, path: str
    ) -> None:
        self.assertEqual(
            completed.returncode, 2, f"应拒绝加载，stderr={completed.stderr!r}"
        )
        self.assertEqual(completed.stdout, b"", "失败时不得输出任何执行报告")
        stderr_text = completed.stderr.decode("utf-8")
        self.assertTrue(
            stderr_text.startswith(DIAGNOSTIC_PREFIX),
            f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr_text!r}",
        )
        self.assertIn(path, stderr_text)
        self.assertIn("JSON 嵌套过深", stderr_text)
        self.assertIn("无法完成解析", stderr_text)
        self.assertNotIn("Traceback", stderr_text)
        self.assertNotIn("request_failed", stderr_text)

    def _write_case_pointing_at_server(self, extra_json: str) -> str:
        case = {
            "name": "health",
            "url": f"http://127.0.0.1:{self.server.port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        # extra 以原始 JSON 文本拼接，避免 json.dumps 自身受递归限制影响
        text = (
            "{"
            + ",".join(
                f"{json.dumps(key)}: {json.dumps(value)}"
                for key, value in case.items()
            )
            + f', "extra": {extra_json}'
            + "}"
        )
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _run_in_process(self, path: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = main(["run", path])
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_static_deep_array_file_rejected_by_run_entry(self) -> None:
        completed = self._run_subprocess(STATIC_DEEP_ARRAY_CASE)
        self._assert_rejected_without_request(completed, STATIC_DEEP_ARRAY_CASE)

    def test_static_deep_object_file_rejected_by_run_entry(self) -> None:
        completed = self._run_subprocess(STATIC_DEEP_OBJECT_CASE)
        self._assert_rejected_without_request(completed, STATIC_DEEP_OBJECT_CASE)

    def test_deep_extra_sends_no_request_even_with_valid_fields(self) -> None:
        # name、url 与断言字段都已合法：深层 extra 仍在解析阶段拒绝，
        # 不建立连接、不发送请求（服务端收到的 GET 数为 0）
        for label, extra_json in (
            ("array", _deep_array()),
            ("object", _deep_object()),
        ):
            with self.subTest(label=label):
                path = self._write_case_pointing_at_server(extra_json)
                self.assertEqual(self.server.get_count, 0)
                exit_code, stdout_text, stderr_text = self._run_in_process(path)
                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout_text, "")
                self.assertTrue(stderr_text.startswith(DIAGNOSTIC_PREFIX))
                self.assertIn(path, stderr_text)
                self.assertIn("JSON 嵌套过深", stderr_text)
                self.assertNotIn("Traceback", stderr_text)
                self.assertEqual(
                    self.server.get_count, 0, "解析失败时不得发送任何请求"
                )


class ShallowExtraCompatibilityTests(unittest.TestCase):
    """兼容对照：浅层嵌套 extra 被忽略，用例照常成功执行。"""

    def setUp(self) -> None:
        self.server = _HealthServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_shallow_extra_is_ignored_and_case_passes(self) -> None:
        # 以 health 用例全部必需字段为基础追加浅层（两层）嵌套 extra：
        # 它不是用例字段，必须被忽略，断言与执行路径不受影响
        case = {
            "name": "health",
            "url": f"http://127.0.0.1:{self.server.port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
            "extra": [[0]],
        }
        path = os.path.join(self.tmpdir.name, "health_shallow_extra.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        self.assertEqual(
            completed.returncode, 0, f"应成功执行，stderr={completed.stderr!r}"
        )
        self.assertEqual(
            completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}"
        )
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        expected = {
            "name": "health",
            "passed": True,
            "error": None,
            "status_check": {"expected": 200, "actual": 200, "passed": True},
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": "ok",
                "passed": True,
                "present": True,
            },
        }
        self.assertEqual(report, expected)
        self.assertTrue(report["passed"])


class ExpectedValueShallowNestingStillRejectedTests(unittest.TestCase):
    """解析改动不放宽字段校验：对象/浅层嵌套数组仍不能成为 expected_value。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_case(self, expected_value_json: str) -> str:
        text = (
            "{\n"
            '  "name": "health",\n'
            '  "url": "http://127.0.0.1:8765/health",\n'
            '  "expected_status": 200,\n'
            '  "field": "status",\n'
            f'  "expected_value": {expected_value_json}\n'
            "}\n"
        )
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_object_and_nested_array_expected_value_rejected(self) -> None:
        for label, value_json in (
            ("object", '{"a": 1}'),
            ("nested-array", "[[0]]"),
        ):
            with self.subTest(label=label):
                path = self._write_case(value_json)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, b"")
                stderr_text = completed.stderr.decode("utf-8")
                self.assertTrue(stderr_text.startswith(DIAGNOSTIC_PREFIX))
                self.assertIn("expected_value", stderr_text)
                self.assertNotIn("Traceback", stderr_text)


if __name__ == "__main__":
    unittest.main()
