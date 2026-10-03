"""用例文件加载阶段的回归测试。

固定 ``python -m api_workbench run case.json`` 在加载阶段失败时的公开结果：

1. 文件不存在或路径是目录：退出码 2，stdout 为空，stderr 以
   ``api_workbench:`` 为前缀，包含“无法读取用例文件”与传入路径，无 Traceback；
2. 文件不是合法 UTF-8（仅含字节 0xFF）：退出码 2，诊断包含 UTF-8 与路径；
3. 空文件、``{"name":}``、``{}{}`` 等非法 JSON：退出码 2，诊断包含 JSON 与路径；
4. 顶层为 []、null、"text"、200、true 等非对象：诊断说明“用例必须是一个 JSON 对象”；
5. 以上失败均不生成请求失败报告，也不会发起任何 HTTP 请求；
6. 合法对照用例（中文名称、受控 127.0.0.1 临时端口、文件首尾带空白）：
   恰好一次 GET，退出码 0，stderr 为空，stdout 只含一个可解析的 JSON 报告。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，不占用固定
8765、不访问公网、不依赖第三方包。
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
from unittest import mock

import api_workbench.cli as cli
from api_workbench import runner

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIAGNOSTIC_PREFIX = "api_workbench:"


class _HealthServer(ThreadingHTTPServer):
    """仅在 GET /health 上返回 200 与 {"status":"ok"} 的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server_address[1]
        self.get_count = 0
        self.requested_paths: list[str] = []
        self._lock = threading.Lock()

    def record(self, path: str) -> None:
        with self._lock:
            self.get_count += 1
            self.requested_paths.append(path)


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _HealthServer = self.server
        server.record(self.path)
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class _LoadingFailureTestCase(unittest.TestCase):
    """公共设施：临时目录写文件、经公开入口以子进程执行。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_case(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _run(self, path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _assert_rejected(
        self,
        completed: subprocess.CompletedProcess,
        *,
        path: str,
        fragments: list[str],
    ) -> None:
        """加载失败的统一公开结果：退出码 2、stdout 空、诊断稳定且无 Traceback。"""
        self.assertEqual(
            completed.returncode, 2, f"应拒绝加载，stderr={completed.stderr!r}"
        )
        self.assertEqual(completed.stdout, b"", "失败时不得输出任何报告")
        stderr_text = completed.stderr.decode("utf-8")
        self.assertTrue(
            stderr_text.startswith(DIAGNOSTIC_PREFIX),
            f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr_text!r}",
        )
        for fragment in fragments:
            self.assertIn(fragment, stderr_text)
        self.assertNotIn("Traceback", stderr_text)
        # 不得生成请求失败报告（stdout 已为空，stderr 中同样不应出现该类别）
        self.assertNotIn("request_failed", stderr_text)


class UnreadableCaseFileTests(_LoadingFailureTestCase):
    """文件无法读取（不存在、路径为目录）。"""

    def test_missing_file_is_rejected_with_path_in_diagnostic(self) -> None:
        path = os.path.join(self.tmpdir.name, "missing.json")
        self.assertFalse(os.path.exists(path))
        completed = self._run(path)
        self._assert_rejected(
            completed, path=path, fragments=["无法读取用例文件", path]
        )

    def test_directory_path_is_rejected_with_path_in_diagnostic(self) -> None:
        path = self.tmpdir.name
        self.assertTrue(os.path.isdir(path))
        completed = self._run(path)
        self._assert_rejected(
            completed, path=path, fragments=["无法读取用例文件", path]
        )


class UndecodableCaseFileTests(_LoadingFailureTestCase):
    """文件内容无法按 UTF-8 解码。"""

    def test_file_with_invalid_utf8_byte_is_rejected(self) -> None:
        path = self._write_case("bad_utf8.json", b"\xff")
        completed = self._run(path)
        self._assert_rejected(completed, path=path, fragments=["UTF-8", path])


class UnparseableJsonCaseFileTests(_LoadingFailureTestCase):
    """字节可解码但不是合法 JSON。"""

    def test_empty_file_is_rejected_as_json_error(self) -> None:
        path = self._write_case("empty.json", b"")
        completed = self._run(path)
        self._assert_rejected(completed, path=path, fragments=["JSON", path])

    def test_syntax_error_json_is_rejected_as_json_error(self) -> None:
        path = self._write_case("syntax_error.json", '{"name":}'.encode("utf-8"))
        completed = self._run(path)
        self._assert_rejected(completed, path=path, fragments=["JSON", path])

    def test_two_consecutive_json_objects_is_rejected_as_json_error(self) -> None:
        path = self._write_case("two_objects.json", b"{}{}")
        completed = self._run(path)
        self._assert_rejected(completed, path=path, fragments=["JSON", path])


class NonObjectTopLevelTests(_LoadingFailureTestCase):
    """合法 JSON 但顶层不是对象：诊断要求必须是 JSON 对象。"""

    def test_non_object_top_level_values_are_rejected(self) -> None:
        cases = {
            "array.json": b"[]",
            "null.json": b"null",
            "string.json": b'"text"',
            "number.json": b"200",
            "bool.json": b"true",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                path = self._write_case(name, data)
                completed = self._run(path)
                self._assert_rejected(
                    completed,
                    path=path,
                    fragments=["用例必须是一个 JSON 对象"],
                )


class NoHttpRequestOnLoadingFailureTests(unittest.TestCase):
    """加载阶段失败时绝不能进入请求执行路径。

    通过进程内调用公开入口 ``cli.main``，把 ``runner.execute`` 替换为
    一旦被调用即令测试失败的哨兵，覆盖全部加载失败输入。
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_case(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp(dir=self.tmpdir.name, suffix=".json")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return path

    def test_no_execute_and_exit_code_2_for_every_failure(self) -> None:
        missing_path = os.path.join(self.tmpdir.name, "missing.json")
        bad_utf8_path = self._write_case(b"\xff")
        invalid_paths = [
            self._write_case(b""),
            self._write_case(b'{"name":}'),
            self._write_case(b"{}{}"),
        ]
        non_object_paths = [
            self._write_case(b"[]"),
            self._write_case(b"null"),
            self._write_case(b'"text"'),
            self._write_case(b"200"),
            self._write_case(b"true"),
        ]

        scenarios = [(missing_path, "missing-file"), (bad_utf8_path, "bad-utf8")]
        scenarios += [(p, "invalid-json") for p in invalid_paths]
        scenarios += [(p, "non-object") for p in non_object_paths]
        scenarios.append((self.tmpdir.name, "directory"))

        for path, label in scenarios:
            with self.subTest(label=label):
                def _forbidden_execute(*args, **kwargs):
                    raise AssertionError("加载失败时不得执行 HTTP 请求")

                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ), mock.patch.object(
                    runner, "execute", _forbidden_execute
                ):
                    exit_code = cli.main(["run", path])

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertTrue(stderr.getvalue().startswith(DIAGNOSTIC_PREFIX))
                self.assertNotIn("Traceback", stderr.getvalue())


