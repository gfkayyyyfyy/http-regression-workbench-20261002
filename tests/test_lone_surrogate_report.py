"""报告输出中孤立代理项（lone surrogate）的端到端回归测试。

用例文件以 JSON 转义 ``\\ud800`` / ``\\udc00`` 承载代理项，这类值可被
json 正常解析，但无法直接编码为 UTF-8。验证报告输出修复后：

1. name、field、expected_value 中的孤立代理项不导致 UnicodeEncodeError；
2. 响应实际值（含嵌套对象/数组的字符串值与对象键）中的代理项原样保留，
   且合法代理对（表情）、中文、字面反斜杠仍以原文输出；
3. stdout 始终是且仅是一个可按 UTF-8 解码、由标准库 json 解析的完整报告，
   stderr 为空；
4. 解析回的字符串与输入 JSON 解析后的值逐码元一致（不删除、不替换为
   问号或替代字符、不改为描述文字），类型与错误分类规则不变。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_workbench.runner import (
    ASSERTION_FAILED,
    INVALID_RESPONSE,
    REQUEST_FAILED,
    _escape_lone_surrogates,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HIGH = "health\ud800"  # 孤立高代理项
LOW = "\udc00"  # 孤立低代理项


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
    server_version = "api_workbench-test/0.1"

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


class _SurrogateFlowTestCase(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与单报告解析。"""

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

    def _run_case(self, case: dict) -> tuple[dict, subprocess.CompletedProcess, str]:
        """写用例（代理项以 \\uXXXX 转义落盘）、执行、解析并返回。

        返回 (报告, 进程结果, stdout 文本)。同时校验：
        stderr 为空、stdout 可按 UTF-8 解码且仅有一个完整 JSON 报告。
        """
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "wb") as handle:
            # ensure_ascii=True：孤立代理项写成 \uXXXX 转义（用例输入形式），
            # 与手工编写 "health\ud800" 的文件等价
            handle.write(json.dumps(case, ensure_ascii=True).encode("ascii"))

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertNotIn(b"UnicodeEncodeError", completed.stderr)

        stdout_text = completed.stdout.decode("utf-8")  # 必须可按 UTF-8 解码
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed, stdout_text

    def _execute(
        self,
        *,
        name: str,
        field: str,
        expected_value,
        response_status: int = 200,
        response_body: bytes,
        expected_status: int = 200,
    ) -> tuple[dict, subprocess.CompletedProcess, str]:
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": expected_status,
            "field": field,
            "expected_value": expected_value,
        }
        return self._run_case(case)


