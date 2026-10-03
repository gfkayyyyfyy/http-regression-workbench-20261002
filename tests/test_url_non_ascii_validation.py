"""路径/查询中未转义非 ASCII 字符的回归测试。

这类地址必须在连接前按用例错误拒绝（退出码 2、CaseError、不建立连接）；
已百分号编码的地址原样发送（不解码、不重复编码），井号后的片段不参与校验。
"""

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

# 路径或查询中含未转义非 ASCII 字符：中文、重音字母、表情
NON_ASCII_URLS = [
    "http://127.0.0.1:8765/health?tag=回归",
    "http://127.0.0.1:8765/中文",
    "http://127.0.0.1:8765/health?tag=café",
    "http://127.0.0.1:8765/café",
    "http://127.0.0.1:8765/health?tag=😀",
    "http://127.0.0.1:8765/😀",
]

# 用 JSON 的 反斜杠 uXXXX 转义写入文件，json.loads 还原后同样必须拒绝
ESCAPED_URL_LITERAL = "http://127.0.0.1:8765/health?tag=回归"
ESCAPED_URL_JSON = (
    '{"name": "health",'
    ' "url": "http://127.0.0.1:8765/health?tag=\\u56de\\u5f52",'
    ' "expected_status": 200,'
    ' "field": "status",'
    ' "expected_value": "ok"}'
)

# 片段单独含非 ASCII 不发送给服务端，不得因本次校验被拒绝
FRAGMENT_ONLY_URL = "http://127.0.0.1:8765/health#备注"
# 百分号编码的查询 + 中文片段：查询原样发送、片段剥离
ENCODED_URL = (
    "http://127.0.0.1:8765/health?tag=%E5%9B%9E%E5%BD%92#备注"
)
ENCODED_REQUEST_TARGET = "/health?tag=%E5%9B%9E%E5%BD%92"


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


def _write_raw_case(raw_json: str) -> str:
    """把给定 JSON 文本原样写入临时用例文件（用于 \\uXXXX 转义场景）。"""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw_json)
    return path


class _RecordingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数与原始请求目标（self.path）的受控健康检查服务。"""

    get_count = 0
    request_targets: list[str] = []
    _lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
            self.__class__.request_targets.append(self.path)
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
        self.httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "LocalServer":
        # 同一进程内多个用例复用该处理器类，进入时清零计数
        _RecordingHandler.get_count = 0
        _RecordingHandler.request_targets = []
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
    def request_targets(self) -> list[str]:
        return list(_RecordingHandler.request_targets)


class NonAsciiRejectionTests(unittest.TestCase):
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
                self.assertIn("路径或查询", message)
                self.assertIn("未转义", message)
                # 原始地址的可读表示（非 ASCII 字符不转义）
                self.assertIn(url, message)

    def test_load_case_rejects_unicode_escaped_after_json_decode(self) -> None:
        """JSON 中以 \\uXXXX 转义的字符还原后与直接书写同等处理。"""
        case_path = _write_raw_case(ESCAPED_URL_JSON)
        self.addCleanup(os.unlink, case_path)
        with self.assertRaises(CaseError) as context:
            load_case(case_path)
        self.assertIs(type(context.exception), CaseError)
        message = str(context.exception)
        self.assertIn("路径或查询", message)
        self.assertIn(ESCAPED_URL_LITERAL, message)

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
                self.assertIn("路径或查询", message)
                self.assertIn("未转义", message)
                self.assertIn(url, message, "诊断必须包含原始地址的可读表示")
                self.assertNotIn("Traceback", message)
                # 不得归入 request_failed：任何报告都不应产生，连接也不得建立
                self.assertNotIn("request_failed", message)
                self.assertEqual(
                    connection_cls.call_count,
                    0,
                    f"路径或查询含未转义字符时不得建立连接: {url}",
                )

    def test_rejected_as_subprocess(self) -> None:
        """python -m api_workbench run case.json 端到端拒绝行为。"""
        for url in NON_ASCII_URLS:
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
                self.assertIn("路径或查询", stderr)
                self.assertIn("未转义", stderr)
                self.assertIn(url, stderr)
                self.assertNotIn("Traceback", stderr)

    def test_rejected_without_sending_to_server(self) -> None:
        """对真实受控服务：含未转义字符时服务端必须一次请求都收不到。"""
        with LocalServer() as server:
            for suffix in [f"/health?tag=回归", "/中文", "/health?tag=😀"]:
                url = f"http://127.0.0.1:{server.port}{suffix}"
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
                    self.assertNotIn("Traceback", stderr)
        self.assertEqual(server.get_count, 0, "拒绝用例不得向服务端发送请求")


class FragmentAndPercentEncodingTests(unittest.TestCase):
    def test_fragment_only_non_ascii_is_accepted(self) -> None:
        """片段不发送给服务端，单独含中文时不得拒绝。"""
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["url"], FRAGMENT_ONLY_URL)
        self.assertEqual(case["_parsed"].fragment, "备注")

    def test_ascii_paths_and_queries_unchanged(self) -> None:
        """纯 ASCII 的路径与查询不受新校验影响。"""
        for url in [
            "http://127.0.0.1:8765/health",
            "http://127.0.0.1:8765/health?tag=abc",
            "http://127.0.0.1:8765/health?tag=abc%20def#frag",
        ]:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                self.assertEqual(load_case(case_path)["url"], url)

    def test_percent_encoded_url_sent_verbatim_with_single_get(self) -> None:
        """百分号编码地址：只发一次 GET，编码文本原样送达，片段剥离。"""
        with LocalServer() as server:
            case_path = _write_case(
                ENCODED_URL.replace("8765", str(server.port))
            )
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
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
        self.assertEqual(
            server.request_targets,
            [ENCODED_REQUEST_TARGET],
            "百分号编码文本必须原样发送、不解码不重复编码，且不含片段",
        )

    def test_percent_encoded_url_accepted_by_load_case(self) -> None:
        case_path = _write_case(ENCODED_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["_parsed"].query, "tag=%E5%9B%9E%E5%BD%92")
        self.assertEqual(case["_parsed"].fragment, "备注")


if __name__ == "__main__":
    unittest.main()
