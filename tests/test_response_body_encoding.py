"""响应正文编码判定的端到端回归测试。

完整收到正文后，只接受严格 UTF-8 编码（开头允许恰好一个 UTF-8 BOM）：
UTF-16/UTF-32 正文、孤立代理项等非法 UTF-8 字节一律判为 invalid_response，
不依据响应头的 charset 改用其他编码，不忽略坏字节或以替代字符顶替；
编码检查覆盖整份正文，坏字节位于非目标字段、对象键或嵌套内容时，
目标字段断言同样不能通过。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 原始孤立代理项字节 ED A0 80（位于非目标字段 extra，结构为
   {"status":"ok","extra":…}）：200 时为 invalid_response 且状态检查
   通过；404 时仍为 invalid_response、状态检查失败，不被
   assertion_failed 覆盖；状态码 actual 始终保留真实值，字段
   actual/present 均为 null，退出码 1；
2. 其他非法 UTF-8（0xFF 0xFE、截断的多字节序列、超长序列、
   孤立延续字节、双 BOM），以及带/不带 BOM 的 UTF-16、UTF-32
   正文（LE/BE）一律 invalid_response，即使响应头声明 charset=utf-8
   也不按该编码解码；
3. 坏字节位于嵌套对象、数组元素或对象键时整份正文仍无效；
4. 对照通过：普通 UTF-8 中文与表情、开头带一个 BOM 的对象照常解析
   并参与既有严格类型比较；响应头谎称 charset=utf-16 不影响 UTF-8
   正文被正确接受（charset 一律不参考）；
5. ASCII 正文中以 JSON 转义书写的 \\ud800 / \\udc00 仍按既有规则
   参与字符串比较、在报告中原样保留（匹配通过、不匹配为
   assertion_failed），不与非法原始字节混淆。

stdout 始终只有一个可按 UTF-8 解码的 JSON 报告，stderr 为空、
无 Traceback；每次执行服务只收到一次 GET。仅使用 Python 标准库
unittest；服务使用系统分配的临时端口。
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

from api_workbench.runner import ASSERTION_FAILED, INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMMAND_HARD_LIMIT = 10.0

# 原始孤立高代理项的 UTF-8 编码：ED A0 80（U+D800 在合法 UTF-8 中不存在）
RAW_SURROGATE = b"\xed\xa0\x80"
UTF8_BOM = b"\xef\xbb\xbf"


def _utf16(text: str, bom: bytes | None, endian: str) -> bytes:
    body = text.encode(f"utf-16-{endian}")
    return bom + body if bom is not None else body


def _utf32(text: str, bom: bytes | None, endian: str) -> bytes:
    body = text.encode(f"utf-32-{endian}")
    return bom + body if bom is not None else body


VALID_OBJECT = '{"status":"ok","extra":"x"}'


class _ScenarioServer(ThreadingHTTPServer):
    """按测试设定返回固定状态码、原始正文与 Content-Type 的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.server_address[1]
        self.status_code = 200
        self.body = b"{}"
        # 默认谎称 charset=utf-16：证明客户端从不参考响应头 charset，
        # 合法 UTF-8 正文照样按 UTF-8 接受
        self.content_type = "application/json; charset=utf-16"
        self.get_count = 0
        self._lock = threading.Lock()

    def set_scenario(
        self,
        status_code: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-16",
    ) -> None:
        with self._lock:
            self.status_code = status_code
            self.body = body
            self.content_type = content_type
            self.get_count = 0


