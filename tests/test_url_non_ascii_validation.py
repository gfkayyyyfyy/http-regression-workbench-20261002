"""URL 路径/查询非 ASCII 字符的回归测试：未转义字符须在连接前按用例错误拒绝，
已百分号编码的地址保持原有文本发送，片段（# 之后）不参与校验。"""

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

# 路径或查询中含未转义非 ASCII 字符的地址：中文、带重音字母、表情
NON_ASCII_URLS = [
    "http://127.0.0.1:8765/health?tag=回归",
    "http://127.0.0.1:8765/中文",
    "http://127.0.0.1:8765/café",
    "http://127.0.0.1:8765/health?mood=😀",
]

# 片段不发送给服务端：仅片段含中文时不因本次校验被拒绝
FRAGMENT_ONLY_URL = "http://127.0.0.1:8765/health#备注"

# 已百分号编码的查询 + 仅含中文的片段：应按原有文本发送，不解码、不重复编码
ENCODED_QUERY = "tag=%E5%9B%9E%E5%BD%92"
ENCODED_URL = f"http://127.0.0.1:8765/health?{ENCODED_QUERY}#备注"


def _write_case(url: str, ensure_ascii: bool = False) -> str:
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
        json.dump(case, handle, ensure_ascii=ensure_ascii)
    return path


class _RecordingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数与原始请求路径的受控健康检查服务。"""

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


class NonAsciiUrlRejectionTests(unittest.TestCase):
    def test_load_case_raises_case_error(self) -> None:
        for url in NON_ASCII_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("未转义", message)
                self.assertIn(url, message, "诊断必须包含原始地址")

    def test_load_case_raises_case_error_for_unicode_escaped_json(self) -> None:
        # 用例 JSON 经 \uXXXX 转义还原后的同类字符同样必须拒绝
        for url in NON_ASCII_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url, ensure_ascii=True)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("未转义", message)
                self.assertIn(url, message)

    def test_run_case_rejects_before_connection(self) -> None:
        for url in NON_ASCII_URLS:
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
                self.assertIn("未转义", message)
                self.assertIn(url, message, "诊断必须包含原始地址")
                self.assertNotIn("Traceback", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"含未转义字符时不得建立连接: {url}",
                )

    def test_fragment_only_non_ascii_passes_validation(self) -> None:
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["_parsed"].fragment, "备注")

    def test_rejection_happens_with_server_running_and_no_request_sent(self) -> None:
        # 服务在线时同样拒绝，且服务端收不到任何请求
        with LocalServer() as server:
            for url in NON_ASCII_URLS:
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
                    self.assertIn("未转义", stderr)
                    self.assertIn(url, stderr)
                    self.assertNotIn("Traceback", stderr)
            self.assertEqual(server.get_count, 0, "被拒绝的用例不得发送任何请求")


class PercentEncodedCompatibilityTests(unittest.TestCase):
    def test_encoded_url_passes_validation(self) -> None:
        for url in [ENCODED_URL, FRAGMENT_ONLY_URL]:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertEqual(case["url"], url)

    def test_encoded_url_sent_verbatim_with_single_get(self) -> None:
        with LocalServer() as server:
            url = ENCODED_URL.replace("8765", str(server.port))
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
            self.assertEqual(report["status_check"]["actual"], 200)
            self.assertEqual(report["field_check"]["actual"], "ok")
            self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
            # 已编码文本原样发送：不解码、不重复编码，片段不发送
            self.assertEqual(server.paths, [f"/health?{ENCODED_QUERY}"])

    def test_fragment_not_sent_to_server(self) -> None:
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
            report = json.loads(completed.stdout.decode("utf-8"))
            self.assertIsNone(report["error"])
            self.assertTrue(report["passed"])
            self.assertEqual(server.paths, ["/health"])


if __name__ == "__main__":
    unittest.main()
