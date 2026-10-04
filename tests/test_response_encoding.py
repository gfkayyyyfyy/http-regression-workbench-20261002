"""响应正文编码判定的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证完整收到正文后的编码规则：

1. 只接受严格 UTF-8 编码，允许正文开头有一个 UTF-8 BOM：
   普通 UTF-8 中文、表情以及带 BOM 的 JSON 对象照常解析并参与断言；
2. UTF-8 编码的孤立代理项原始字节（ED A0 80 等）与其他非法 UTF-8
   字节，无论位于目标字段、其他字段、对象键还是嵌套对象/数组，
   整份正文一律 invalid_response，字段 actual/present 为 null，
   状态码 actual 保留真实值，状态检查按期望独立计算；
3. UTF-16、UTF-32 正文（带 BOM 或不带 BOM）一律 invalid_response，
   不依据响应头的 charset 改用其他编码，不忽略坏字节、不替换字符；
4. 200 与 404 两种状态下编码无效都归 invalid_response（后者状态检查
   失败），不能被 assertion_failed 覆盖；
5. ASCII 正文中经 JSON 转义写出的 ``\\ud800`` / ``\\udc00`` 不是原始
   字节，继续按既有规则参与字符串比较并在报告中原样保留，二者不得
   混淆——同样的转义出现在非目标字段也完全合法。

所有场景的 stdout 都只能包含一个可按 UTF-8 解码、json 可解析的完整
报告，stderr 为空、无 Traceback；每次执行服务只收到一次 GET，没有
重试。仅使用 Python 标准库 unittest，服务使用系统分配的临时端口。
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

DEFAULT_CONTENT_TYPE = "application/json; charset=utf-8"

# 验收正文：{"status":"ok","extra":"x"} 中把 x 替换为原始字节 ED A0 80
# （UTF-8 编码的孤立高代理项 U+D800），非法字节位于非目标字段 extra。
RAW_SURROGATE_BODY = b'{"status":"ok","extra":"\xed\xa0\x80"}'


class _ScenarioServer(ThreadingHTTPServer):
    """按测试设定返回固定状态码、Content-Type 与原始正文的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.server_address[1]
        self.status_code = 200
        self.body = b"{}"
        self.content_type = DEFAULT_CONTENT_TYPE
        self.get_count = 0
        self._lock = threading.Lock()

    def set_scenario(
        self,
        status_code: int,
        body: bytes,
        content_type: str = DEFAULT_CONTENT_TYPE,
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


class _EncodingFlowTestCase(unittest.TestCase):
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

    def _run(
        self,
        *,
        response_status: int = 200,
        response_body: bytes = b"{}",
        content_type: str = DEFAULT_CONTENT_TYPE,
        expected_status: int = 200,
        field: str = "status",
        expected_value="ok",
        name: str = "encoding-case",
        case_ensure_ascii: bool = False,
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例、经公开入口执行，返回 (解析后的报告, 进程结果)。"""
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
            # 含孤立代理项的用例需以 ASCII 转义写出，与现有用例书写方式一致
            json.dump(case, handle, ensure_ascii=case_ensure_ascii)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        self.assertEqual(completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}")
        self.assertNotIn(b"Traceback", completed.stderr)
        # stdout 必须严格按 UTF-8 解码，且只包含一个 JSON 报告
        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _assert_invalid_response(
        self,
        report: dict,
        completed: subprocess.CompletedProcess,
        *,
        status_actual: int,
        status_passed: bool,
        expected_status: int = 200,
        expected_value="ok",
        name: str = "encoding-case",
        field: str = "status",
    ) -> None:
        """编码无效：error 固定 invalid_response，字段 actual/present 为 null，
        状态码 actual 与状态检查结果按真实响应独立保留。"""
        expected = {
            "name": name,
            "passed": False,
            "error": INVALID_RESPONSE,
            "status_check": {
                "expected": expected_status,
                "actual": status_actual,
                "passed": status_passed,
            },
            "field_check": {
                "field": field,
                "expected": expected_value,
                "actual": None,
                "passed": False,
                "present": None,
            },
        }
        self.assertEqual(report, expected)
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertNotEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")


class RawSurrogateByteTests(_EncodingFlowTestCase):
    """UTF-8 编码的孤立代理项原始字节（ED A0 80 等）：整份正文无效。"""

    def test_raw_surrogate_bytes_200_is_invalid_with_status_pass(self) -> None:
        # 验收场景：200 + {"status":"ok","extra":"<ED A0 80>"}。
        # 非法字节位于非目标字段，目标字段本可匹配，仍必须判 invalid_response。
        report, completed = self._run(
            response_status=200,
            response_body=RAW_SURROGATE_BODY,
            name="raw-surrogate-200",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="raw-surrogate-200",
        )

    def test_raw_surrogate_bytes_404_is_invalid_with_status_fail(self) -> None:
        # 同一正文改为 404：仍归 invalid_response，状态检查失败，
        # 不能被 assertion_failed 覆盖。
        report, completed = self._run(
            response_status=404,
            response_body=RAW_SURROGATE_BODY,
            name="raw-surrogate-404",
        )
        self._assert_invalid_response(
            report, completed, status_actual=404, status_passed=False,
            name="raw-surrogate-404",
        )

    def test_raw_surrogate_in_target_field_is_invalid(self) -> None:
        report, completed = self._run(
            response_body=b'{"status":"\xed\xa0\x80"}',
            name="raw-surrogate-target",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="raw-surrogate-target",
        )

    def test_raw_low_surrogate_bytes_are_invalid(self) -> None:
        # ED B0 80 = U+DC00 的 UTF-8 编码（孤立低代理项）
        report, completed = self._run(
            response_body=b'{"status":"ok","extra":"\xed\xb0\x80"}',
            name="raw-low-surrogate",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="raw-low-surrogate",
        )

    def test_raw_surrogate_in_object_key_is_invalid(self) -> None:
        # 非法字节位于对象键：字段检查同样无法进行
        report, completed = self._run(
            response_body=b'{"\xed\xa0\x80":1,"status":"ok"}',
            name="raw-surrogate-key",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="raw-surrogate-key",
        )

    def test_raw_surrogate_in_nested_content_is_invalid(self) -> None:
        # 非法字节位于嵌套对象与数组元素：覆盖整份正文，不能让目标字段通过
        bodies = [
            b'{"status":"ok","deep":{"x":"\xed\xa0\x80"}}',
            b'{"status":"ok","items":[1,2,"\xed\xa0\x80"]}',
            b'{"status":"ok","items":[{"x":["\xed\xb0\x80"]}]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed = self._run(
                    response_body=body, name="raw-surrogate-nested"
                )
                self._assert_invalid_response(
                    report, completed, status_actual=200, status_passed=True,
                    name="raw-surrogate-nested",
                )


class OtherInvalidUtf8Tests(_EncodingFlowTestCase):
    """其他非法 UTF-8 字节序列：无论位于何处一律 invalid_response。"""

    def test_various_invalid_bytes_in_other_field(self) -> None:
        bodies = [
            b'{"status":"ok","extra":"\xff"}',                      # 非法起始字节
            b'{"status":"ok","extra":"\xc0\x80"}',                  # 过长编码 NUL
            b'{"status":"ok","extra":"\xe4\xb8"}',                  # 被截断的三字节序列
            b'{"status":"ok","extra":"\xf4\x90\x80\x80"}',          # 超出 U+10FFFF
            b'{"status":"ok","extra":"\xed\xbf\xbf"}',              # U+DFFF 代理项
            b'{"status":"ok","extra":"\x80"}',                      # 裸延续字节
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed = self._run(
                    response_body=body, name="invalid-utf8-other-field"
                )
                self._assert_invalid_response(
                    report, completed, status_actual=200, status_passed=True,
                    name="invalid-utf8-other-field",
                )

    def test_invalid_bytes_in_key_and_nested_content(self) -> None:
        bodies = [
            b'{"status":"ok","\xff":1}',
            b'{"status":"ok","a":{"b":["\xc0\xaf"]}}',
            b'{"status":"ok","a":[[{"\xe4\xb8":true}]]}',
        ]
        for body in bodies:
            with self.subTest(body=body):
                report, completed = self._run(
                    response_body=body, name="invalid-utf8-key-nested"
                )
                self._assert_invalid_response(
                    report, completed, status_actual=200, status_passed=True,
                    name="invalid-utf8-key-nested",
                )

    def test_invalid_bytes_with_404_keep_status_failure(self) -> None:
        report, completed = self._run(
            response_status=404,
            response_body=b'{"status":"ok","extra":"\xff"}',
            name="invalid-utf8-404",
        )
        self._assert_invalid_response(
            report, completed, status_actual=404, status_passed=False,
            name="invalid-utf8-404",
        )


class Utf16Utf32BodyTests(_EncodingFlowTestCase):
    """UTF-16 / UTF-32 正文（含 BOM 与无 BOM）一律 invalid_response。"""

    def _encoded_bodies(self) -> list[tuple[str, bytes]]:
        text = '{"status":"ok","extra":"x"}'
        return [
            ("utf-16 BOM(BE)", b"\xfe\xff" + text.encode("utf-16-be")),
            ("utf-16 BOM(LE)", b"\xff\xfe" + text.encode("utf-16-le")),
            ("utf-16 无 BOM(LE)", text.encode("utf-16-le")),
            ("utf-16 无 BOM(BE)", text.encode("utf-16-be")),
            ("utf-32 BOM(BE)", b"\x00\x00\xfe\xff" + text.encode("utf-32-be")),
            ("utf-32 BOM(LE)", b"\xff\xfe\x00\x00" + text.encode("utf-32-le")),
        ]

    def test_utf16_utf32_bodies_200_are_invalid(self) -> None:
        for label, body in self._encoded_bodies():
            with self.subTest(body=label):
                report, completed = self._run(
                    response_body=body, name=f"non-utf8-{label}"
                )
                self._assert_invalid_response(
                    report, completed, status_actual=200, status_passed=True,
                    name=f"non-utf8-{label}",
                )

    def test_utf16_utf32_bodies_404_are_invalid(self) -> None:
        for label, body in self._encoded_bodies():
            with self.subTest(body=label):
                report, completed = self._run(
                    response_status=404,
                    response_body=body,
                    name=f"non-utf8-{label}-404",
                )
                self._assert_invalid_response(
                    report, completed, status_actual=404, status_passed=False,
                    name=f"non-utf8-{label}-404",
                )


class CharsetHeaderIsIgnoredTests(_EncodingFlowTestCase):
    """判定只看正文字节：响应头 charset 既不会放行坏字节，也不会改判好正文。"""

    def test_utf8_body_with_other_charset_header_still_passes(self) -> None:
        # 头里声称 utf-16 / iso-8859-1，正文实际是严格 UTF-8：照常通过
        body = '{"status":"ok","extra":"中文"}'.encode("utf-8")
        for content_type in (
            "application/json; charset=utf-16",
            "text/plain; charset=iso-8859-1",
            "application/json",
        ):
            with self.subTest(content_type=content_type):
                report, completed = self._run(
                    response_body=body,
                    content_type=content_type,
                    name="charset-header-ignored-pass",
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIsNone(report["error"])
                self.assertTrue(report["passed"])
                self.assertEqual(report["field_check"]["actual"], "ok")
                self.assertEqual(self.server.get_count, 1)

    def test_utf16_body_with_utf8_charset_header_is_invalid(self) -> None:
        # 头里声称 utf-8，正文实际是 UTF-16：不因头部声明而按 UTF-16 解码
        body = b"\xff\xfe" + '{"status":"ok"}'.encode("utf-16-le")
        report, completed = self._run(
            response_body=body,
            content_type="application/json; charset=utf-8",
            name="charset-header-ignored-fail",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="charset-header-ignored-fail",
        )


class ValidUtf8ControlTests(_EncodingFlowTestCase):
    """成功对照：普通 UTF-8 中文、表情与带 BOM 的对象照常通过。"""

    def test_plain_utf8_chinese_passes(self) -> None:
        body = '{"status":"中文","extra":"接口"}'.encode("utf-8")
        report, completed = self._run(
            response_body=body,
            expected_value="中文",
            name="utf8-chinese",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "中文")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIs(report["field_check"]["present"], True)
        self.assertEqual(self.server.get_count, 1)

    def test_utf8_emoji_passes(self) -> None:
        body = '{"status":"😀","extra":"🧜\U0001F600"}'.encode("utf-8")
        report, completed = self._run(
            response_body=body,
            expected_value="😀",
            name="utf8-emoji",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "😀")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])

    def test_utf8_bom_object_passes(self) -> None:
        # 正文开头允许恰好一个 UTF-8 BOM；BOM 不进入字段值
        body = b"\xef\xbb\xbf" + b'{"status":"ok","extra":"x"}'
        report, completed = self._run(response_body=body, name="utf8-bom")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertIs(report["field_check"]["present"], True)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(self.server.get_count, 1)

    def test_utf8_bom_with_chinese_and_emoji_passes(self) -> None:
        inner = '{"status":"中文😀"}'.encode("utf-8")
        report, completed = self._run(
            response_body=b"\xef\xbb\xbf" + inner,
            expected_value="中文😀",
            name="utf8-bom-mixed",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertTrue(report["field_check"]["passed"])

    def test_bom_not_accepted_after_leading_whitespace(self) -> None:
        # 只允许正文“开头”有一个 BOM：前面出现空格后 BOM 成为正文中的
        # 非法字符，整份正文无效（JSON 不接受 U+FEFF）
        report, completed = self._run(
            response_body=b" \xef\xbb\xbf" + b'{"status":"ok"}',
            name="bom-after-space",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="bom-after-space",
        )

    def test_double_bom_is_invalid(self) -> None:
        # 第二个 BOM 不再是开头标记：作为 U+FEFF 出现在 JSON 文本中，非法
        report, completed = self._run(
            response_body=b"\xef\xbb\xbf\xef\xbb\xbf" + b'{"status":"ok"}',
            name="double-bom",
        )
        self._assert_invalid_response(
            report, completed, status_actual=200, status_passed=True,
            name="double-bom",
        )


class AsciiEscapedSurrogateTests(_EncodingFlowTestCase):
    """JSON 转义 \\ud800 / \\udc00 是文本语法，不是原始字节，规则保持不变。"""

    def test_escaped_high_surrogate_matches_and_is_preserved(self) -> None:
        # ASCII 正文：{"status":"\ud800"}（反斜杠为字面 ASCII 字符）
        report, completed = self._run(
            response_body=b'{"status":"\\ud800"}',
            expected_value="\ud800",
            case_ensure_ascii=True,
            name="escape-d800",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertIsInstance(report["field_check"]["actual"], str)
        self.assertEqual(report["field_check"]["actual"], "\ud800")
        self.assertEqual(report["field_check"]["expected"], "\ud800")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(self.server.get_count, 1)

    def test_escaped_low_surrogate_matches_and_is_preserved(self) -> None:
        report, completed = self._run(
            response_body=b'{"status":"\\udc00"}',
            expected_value="\udc00",
            case_ensure_ascii=True,
            name="escape-dc00",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "\udc00")
        self.assertTrue(report["field_check"]["passed"])

    def test_escaped_surrogate_mismatch_is_assertion_failed_with_value(self) -> None:
        # 与原始字节场景区分：转义值可解析、可比较，不符时是 assertion_failed，
        # actual 原样保留而非 null，present 为 true
        report, completed = self._run(
            response_body=b'{"status":"\\udc00"}',
            expected_value="ok",
            name="escape-mismatch",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(report["field_check"]["actual"], "\udc00")
        self.assertIs(report["field_check"]["present"], True)
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_escaped_surrogate_in_other_field_still_passes(self) -> None:
        # 非目标字段中的 ASCII 转义代理项完全合法，不与 ED A0 80 原始字节混淆
        report, completed = self._run(
            response_body=b'{"status":"ok","extra":"\\ud800","x":["\\udc00"]}',
            name="escape-other-field",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsNone(report["error"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(self.server.get_count, 1)


if __name__ == "__main__":
    unittest.main()
