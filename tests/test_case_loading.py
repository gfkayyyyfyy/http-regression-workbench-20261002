"""用例文件加载阶段的独立回归测试。

通过公开入口 ``api_workbench.cli.main(["run", ...])``（即
``python -m api_workbench run case.json`` 的同一入口）固定加载失败时的
公开结果：

1. 文件无法读取（路径不存在、路径是目录）：退出码 2，stdout 为空，
   stderr 以 ``api_workbench:`` 前缀给出“无法读取用例文件”并包含传入路径；
2. 字节无法按 UTF-8 解码：退出码 2，诊断包含 UTF-8 与文件路径；
3. JSON 无法解析（空文件、``{"name":}``、连续两个对象 ``{}{}``）：
   退出码 2，诊断包含 JSON 与文件路径；
4. 顶层不是 JSON 对象（[]、null、"text"、200、true）：
   诊断说明“用例必须是一个 JSON 对象”。

所有失败都不得输出 Traceback、不得生成请求失败报告，并且不得发起任何
HTTP 请求（用桩替换 HTTPConnection 验证）。

另含一个合法用例对照：以 cases/health.json 为基础，地址指向受控
127.0.0.1 临时端口上的 /health，名称改为“健康检查”；服务返回
200 与 {"status":"ok"} 后应恰好收到一次 GET，退出码 0，stderr 为空，
stdout 只包含一个可解析的 JSON 报告，两项检查通过、error 为 null。

仅使用 Python 标准库 unittest；临时文件与本地服务自行释放，
不占用固定 8765 端口，不依赖公网或第三方包。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from api_workbench import cli

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _CaseLoadingTestCase(unittest.TestCase):
    """公共设施：临时目录、写入原始字节用例、经公开入口执行并捕获输出。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _case_path(self, filename: str) -> str:
        return os.path.join(self.tmpdir.name, filename)

    def _write_raw(self, filename: str, raw: bytes) -> str:
        path = self._case_path(filename)
        with open(path, "wb") as handle:
            handle.write(raw)
        return path

    def _run(self, case_path: str) -> tuple[int, bytes, str]:
        """经 cli.main 执行，返回 (退出码, stdout 字节, stderr 文本)。

        run_case 成功时直接写 sys.stdout.buffer，因此用 TextIOWrapper
        包裹 BytesIO 作为 stdout；stderr 用 StringIO 捕获。
        """
        stdout_buffer = io.BytesIO()
        stdout_wrapper = io.TextIOWrapper(stdout_buffer, encoding="utf-8")
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_wrapper), contextlib.redirect_stderr(
            stderr_buffer
        ):
            returncode = cli.main(["run", case_path])
        stdout_wrapper.flush()
        return returncode, stdout_buffer.getvalue(), stderr_buffer.getvalue()

    def _assert_rejected(
        self, case_path: str, *diagnostic_fragments: str
    ) -> str:
        """断言加载失败的稳定公开结果，并确认全程未发起 HTTP 请求。"""
        with mock.patch("api_workbench.runner.HTTPConnection") as connection_cls:
            returncode, stdout_bytes, stderr_text = self._run(case_path)

        self.assertEqual(returncode, 2, f"加载失败必须以退出码 2 拒绝: {stderr_text!r}")
        self.assertEqual(stdout_bytes, b"", "失败时 stdout 必须为空，不得输出报告")
        self.assertNotIn(b"request_failed", stdout_bytes, "不得生成请求失败报告")
        self.assertTrue(
            stderr_text.startswith("api_workbench: "),
            f"诊断必须使用 api_workbench: 前缀: {stderr_text!r}",
        )
        self.assertNotIn("Traceback", stderr_text, "不得输出 Traceback")
        for fragment in diagnostic_fragments:
            self.assertIn(fragment, stderr_text)
        # 加载阶段失败时连接类根本不应被实例化，即没有发起任何 HTTP 请求
        connection_cls.assert_not_called()
        return stderr_text


class UnreadableCaseFileTests(_CaseLoadingTestCase):
    """文件无法读取：退出码 2，诊断说明无法读取并回显传入路径。"""

    def test_nonexistent_path_is_rejected(self) -> None:
        missing_path = self._case_path("does-not-exist.json")
        self.assertFalse(os.path.exists(missing_path))
        self._assert_rejected(missing_path, "无法读取用例文件", missing_path)

    def test_directory_path_is_rejected(self) -> None:
        # 目录本身存在，但 open() 读取会失败：与不存在的路径同样拒绝
        self.assertTrue(os.path.isdir(self.tmpdir.name))
        self._assert_rejected(self.tmpdir.name, "无法读取用例文件", self.tmpdir.name)


