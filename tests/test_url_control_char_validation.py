"""URL 中制表符/换行符/回车符（U+0009/U+000A/U+000D）的回归测试：

这三种控制字符在地址解析时会被底层解析器静默消去，导致实际请求地址偏离
用例原文，因此第一个 # 之前（协议、主机、端口、路径、查询）出现时必须
在连接前按用例错误拒绝；仅出现在片段时允许（片段不发送）；已百分号编码
的 %09/%0A/%0D（含小写）按原有文本发送，不解码、不重复编码。"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from api_workbench.runner import CaseError, load_case, run_case

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ESCAPES = {"\t": "\\t", "\n": "\\n", "\r": "\\r"}


def _escape_url(url: str) -> str:
    """诊断中地址的期望形态：三种控制字符以 \\t、\\n、\\r 字面形式表示。"""
    return "".join(ESCAPES.get(char, char) for char in url)


# 第一个 # 之前含控制字符的地址：路径、查询、协议、主机、端口各位置
CONTROL_URLS = [
    "http://127.0.0.1:8765/he\talth",
    "http://127.0.0.1:8765/health?q=o\nk",
    "http://127.0.0.1:8765/health\r",
    "ht\ttp://127.0.0.1:8765/health",
    "http://127.0.0.\t1:8765/health",
    "http://127.0.0.1:8\r765/health",
    "http://127.0.0.1:8765/health?q=a\tb\nc\rd",
]

# 片段不发送给服务端：控制字符仅出现在第一个 # 之后时不因本次校验被拒绝
FRAGMENT_ONLY_URL = "http://127.0.0.1:8765/health#备注\r\n"

# 已百分号编码的查询（含小写形式）：按原有文本发送，不解码、不重复编码
ENCODED_URLS = [
    ("http://127.0.0.1:8765/health?q=%09%0A%0D", "/health?q=%09%0A%0D"),
    ("http://127.0.0.1:8765/health?q=%09%0a%0d", "/health?q=%09%0a%0d"),
]


def _write_case(url: str, unicode_escape: bool = False) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。

    json.dumps 会把控制字符写成 \\t、\\n、\\r 短转义；unicode_escape 为
    True 时改写成 \\u0009、\\u000a、\\u000d 形式，两种输入还原后相同。
    """
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    text = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
    if unicode_escape:
        text = (
            text.replace("\\t", "\\u0009")
            .replace("\\n", "\\u000a")
            .replace("\\r", "\\u000d")
        )
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class _RecordingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数与原始请求目标的受控健康检查服务。"""

    get_count = 0
    paths: list[str] = []
    _lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
            self.__class__.paths.append(self.path)
        body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class LocalServer:
    """在 127.0.0.1 临时端口上运行受控服务的上下文管理。"""

    def __init__(self) -> None:
        _RecordingHandler.get_count = 0
        _RecordingHandler.paths = []
        self.httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "LocalServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def get_count(self) -> int:
        return _RecordingHandler.get_count

    @property
    def paths(self) -> list[str]:
        return list(_RecordingHandler.paths)


class ControlCharUrlRejectionTests(unittest.TestCase):
    def _assert_diagnostic(self, message: str, url: str) -> None:
        self.assertIn("url", message)
        self.assertIn("控制字符", message)
        # 出现的控制字符以 \t、\n、\r 字面形式点名
        head = url.split("#", 1)[0]
        for char, escaped in ESCAPES.items():
            if char in head:
                self.assertIn(escaped, message)
        # 完整原始地址同样以转义形式给出
        self.assertIn(_escape_url(url), message, "诊断必须包含转义后的完整原始地址")
        # 诊断本身不得包含实际的控制字符（stderr 末尾仅有 print 的一个换行）
        body = message[:-1] if message.endswith("\n") else message
        for char in ESCAPES:
            self.assertNotIn(char, body)

    def test_load_case_raises_case_error(self) -> None:
        for url in CONTROL_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                self._assert_diagnostic(str(context.exception), url)

    def test_load_case_raises_case_error_for_unicode_escaped_json(self) -> None:
        # 用例 JSON 中把控制字符写成 \t、\n、\r 短转义或
        # \u0009、\u000a、\u000d 转义序列，还原后同样必须拒绝
         # 转义序列，还原后同样必须拒绝
        for url in CONTROL_URLS:
            for unicode_escape in (False, True):
                with self.subTest(url=url, unicode_escape=unicode_escape):
                    case_path = _write_case(url, unicode_escape=unicode_escape)
                    self.addCleanup(os.unlink, case_path)
                    with self.assertRaises(CaseError) as context:
                        load_case(case_path)
                    self.assertIs(type(context.exception), CaseError)
                    self._assert_diagnostic(str(context.exception), url)

    def test_run_case_rejects_before_connection(self) -> None:
        for url in CONTROL_URLS:
            for unicode_escape in (False, True):
                with self.subTest(url=url, unicode_escape=unicode_escape):
                    case_path = _write_case(url, unicode_escape=unicode_escape)
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
                    self._assert_diagnostic(message, url)
                    self.assertNotIn("Traceback", message)
                    # 不得归为 request_failed，也不得删除字符或自动编码后继续请求
                    self.assertNotIn("request_failed", message)
                    self.assertEqual(
                        connection_cls.call_count,
                        0,
                        f"含控制字符时不得建立连接: {_escape_url(url)}",
                    )

    def test_fragment_only_control_chars_pass_validation(self) -> None:
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["url"], FRAGMENT_ONLY_URL)

    def test_rejection_happens_with_server_running_and_no_request_sent(self) -> None:
        # 服务在线时同样拒绝，且服务端收不到任何请求
        with LocalServer() as server:
            for url in CONTROL_URLS:
                url = url.replace("8765", str(server.port))
                for unicode_escape in (False, True):
                    with self.subTest(url=url, unicode_escape=unicode_escape):
                        case_path = _write_case(url, unicode_escape=unicode_escape)
                        self.addCleanup(os.unlink, case_path)
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
                        self._assert_diagnostic(stderr, url)
                        self.assertNotIn("Traceback", stderr)
                        self.assertNotIn("request_failed", stderr)
            self.assertEqual(server.get_count, 0, "被拒绝的用例不得发送任何请求")


class PercentEncodedCompatibilityTests(unittest.TestCase):
    def test_encoded_and_fragment_urls_pass_validation(self) -> None:
        for url, _ in ENCODED_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertEqual(case["url"], url)

    def test_encoded_urls_sent_verbatim_with_single_get(self) -> None:
        with LocalServer() as server:
            expected_targets = []
            for url, expected_target in ENCODED_URLS:
                url = url.replace("8765", str(server.port))
                expected_targets.append(expected_target)
                with self.subTest(url=url):
                    case_path = _write_case(url)
                    self.addCleanup(os.unlink, case_path)
                    completed = subprocess.run(
                        [sys.executable, "-m", "api_workbench", "run", case_path],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                    self.assertEqual(completed.stderr, b"")
                    # stdout 只有一个可解析的 JSON 报告
                    report = json.loads(completed.stdout.decode("utf-8"))
                    self.assertIsNone(report["error"])
                    self.assertTrue(report["passed"])
                    self.assertTrue(report["status_check"]["passed"])
                    self.assertEqual(report["status_check"]["actual"], 200)
                    self.assertTrue(report["field_check"]["passed"])
                    self.assertEqual(report["field_check"]["actual"], "ok")
                    self.assertTrue(report["field_check"]["present"])
                    # 每个用例只收到一次 GET，请求目标与输入逐字符一致：
                    # 不解码、不重复编码
                    self.assertEqual(server.paths, expected_targets)
            self.assertEqual(server.get_count, len(ENCODED_URLS))

    def test_fragment_with_control_chars_not_sent_to_server(self) -> None:
        with LocalServer() as server:
            url = FRAGMENT_ONLY_URL.replace("8765", str(server.port))
            case_path = _write_case(url)
            self.addCleanup(os.unlink, case_path)
            completed = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stderr, b"")
            report = json.loads(completed.stdout.decode("utf-8"))
            self.assertIsNone(report["error"])
            self.assertTrue(report["passed"])
            self.assertEqual(report["field_check"]["actual"], "ok")
            self.assertTrue(report["field_check"]["present"])
            # 片段不发送：服务端只收到一次 GET /health
            self.assertEqual(server.paths, ["/health"])
            self.assertEqual(server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
