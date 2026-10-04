"""URL 第一个 # 之前原始制表符/换行符/回车符（U+0009/U+000A/U+000D）的回归
测试：URL 解析会把这三种字符当作空白直接删去（如 /he\\talth 解析成 /health），
因此它们必须在连接前按用例错误拒绝，不删除字符、不自动编码后继续请求。
%09/%0A/%0D（含小写）只是普通百分号文本，按原文本发送；片段（# 之后）不发送，
仅出现在片段中的这些字符允许执行。"""

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

# 第一个 # 之前各处出现原始控制字符的地址：路径、查询、协议、主机、端口
CONTROL_URLS = [
    ("http://127.0.0.1:8765/he\talth", "\\t"),
    ("http://127.0.0.1:8765/health?q=o\nk", "\\n"),
    ("http://127.0.0.1:8765/health?q=o\rk", "\\r"),
    ("http\t://127.0.0.1:8765/health", "\\t"),
    ("http://127.0.0.1\r:8765/health", "\\r"),
    ("http://127.0.0.1:8\t765/health", "\\t"),
]

# 片段不发送给服务端：控制字符（这里还伴随非 ASCII 的片段文本）只出现在 # 之后
FRAGMENT_ONLY_URL = "http://127.0.0.1:8765/health#备注\r\n"

# 合法对照：百分号编码的 %09/%0A/%0D（含小写）原样发送，不解码、不重复编码
ENCODED_URLS = [
    ("http://127.0.0.1:8765/health?q=%09%0A%0D", "/health?q=%09%0A%0D"),
    ("http://127.0.0.1:8765/health?q=%09%0a%0d", "/health?q=%09%0a%0d"),
]


def _write_case(url: str, unicode_escape: bool = False) -> str:
    """生成与 cases/health.json 同构、仅覆盖 url 的临时用例文件。

    unicode_escape 为 True 时把 JSON 文本中的短转义 \\t/\\n/\\r 改写成
    \\u0009/\\u000a/\\u000d，还原后须与短转义得到相同结果。
    """
    case = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    # 紧凑分隔符保证控制字符只出现在 URL 字符串内
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


class ControlCharRejectionTests(unittest.TestCase):
    def test_load_case_raises_case_error(self) -> None:
        for url, token in CONTROL_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                # 必须是 CaseError 本身，不能泄漏普通 ValueError
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("url", message)
                self.assertIn("控制字符", message)
                self.assertIn(token, message, "诊断须以转义字面量标出控制字符")
                # 诊断包含完整原始地址（repr 形式），但不含真实控制字符
                self.assertIn(repr(url), message)
                self.assertNotIn("\t", message)
                self.assertNotIn("\r", message)
                self.assertNotIn("\n", message)

    def test_load_case_raises_case_error_for_unicode_escaped_json(self) -> None:
        # JSON 中写成 \u0009/\u000a/\u000d（短转义 \t/\n/\r 已被默认序列化
        # 覆盖），还原后同样必须拒绝
        for url, token in CONTROL_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url, unicode_escape=True)
                self.addCleanup(os.unlink, case_path)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIs(type(context.exception), CaseError)
                message = str(context.exception)
                self.assertIn("控制字符", message)
                self.assertIn(token, message)
                self.assertIn(repr(url), message)
                self.assertNotIn("\t", message)
                self.assertNotIn("\r", message)
                self.assertNotIn("\n", message)

    def test_run_case_rejects_before_connection(self) -> None:
        # 服务离线时也不得建立连接
        for url, token in CONTROL_URLS:
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
                    # 唯一的换行是行尾终止符；诊断正文不得含真实控制字符
                    self.assertTrue(message.endswith("\n"))
                    content = message[:-1]
                    self.assertIn("控制字符", content)
                    self.assertIn(token, content)
                    self.assertIn(repr(url), content, "诊断必须包含完整原始地址")
                    self.assertNotIn("\t", content)
                    self.assertNotIn("\r", content)
                    self.assertNotIn("\n", content)
                    self.assertNotIn("Traceback", content)
                    # 不得归为 request_failed，也不得自动编码后继续请求
                    self.assertNotIn("request_failed", content)
                    self.assertEqual(
                        connection_cls.call_count,
                        0,
                        f"含未编码控制字符时不得建立连接: {url!r}",
                    )

    def test_fragment_only_control_chars_pass_validation(self) -> None:
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["_parsed"].path, "/health")
        # 片段保留（是否保留控制字符取决于解析器），但第一个 # 之前无控制字符
        self.assertTrue(case["_parsed"].fragment.startswith("备注"))

    def test_rejection_happens_with_server_running_and_no_request_sent(self) -> None:
        # 服务在线时同样拒绝，且服务端收不到任何请求
        with LocalServer() as server:
            for url, token in CONTROL_URLS:
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
                        raw = completed.stderr.decode("utf-8")
                        self.assertEqual(raw.count("\n"), 1)
                        self.assertTrue(raw.endswith("\n"))
                        text = raw[:-1]
                        self.assertTrue(text.startswith("api_workbench:"))
                        self.assertIn("控制字符", text)
                        self.assertIn(token, text)
                        self.assertIn(repr(url), text)
                        self.assertNotIn("\t", text)
                        self.assertNotIn("\r", text)
                        self.assertNotIn("\n", text)
                        self.assertNotIn("Traceback", text)
                        self.assertNotIn("request_failed", text)
            self.assertEqual(server.get_count, 0, "被拒绝的用例不得发送任何请求")


class PercentEncodedCompatibilityTests(unittest.TestCase):
    def test_encoded_and_fragment_urls_pass_validation(self) -> None:
        for url, _ in ENCODED_URLS:
            with self.subTest(url=url):
                case_path = _write_case(url)
                self.addCleanup(os.unlink, case_path)
                case = load_case(case_path)
                self.assertEqual(case["url"], url)
        case_path = _write_case(FRAGMENT_ONLY_URL)
        self.addCleanup(os.unlink, case_path)
        case = load_case(case_path)
        self.assertEqual(case["url"], FRAGMENT_ONLY_URL)

    def test_encoded_urls_sent_verbatim_with_single_get(self) -> None:
        with LocalServer() as server:
            seen_targets = []
            for url, expected_target in ENCODED_URLS:
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
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                    self.assertEqual(completed.stderr, b"")
                    report = json.loads(completed.stdout.decode("utf-8"))
                    self.assertIsNone(report["error"])
                    self.assertTrue(report["passed"])
                    self.assertTrue(report["status_check"]["passed"])
                    self.assertEqual(report["status_check"]["actual"], 200)
                    self.assertTrue(report["field_check"]["passed"])
                    self.assertEqual(report["field_check"]["actual"], "ok")
                    self.assertTrue(report["field_check"]["present"])
                    # 请求目标与输入逐字符一致：不解码、不重复编码
                    self.assertEqual(server.paths[-1:], [expected_target])
                    seen_targets.append(expected_target)
            self.assertEqual(server.get_count, len(ENCODED_URLS))
            self.assertEqual(server.paths, seen_targets)

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
