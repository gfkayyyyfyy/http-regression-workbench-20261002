"""报告输出保留孤立代理项的端到端回归测试。

用例文件中的 JSON 转义 \\ud800 / \\udc00 会被既有加载规则接受（解析为
Python 中的孤立代理项），但这类字符无法直接编码为 UTF-8。报告输出必须：

1. 对 name、field、expected_value 以及响应实际值（含对象/数组嵌套内容
   与对象键）中的孤立代理项原样保留，stdout 仍是唯一一份可按 UTF-8
   解码、可由标准库 json 解析的完整报告；
2. 不删除代理项、不替换为问号或替代字符，actual 仍为原值而非描述文字；
3. 普通中文、表情、合法代理对与字面反斜杠照常输出并保持严格类型比较；
4. 既有错误分类（invalid_response / request_failed）与用例错误退出码 2
   （stdout 为空）的行为不受影响。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _ScenarioServer(ThreadingHTTPServer):
    """按测试设定返回固定状态码与原始正文的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.server_address[1]
        self.status_code = 200
        self.body = b"{}"
        self.get_count = 0
        self._lock = threading.Lock()

    def set_scenario(self, status_code: int, body: bytes) -> None:
        with self._lock:
            self.status_code = status_code
            self.body = body
            self.get_count = 0


class _ScenarioHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-surrogate-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _ScenarioServer = self.server
        with server._lock:
            status_code = server.status_code
            body = server.body
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class LoneSurrogateReportTests(unittest.TestCase):
    """孤立代理项在报告各位置均原样保留，输出不中断。"""

    def setUp(self) -> None:
        self.server = _ScenarioServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _run(
        self,
        case: dict,
        *,
        response_status: int = 200,
        response_body: bytes = b"{}",
    ) -> subprocess.CompletedProcess:
        self.server.set_scenario(response_status, response_body)
        case_path = os.path.join(self.tmpdir.name, "case.json")
        # ensure_ascii 使孤立代理项以 \uXXXX 转义写入文件，
        # 与验收场景中“name 改成 JSON 字符串 "health\ud800"”一致。
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=True)
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _case(self, **overrides) -> dict:
        case = {
            "name": "health\ud800",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case.update(overrides)
        return case

    def _parse_single_report(self, completed: subprocess.CompletedProcess) -> dict:
        """stdout 必须是唯一一份可按 UTF-8 解码、json 可解析的完整报告。"""
        self.assertEqual(completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}")
        text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(text)
        self.assertEqual(text[end:].strip(), "", f"stdout 只能包含一个 JSON 报告: {text!r}")
        self.assertIsInstance(report, dict)
        return report

    def test_lone_surrogate_in_name_passes_and_is_preserved(self) -> None:
        completed = self._run(
            self._case(),
            response_body=b'{"status":"ok"}',
        )
        self.assertEqual(completed.returncode, 0)
        report = self._parse_single_report(completed)
        self.assertEqual(report["name"], "health\ud800")
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(self.server.get_count, 1)

    def test_lone_low_surrogate_actual_matching_expected_passes(self) -> None:
        completed = self._run(
            self._case(name="case\udc00", expected_value="\udc00"),
            response_body=b'{"status":"\\udc00"}',
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = self._parse_single_report(completed)
        self.assertEqual(report["name"], "case\udc00")
        self.assertEqual(report["field_check"]["expected"], "\udc00")
        actual = report["field_check"]["actual"]
        self.assertIsInstance(actual, str)
        self.assertEqual(actual, "\udc00")
        self.assertTrue(report["field_check"]["passed"])
        self.assertIsNone(report["error"])

    def test_lone_surrogate_actual_retained_on_assertion_failure(self) -> None:
        completed = self._run(
            self._case(expected_value="ok"),
            response_body=b'{"status":"\\udc00"}',
        )
        self.assertEqual(completed.returncode, 1)
        report = self._parse_single_report(completed)
        self.assertEqual(report["error"], "assertion_failed")
        actual = report["field_check"]["actual"]
        # 实际值必须原样保留（字符串且等于含孤立代理项的原值），
        # 不能改成 null、问号、替代字符或描述文字
        self.assertIsInstance(actual, str)
        self.assertEqual(actual, "\udc00")
        self.assertNotIn("?", actual)
        self.assertNotIn("�", actual)
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_surrogates_in_nested_values_and_object_keys_are_preserved(self) -> None:
        completed = self._run(
            self._case(expected_value="ok"),
            response_body=(
                b'{"status":["a",{"k":"\\ud800"},"\\udc02"],'
                b'"\\udc01":{"deep":"x\\ud800y"}}'
            ),
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = self._parse_single_report(completed)
        self.assertEqual(
            report["field_check"]["actual"],
            ["a", {"k": "\ud800"}, "\udc02"],
        )

    def test_field_name_with_surrogate_is_preserved(self) -> None:
        # field 本身含孤立代理项，且响应中的对象键也含孤立代理项
        completed = self._run(
            self._case(field="st\ud800atus", expected_value="ok"),
            response_body=b'{"st\\ud800atus":"ok","\\udc01":1}',
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = self._parse_single_report(completed)
        self.assertEqual(report["field_check"]["field"], "st\ud800atus")
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])

    def test_surrogate_name_with_invalid_response_still_classified(self) -> None:
        completed = self._run(
            self._case(),
            response_body=b"this is {not valid json",
        )
        self.assertEqual(completed.returncode, 1)
        report = self._parse_single_report(completed)
        self.assertEqual(report["error"], "invalid_response")
        self.assertEqual(report["name"], "health\ud800")
        self.assertIsNone(report["field_check"]["actual"])

    def test_surrogate_name_with_connection_failure_still_classified(self) -> None:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        dead_port = free.getsockname()[1]
        free.close()
        completed = self._run(self._case(url=f"http://127.0.0.1:{dead_port}/x"))
        self.assertEqual(completed.returncode, 1)
        report = self._parse_single_report(completed)
        self.assertEqual(report["error"], "request_failed")
        self.assertEqual(report["name"], "health\ud800")

    def test_case_error_with_surrogate_name_still_rejected_before_request(self) -> None:
        # expected_status 类型错误：请求前拒绝，退出码 2，stdout 为空
        completed = self._run(self._case(expected_status="200"))
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertEqual(self.server.get_count, 0)

    def test_chinese_emoji_valid_pair_and_backslash_unchanged(self) -> None:
        value = "中文😀\U0001F600路径\\end"
        # 响应正文以 ASCII 转义书写：表情成为合法代理对 😀，
        # 中文为 \uXXXX，字面反斜杠为 \\
        body = json.dumps({"status": value}, ensure_ascii=True).encode("ascii")
        completed = self._run(
            self._case(name="用例\ud800", expected_value=value),
            response_body=body,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = self._parse_single_report(completed)
        self.assertEqual(report["name"], "用例\ud800")
        self.assertEqual(report["field_check"]["actual"], value)
        self.assertEqual(report["field_check"]["expected"], value)
        self.assertTrue(report["field_check"]["passed"])

    def test_boolean_expected_still_strict_with_surrogate_name(self) -> None:
        completed = self._run(
            self._case(expected_value=True),
            response_body=b'{"status":true}',
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = self._parse_single_report(completed)
        self.assertIs(report["field_check"]["actual"], True)
        self.assertTrue(report["field_check"]["passed"])


if __name__ == "__main__":
    unittest.main()
