"""URL 路径/查询原始空格（U+0020）的回归测试：未编码空格须在连接前按用例错误
拒绝，已百分号编码的 %20 与查询加号保持原有文本发送，片段（# 之后）中的空格
不参与校验、不发送给服务端。本模块只覆盖 U+0020，不扩大到其他空白字符。"""

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

# 路径或查询中含未编码空格的地址：路径内空格、查询值内空格
SPACE_URLS = [
    "http://127.0.0.1:8765/health check",
    "http://127.0.0.1:8765/health?tag=a b",
]

# 已编码空格与查询加号：应按原有文本发送，不解码、不重复编码
ENCODED_CASES = [
    ("http://127.0.0.1:8765/health%20check", "/health%20check"),
    ("http://127.0.0.1:8765/health?tag=a%20b", "/health?tag=a%20b"),
    ("http://127.0.0.1:8765/health?tag=a+b", "/health?tag=a+b"),
]

# 片段不发送给服务端：仅片段含空格时不因本次校验被拒绝
FRAGMENT_ONLY_URL = "http://127.0.0.1:8765/health#note text"


def _write_case(url: str, escape_space: bool = False) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。

    escape_space 为 True 时把 url 中的空格写成 \\u0020 转义，
    json.loads 还原后仍是 U+0020 空格，用于验证两种输入得到相同结果。
    """
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    # 紧凑分隔符保证 JSON 结构本身不含空格，唯一的空格在 url 字符串值内
    text = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
    if escape_space:
        text = text.replace(url, url.replace(" ", "\\u0020"))
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


class SpaceUrlRejectionTests(unittest.TestCase):
    def test_load_case_raises_case_error(self) -> None:
        for url in SPACE_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("url", message)
                self.assertIn("未编码的空格", message)
                self.assertIn(url, message, "诊断必须包含还原后的完整地址")

    def test_load_case_raises_case_error_for_unicode_escaped_json(self) -> None:
        # 用例 JSON 中把空格写成 \\u0020 转义，还原后仍是 U+0020，同样必须拒绝
        for url in SPACE_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url, escape_space=True)
                self.addCleanup(os.unlink, case_path)
                # 确认文件中的转义确实还原为空格，两种输入等价
                with open(case_path, encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle)["url"], url)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("url", message)
                self.assertIn("未编码的空格", message)
                self.assertIn(url, message, "诊断必须包含还原后的完整地址")

    def test_run_case_rejects_before_connection(self) -> None:
        for url in SPACE_URLS:
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
                self.assertIn("url", message)
                self.assertIn("未编码的空格", message)
                self.assertIn(url, message, "诊断必须包含还原后的完整地址")
                self.assertNotIn("Traceback", message)
                # 用例错误不得归为 request_failed，也不得自动编码后继续请求
                self.assertNotIn("request_failed", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"含未编码空格时不得建立连接: {url}",
                )

    def test_rejection_happens_with_server_running_and_no_request_sent(self) -> None:
        # 服务在线时同样拒绝，且服务端收不到任何请求
        with LocalServer() as server:
            for url in SPACE_URLS:
                url = url.replace("8765", str(server.port))
                with self.subTest(url=url):
                    case_path = _write_case(url)
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
                    self.assertIn("url", stderr)
                    self.assertIn("未编码的空格", stderr)
                    self.assertIn(url, stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertNotIn("request_failed", stderr)
            self.assertEqual(server.get_count, 0, "被拒绝的用例不得发送任何请求")


class EncodedSpaceCompatibilityTests(unittest.TestCase):
    def test_encoded_and_fragment_urls_pass_validation(self) -> None:
        urls = [url for url, _ in ENCODED_CASES] + [FRAGMENT_ONLY_URL]
        for url in urls:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertEqual(case["url"], url)

    def test_fragment_only_space_passes_validation(self) -> None:
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["_parsed"].fragment, "note text")

    def test_encoded_urls_sent_verbatim_with_single_get(self) -> None:
        with LocalServer() as server:
            for url_template, expected_target in ENCODED_CASES:
                url = url_template.replace("8765", str(server.port))
                with self.subTest(url=url):
                    case_path = _write_case(url)
                    self.addCleanup(os.unlink, case_path)
                    completed = subprocess.run(
                        [sys.executable, "-m", "api_workbench", "run", case_path],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(
                        completed.returncode, 0, completed.stderr.decode()
                    )
                    self.assertEqual(completed.stderr, b"")
                    report = json.loads(completed.stdout.decode("utf-8"))
                    self.assertIsNone(report["error"])
                    self.assertTrue(report["passed"])
                    self.assertTrue(report["status_check"]["passed"])
                    self.assertEqual(report["status_check"]["actual"], 200)
                    self.assertTrue(report["field_check"]["passed"])
                    self.assertEqual(report["field_check"]["actual"], "ok")
                    self.assertIs(report["field_check"]["present"], True)
                    # 已编码文本原样发送：不解码、不重复编码
                    self.assertEqual(server.paths[-1], expected_target)
            self.assertEqual(
                server.get_count, len(ENCODED_CASES), "每个用例只应发送一次 GET"
            )

    def test_fragment_with_space_not_sent_to_server(self) -> None:
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
            self.assertTrue(report["status_check"]["passed"])
            self.assertTrue(report["field_check"]["passed"])
            self.assertEqual(report["field_check"]["actual"], "ok")
            self.assertIs(report["field_check"]["present"], True)
            self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
            # 片段（含空格）不发送，服务端只收到 /health
            self.assertEqual(server.paths, ["/health"])


if __name__ == "__main__":
    unittest.main()