class ValidCaseLoadingControlTests(unittest.TestCase):
    """合法对照：以 cases/health.json 为基础，改临时端口与中文名称。"""

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

    def test_valid_utf8_case_with_chinese_name_runs_single_successful_get(self) -> None:
        case = {
            "name": "健康检查",
            "url": f"http://127.0.0.1:{self.server.port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        # 以 UTF-8 保存，文件首尾带合法空白（空格、制表符、CR、LF）
        text = json.dumps(case, ensure_ascii=False, indent=2)
        data = b"\n\t " + text.encode("utf-8") + b" \r\n"
        case_path = os.path.join(self.tmpdir.name, "health.json")
        with open(case_path, "wb") as handle:
            handle.write(data)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}"
        )

        # 恰好一次 GET，且请求目标为 /health
        self.assertEqual(self.server.get_count, 1, "整个运行只应收到一次 GET")
        self.assertEqual(self.server.requested_paths, ["/health"])

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )

        expected = {
            "name": "健康检查",
            "passed": True,
            "error": None,
            "status_check": {
                "expected": 200,
                "actual": 200,
                "passed": True,
            },
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": "ok",
                "passed": True,
            },
        }
        self.assertEqual(report, expected)
        # 名称中的中文必须原样保留
        self.assertEqual(report["name"], "健康检查")
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])


if __name__ == "__main__":
    unittest.main()