class LoneSurrogateReportTests(_SurrogateFlowTestCase):
    """孤立代理项出现在报告各处时，输出仍为完整可解析的单报告。"""

    def test_lone_high_surrogate_in_name_passes_and_round_trips(self) -> None:
        report, completed, _ = self._execute(
            name=HIGH,
            field="status",
            expected_value="ok",
            response_body=b'{"status":"ok"}',
        )
        self.assertEqual(report["name"], HIGH)
        self.assertEqual(
            [hex(ord(c)) for c in report["name"]],
            ["0x68", "0x65", "0x61", "0x6c", "0x74", "0x68", "0xd800"],
        )
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1)

    def test_lone_surrogates_in_field_and_expected_value(self) -> None:
        # field 与 expected_value 均含孤立代理项，响应同值时通过
        report, completed, _ = self._execute(
            name="n\udc00",
            field="f\ud800",
            expected_value=LOW,
            response_body=b'{"f\\ud800":"\\udc00"}',
        )
        self.assertEqual(report["name"], "n\udc00")
        self.assertEqual(report["field_check"]["field"], "f\ud800")
        self.assertEqual(report["field_check"]["expected"], LOW)
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), str)
        self.assertEqual(actual, LOW)
        self.assertEqual([hex(ord(c)) for c in actual], ["0xdc00"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)

    def test_lone_low_surrogate_actual_mismatch_preserved(self) -> None:
        # 受控响应 {"status":"\udc00"}，期望 "ok"：assertion_failed，
        # actual 必须保留原字符串而非描述文字或 null
        report, completed, _ = self._execute(
            name=HIGH,
            field="status",
            expected_value="ok",
            response_body=b'{"status":"\\udc00"}',
        )
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertFalse(report["passed"])
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), str)
        self.assertEqual(actual, LOW)
        self.assertEqual([hex(ord(c)) for c in actual], ["0xdc00"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_nested_object_and_array_with_surrogate_keys_and_strings(self) -> None:
        # 实际值为对象/数组时，其中的字符串元素与对象键（含孤立代理项）
        # 都必须原样保留，且 JSON 类型不变
        nested_body = (
            b'{"data":{"k\\ud800":["v\\udc00",1,{"\\udc00":"\\ud800"}],'
            b'"emoji":"\xf0\x9f\x98\x80","cn":"\xe4\xb8\xad\xe6\x96\x87",'
            b'"bs":"a\\\\b"}}'
        )
        report, completed, stdout_text = self._execute(
            name=HIGH,
            field="data",
            expected_value="x",  # 类型不符（对象 vs 字符串），断言失败
            response_body=nested_body,
        )
        self.assertEqual(report["error"], ASSERTION_FAILED)
        actual = report["field_check"]["actual"]
        expected_actual = {
            "k\ud800": ["v\udc00", 1, {"\udc00": "\ud800"}],
            "emoji": "😀",
            "cn": "中文",
            "bs": "a\\b",
        }
        self.assertEqual(actual, expected_actual)
        self.assertIsInstance(actual, dict)
        self.assertIsInstance(actual["k\ud800"], list)
        self.assertEqual(actual["k\ud800"][0], "v\udc00")
        self.assertIs(actual["k\ud800"][1], 1)
        self.assertEqual(list(actual["k\ud800"][2])[0], "\udc00")
        self.assertEqual(actual["k\ud800"][2]["\udc00"], "\ud800")
        self.assertEqual(actual["emoji"], "😀")
        self.assertEqual(actual["cn"], "中文")
        self.assertEqual(actual["bs"], "a\\b")
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)
        # 合法代理对与中文必须以原文 UTF-8 输出，而非 \uXXXX 转义
        self.assertIn("😀".encode("utf-8"), stdout_text.encode("utf-8"))
        self.assertIn("中文".encode("utf-8"), stdout_text.encode("utf-8"))

    def test_invalid_response_with_surrogate_name(self) -> None:
        report, completed, _ = self._execute(
            name=HIGH,
            field="status",
            expected_value="ok",
            response_body=b"this is {not valid json",
        )
        self.assertEqual(report["name"], HIGH)
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_request_failed_with_surrogate_name(self) -> None:
        # 绑定后立即关闭的端口制造稳定连接拒绝；不使用 setUp 的服务
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        case = {
            "name": HIGH,
            "url": f"http://127.0.0.1:{refused_port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": LOW,
        }
        report, completed, _ = self._run_case(case)
        self.assertEqual(report["name"], HIGH)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(report["field_check"]["expected"], LOW)
        self.assertEqual(completed.returncode, 1)


class SurrogateEscapingUnitTests(unittest.TestCase):
    """转义函数本身：只转义孤立代理项，不动合法代理对与其他字符。"""

    def test_pairs_and_plain_text_unchanged(self) -> None:
        text = "中文😀\\字面\tA"
        self.assertEqual(_escape_lone_surrogates(text), text)

    def test_lone_surrogates_become_escape_text(self) -> None:
        # dumps 之后的孤立代理项位于 JSON 字符串中，替换为 \uXXXX 文本
        dumped = json.dumps(["health\ud800", "\udc00"], ensure_ascii=False)
        fixed = _escape_lone_surrogates(dumped)
        self.assertEqual(fixed, '["health\\ud800", "\\udc00"]')
        # 替换后可编码、可解析、值还原
        self.assertEqual(json.loads(fixed.encode("utf-8").decode("utf-8")),
                         ["health\ud800", "\udc00"])

    def test_adjacent_high_and_low_not_treated_as_pair(self) -> None:
        # 高代理项与低代理项不相邻（中间隔字符）时两者都须转义
        dumped = json.dumps("a\ud800b\udc00c", ensure_ascii=False)
        fixed = _escape_lone_surrogates(dumped)
        self.assertEqual(fixed, '"a\\ud800b\\udc00c"')
        self.assertEqual(json.loads(fixed), "a\ud800b\udc00c")

    def test_lone_surrogate_next_to_valid_pair(self) -> None:
        for value in ("\ud800😀", "😀\udc00"):
            with self.subTest(value=value):
                fixed = _escape_lone_surrogates(
                    json.dumps(value, ensure_ascii=False)
                )
                self.assertEqual(json.loads(fixed), value)


if __name__ == "__main__":
    unittest.main()