class _ScenarioHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-encoding-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _ScenarioServer = self.server
        with server._lock:
            status_code = server.status_code
            body = server.body
            content_type = server.content_type
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class BodyEncodingTests(unittest.TestCase):
    """响应正文编码判定及与既有规则的边界。"""

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
        *,
        response_status: int = 200,
        response_body: bytes,
        content_type: str = "application/json; charset=utf-16",
        expected_status: int = 200,
        field: str = "status",
        expected_value="ok",
        name: str = "encoding-case",
    ) -> tuple[dict, subprocess.CompletedProcess]:
        self.server.set_scenario(response_status, response_body, content_type)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": expected_status,
            "field": field,
            "expected_value": expected_value,
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=True)
        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=COMMAND_HARD_LIMIT,
        )
        return self._parse_single_report(completed), completed

    def _parse_single_report(self, completed: subprocess.CompletedProcess) -> dict:
        """stdout 必须是唯一一份可按 UTF-8 解码、json 可解析的完整报告。"""
        self.assertEqual(completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}")
        self.assertNotIn(b"Traceback", completed.stderr)
        text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(text)
        self.assertEqual(text[end:].strip(), "", f"stdout 只能包含一个 JSON 报告: {text!r}")
        self.assertIsInstance(report, dict)
        return report

    def _assert_invalid(
        self,
        report: dict,
        completed: subprocess.CompletedProcess,
        *,
        status_actual: int,
        status_passed: bool,
    ) -> None:
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"], "无效正文的字段 actual 为 null")
        self.assertIsNone(report["field_check"]["present"], "无效正文无法检查字段，present 为 null")
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    # ---- 验收主场景：原始代理项字节 ED A0 80 ----

    def test_raw_surrogate_bytes_200_is_invalid_but_status_passes(self) -> None:
        body = b'{"status":"ok","extra":"' + RAW_SURROGATE + b'"}'
        report, completed = self._run(response_status=200, response_body=body)
        self._assert_invalid(
            report, completed, status_actual=200, status_passed=True
        )

    def test_raw_surrogate_bytes_404_stays_invalid_with_failing_status(self) -> None:
        body = b'{"status":"ok","extra":"' + RAW_SURROGATE + b'"}'
        report, completed = self._run(response_status=404, response_body=body)
        # 状态检查失败，分类仍是 invalid_response，不能被 assertion_failed 覆盖
        self._assert_invalid(
            report, completed, status_actual=404, status_passed=False
        )
        self.assertNotEqual(report["error"], ASSERTION_FAILED)

    def test_raw_surrogate_bytes_even_with_utf8_charset_header_is_invalid(self) -> None:
        # 响应头诚实声明 utf-8 也不能让非法字节变合法
        body = b'{"status":"ok","extra":"' + RAW_SURROGATE + b'"}'
        report, completed = self._run(
            response_status=200,
            response_body=body,
            content_type="application/json; charset=utf-8",
        )
        self._assert_invalid(
            report, completed, status_actual=200, status_passed=True
        )

    # ---- 其他非法 UTF-8 字节序列 ----

    def test_other_illegal_utf8_sequences_are_invalid(self) -> None:
        bodies = {
            "ff-fe": b'{"status":"ok","extra":"\xff\xfe"}',
            "truncated-multibyte": '{"status":"ok","extra":"中"}'.encode("utf-8")[:-1],
            "overlong-slash": b'{"status":"ok","extra":"\xc0\xaf"}',
            "lone-continuation": b'{"status":"ok","extra":"\x80"}',
            "lead-without-continuation": b'{"status":"ok","extra":"\xc3"}',
            "two-boms": UTF8_BOM + UTF8_BOM + VALID_OBJECT.encode("utf-8"),
        }
        for label, body in bodies.items():
            with self.subTest(label=label):
                report, completed = self._run(
                    response_status=200, response_body=body
                )
                self._assert_invalid(
                    report, completed, status_actual=200, status_passed=True
                )

    # ---- UTF-16 / UTF-32 正文 ----

    def test_utf16_bodies_are_invalid(self) -> None:
        cases = {
            "utf16le-bom": _utf16(VALID_OBJECT, b"\xff\xfe", "le"),
            "utf16be-bom": _utf16(VALID_OBJECT, b"\xfe\xff", "be"),
            "utf16le-no-bom": _utf16(VALID_OBJECT, None, "le"),
            "utf16be-no-bom": _utf16(VALID_OBJECT, None, "be"),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                # 即使头里声明 charset=utf-8 也绝不按 UTF-16 解码
                report, completed = self._run(
                    response_status=200,
                    response_body=body,
                    content_type="application/json; charset=utf-8",
                )
                self._assert_invalid(
                    report, completed, status_actual=200, status_passed=True
                )

    def test_utf32_bodies_are_invalid(self) -> None:
        cases = {
            "utf32le-bom": _utf32(VALID_OBJECT, b"\xff\xfe\x00\x00", "le"),
            "utf32be-bom": _utf32(VALID_OBJECT, b"\x00\x00\xfe\xff", "be"),
            "utf32le-no-bom": _utf32(VALID_OBJECT, None, "le"),
            "utf32be-no-bom": _utf32(VALID_OBJECT, None, "be"),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                report, completed = self._run(
                    response_status=404, response_body=body
                )
                self._assert_invalid(
                    report, completed, status_actual=404, status_passed=False
                )

    # ---- 坏字节位于非目标位置：嵌套对象、数组、对象键 ----

    def test_bad_bytes_anywhere_make_whole_body_invalid(self) -> None:
        bodies = {
            "nested-object": b'{"status":"ok","other":{"a":"' + RAW_SURROGATE + b'"}}',
            "array-element": b'{"status":"ok","items":[1,2,"\xff"]}',
            "object-key": b'{"status":"ok","' + RAW_SURROGATE + b'":1}',
            "nested-key": b'{"status":"ok","deep":[{"\xed\xb0\x80x":1}]}',
        }
        for label, body in bodies.items():
            with self.subTest(label=label):
                # status 字段本身完好且等于 "ok"，但整份正文编码无效，
                # 目标字段断言绝不能通过
                report, completed = self._run(
                    response_status=200, response_body=body
                )
                self._assert_invalid(
                    report, completed, status_actual=200, status_passed=True
                )

    # ---- 成功对照 ----

    def test_plain_utf8_chinese_and_emoji_pass(self) -> None:
        value = "中文😀"
        body = json.dumps({"status": value}, ensure_ascii=False).encode("utf-8")
        report, completed = self._run(
            response_status=200, response_body=body, expected_value=value
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], value)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])

    def test_single_leading_bom_is_accepted_and_stripped(self) -> None:
        value = "中文ok"
        body = UTF8_BOM + json.dumps(
            {"status": value}, ensure_ascii=False
        ).encode("utf-8")
        report, completed = self._run(
            response_status=200, response_body=body, expected_value=value
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], value)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])

    def test_bom_ascii_body_passes_under_lying_charset_header(self) -> None:
        # 头里写 charset=utf-16，合法 UTF-8（带 BOM）正文仍须照常接受
        body = UTF8_BOM + b'{"status":"ok"}'
        report, completed = self._run(response_status=200, response_body=body)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])

    # ---- ASCII JSON 转义的代理项不与原始坏字节混淆 ----

    def test_ascii_escaped_lone_surrogate_matches_and_is_preserved(self) -> None:
        report, completed = self._run(
            response_status=200,
            response_body=b'{"status":"\\ud800"}',
            expected_value="\ud800",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "\ud800")
        self.assertTrue(report["field_check"]["passed"])

    def test_ascii_escaped_low_surrogate_mismatch_is_assertion_failure(self) -> None:
        report, completed = self._run(
            response_status=200,
            response_body=b'{"status":"\\udc00","extra":"x"}',
            expected_value="ok",
        )
        self.assertEqual(completed.returncode, 1)
        # 分类必须是 assertion_failed 而非 invalid_response
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(report["field_check"]["actual"], "\udc00")
        self.assertIs(report["field_check"]["present"], True)
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])

    def test_ascii_escaped_surrogates_in_other_positions_still_parse(self) -> None:
        # 非目标字段、嵌套对象与对象键中的 \uXXXX 转义全部合法
        body = (
            b'{"status":"ok","other":"\\udc01",'
            b'"arr":["\\ud800"],"\\udc02":{"deep":"\\ud800x"}}'
        )
        report, completed = self._run(response_status=200, response_body=body)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])
        self.assertIs(report["field_check"]["present"], True)

    def test_legal_surrogate_pair_emoji_via_ascii_escape_passes(self) -> None:
        # 😀 是 😀 的合法代理对转义，按既有规则解析并参与比较
        body = b'{"status":"\\ud83d\\ude00"}'
        report, completed = self._run(
            response_status=200, response_body=body, expected_value="😀"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "😀")


if __name__ == "__main__":
    unittest.main()
