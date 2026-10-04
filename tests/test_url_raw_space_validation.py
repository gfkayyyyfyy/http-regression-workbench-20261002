"""URL 路径/查询原始空格（U+0020）的回归测试：未编码空格须在连接前
按用例错误拒绝；%20 与查询中的加号保持原文本发送，片段不参与校验。"""

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

# 路径、查询参数值或参数名中含原始空格的地址
RAW_SPACE_URLS = [
    "http://127.0.0.1:8765/health?tag=hello world",
    "http://127.0.0.1:8765/heal th",
    "http://127.0.0.1:8765/health?my tag=v",
    "http://127.0.0.1:8765/health?tag=v  ",
]

# 仅片段含空格：# 之后内容不发送，不参与校验，应允许执行
FRAGMENT_SPACE_URL = "http://127.0.0.1:8765/health?tag=hello%20world#local note"

# 查询中的加号保持原样，不按空格处理
PLUS_QUERY_URL = "http://127.0.0.1:8765/health?tag=a+b"

# JSON 转义字面量（六个 ASCII 字符：反斜杠 u 0 0 2 0）
U0020_ESCAPE = chr(92) + "u0020"


def _write_case(url: str, escaped: bool = False) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。

    escaped=True 时把紧凑 JSON 文本中的原始空格改写为字面转义序列
    ``\\u0020``（紧凑序列化使文本中的空格只可能出现在字符串内部），
    用于验证 JSON 转义还原后的空格与直接书写结果一致。
    """
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        text = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        if escaped:
            text = text.replace(" ", U0020_ESCAPE)
        handle.write(text)
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


class RawSpaceRejectionTests(unittest.TestCase):
    def test_load_case_raises_case_error(self) -> None:
        for url in RAW_SPACE_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("空格", message)
                self.assertIn(url, message, "诊断必须包含完整原始地址")

    def test_load_case_raises_case_error_for_u0020_escaped_json(self) -> None:
        # 用例 JSON 经转义还原后的空格同样必须拒绝，与直接书写结果一致
        for url in RAW_SPACE_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url, escaped=True)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("空格", message)
                self.assertIn(url, message)

    def test_run_case_rejects_before_connection(self) -> None:
        for url in RAW_SPACE_URLS:
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
                self.assertIn("空格", message)
                self.assertIn(url, message, "诊断必须包含完整原始地址")
                self.assertNotIn("Traceback", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"含未编码空格时不得建立连接: {url}",
                )

    def test_rejection_with_server_running_sends_no_request(self) -> None:
        # 服务在线时同样拒绝，服务端收不到任何请求
        with LocalServer() as server:
            for url in RAW_SPACE_URLS:
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
                    self.assertIn("空格", stderr)
                    self.assertIn(url, stderr)
                    self.assertNotIn("Traceback", stderr)
            self.assertEqual(server.get_count, 0, "被拒绝的用例不得发送任何请求")


class EncodedSpaceCompatibilityTests(unittest.TestCase):
    def test_fragment_only_space_passes_validation(self) -> None:
        case_path = _write_case(FRAGMENT_SPACE_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["url"], FRAGMENT_SPACE_URL)
        self.assertEqual(case["_parsed"].fragment, "local note")

    def test_encoded_space_sent_verbatim_with_single_get(self) -> None:
        with LocalServer() as server:
            url = FRAGMENT_SPACE_URL.replace("8765", str(server.port))
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
            self.assertIs(report["field_check"]["present"], True)
            self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
            # %20 原样发送：不解码、不重复编码；片段不发送
            self.assertEqual(server.paths, ["/health?tag=hello%20world"])

    def test_plus_in_query_sent_verbatim(self) -> None:
        with LocalServer() as server:
            url = PLUS_QUERY_URL.replace("8765", str(server.port))
            case_path = _write_case(url)
            self.addCleanup(os.unlink, case_path)
            completed = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(server.paths, ["/health?tag=a+b"])


if __name__ == "__main__":
    unittest.main()