class UndecodableCaseFileTests(_CaseLoadingTestCase):
    """文件字节无法按 UTF-8 解码：退出码 2，诊断包含 UTF-8 与路径。"""

    def test_file_with_single_0xff_byte_is_rejected(self) -> None:
        path = self._write_raw("not-utf8.json", b"\xff")
        self._assert_rejected(path, "UTF-8", path)


class InvalidJsonCaseFileTests(_CaseLoadingTestCase):
    """文本可解码但不是合法 JSON：退出码 2，诊断包含 JSON 与路径。

    只断言稳定信息（前缀、JSON 字样、传入路径），不依赖具体的
    解析器报错全文；操作系统或解析器版本差异不得影响验收。
    """

    def test_empty_file_is_rejected(self) -> None:
        path = self._write_raw("empty.json", b"")
        self._assert_rejected(path, "JSON", path)

    def test_syntax_error_json_is_rejected(self) -> None:
        path = self._write_raw("syntax-error.json", '{"name":}'.encode("utf-8"))
        self._assert_rejected(path, "JSON", path)

    def test_two_consecutive_json_objects_are_rejected(self) -> None:
        path = self._write_raw("two-objects.json", b"{}{}")
        self._assert_rejected(path, "JSON", path)


class NonObjectTopLevelTests(_CaseLoadingTestCase):
    """顶层是合法 JSON 但不是对象：诊断必须说明用例必须是 JSON 对象。"""

    def test_each_non_object_top_level_value_is_rejected(self) -> None:
        cases = {
            "array.json": b"[]",
            "null.json": b"null",
            "string.json": b'"text"',
            "number.json": b"200",
            "boolean.json": b"true",
        }
        for filename, raw in cases.items():
            with self.subTest(filename=filename):
                path = self._write_raw(filename, raw)
                self._assert_rejected(path, "用例必须是一个 JSON 对象")


class _HealthHandler(BaseHTTPRequestHandler):
    """受控 /health 服务：固定返回 200 与 {"status":"ok"}，记录 GET 次数。"""

    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _HealthServer = self.server
        with server._lock:
            server.get_count += 1
            server.requested_paths.append(self.path)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class _HealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server_address[1]
        self.get_count = 0
        self.requested_paths: list[str] = []
        self._lock = threading.Lock()


class ValidCaseEndToEndTests(unittest.TestCase):
    """合法用例对照：基于 cases/health.json 跑通受控临时端口服务。"""

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

    def test_valid_health_case_passes_with_single_get(self) -> None:
        # 以 cases/health.json 为基础，仅改地址（临时端口 /health）与名称，
        # 其余字段（expected_status/field/expected_value）保持一致。
        with open(os.path.join(PROJECT_ROOT, "cases", "health.json"), "rb") as handle:
            baseline = json.loads(handle.read().decode("utf-8"))
        case = dict(baseline)
        case["name"] = "健康检查"
        case["url"] = f"http://127.0.0.1:{self.server.port}/health"

        # UTF-8 保存，文件首尾故意带合法空白，加载时必须被容忍
        encoded = json.dumps(case, ensure_ascii=False, indent=2).encode("utf-8")
        case_path = os.path.join(self.tmpdir.name, "health.json")
        with open(case_path, "wb") as handle:
            handle.write(b"\n  " + encoded + b"\t\n")

        stdout_buffer = io.BytesIO()
        stdout_wrapper = io.TextIOWrapper(stdout_buffer, encoding="utf-8")
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_wrapper), contextlib.redirect_stderr(
            stderr_buffer
        ):
            returncode = cli.main(["run", case_path])
        stdout_wrapper.flush()
        stdout_text = stdout_buffer.getvalue().decode("utf-8")
        stderr_text = stderr_buffer.getvalue()

        self.assertEqual(returncode, 0)
        self.assertEqual(stderr_text, "", f"成功路径 stderr 必须为空: {stderr_text!r}")

        # stdout 有且仅有一个可解析的 JSON 报告（前后只允许空白）
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)

        self.assertEqual(report["name"], "健康检查", "报告中的中文名称必须保留")
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(
            report["status_check"],
            {"expected": 200, "actual": 200, "passed": True},
        )
        self.assertEqual(
            report["field_check"],
            {"field": "status", "expected": "ok", "actual": "ok", "passed": True},
        )

        # 整个运行恰好一次 GET，且请求目标为 /health
        self.assertEqual(self.server.get_count, 1, "应恰好收到一次 GET")
        self.assertEqual(self.server.requested_paths, ["/health"])


if __name__ == "__main__":
    unittest.main()
