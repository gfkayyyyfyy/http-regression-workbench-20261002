"""标量数组期望（expected_value 为数组）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 除已有标量（字符串、布尔值、有限数字、null）外，
   还接受空数组或仅由这些标量组成的数组，元素可混合；
2. 数组期望只匹配数组实际值：按长度、顺序及各位置的值逐项比较，
   不排序、不去重；空数组只匹配空数组；元素沿用严格类型规则
   （true 不等于 1，"1" 不等于 1，数字 1 与 1.0 相等，null 只匹配 null，
   大整数仍按任意精度精确比较）；
3. 非法 expected_value（缺失、对象，或含对象、嵌套数组、非有限数字的数组）
   使 load_case 抛出 CaseError：不发送请求，stdout 为空，stderr 以
   api_workbench: 开头并提及 expected_value，退出码 2，无 Traceback；
4. 字段缺失时 present=false、actual=null；字段存在但类型、长度或元素
   不符时 present=true、actual 完整保留，error 为 assertion_failed、
   退出码 1；状态码不符时仍检查字段；两项检查都通过时 error=null、
   passed=true、退出码 0，报告的 expected 保留数组；
5. 每次有效执行仅发送一次 GET，stdout 只含一个 JSON 报告，stderr 为空。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765、不需要手工启动服务或访问公网。
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
    CaseError,
    load_case,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIAGNOSTIC_PREFIX = "api_workbench:"


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


class _ArrayFlowTestCase(unittest.TestCase):
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

    def _write_case(self, expected_value) -> str:
        case = {
            "name": "items",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "items",
            "expected_value": expected_value,
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)
        return case_path

    def _execute(
        self,
        *,
        expected_value,
        response_status: int = 200,
        response_body: bytes = b'{"items":[true,1.0]}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例（name=items, field=items）、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case_path = self._write_case(expected_value)
        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}")
        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertEqual(self.server.get_count, 1, "每次有效执行只应发送一次 GET")
        return report, completed


class ArrayMatchTests(_ArrayFlowTestCase):
    """数组期望与数组实际值的逐项严格比较。"""

    def test_mixed_scalar_array_matches_with_number_int_float_equivalence(self) -> None:
        # 验收场景：期望 [true, 1]，实际 [true, 1.0]（数字 1 与 1.0 相等）
        report, completed = self._execute(expected_value=[True, 1])
        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(
            report["field_check"],
            {
                "field": "items",
                "expected": [True, 1],
                "actual": [True, 1.0],
                "passed": True,
                "present": True,
            },
        )

    def test_element_type_mismatch_fails_and_actual_is_preserved(self) -> None:
        # 验收场景：期望 [1, 1]，实际 [true, 1.0]（true 不等于 1）
        report, completed = self._execute(expected_value=[1, 1])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertFalse(report["passed"])
        self.assertTrue(report["status_check"]["passed"])
        field_check = report["field_check"]
        self.assertIs(field_check["present"], True)
        self.assertEqual(field_check["expected"], [1, 1])
        self.assertEqual(field_check["actual"], [True, 1.0])
        self.assertFalse(field_check["passed"])

    def test_order_matters(self) -> None:
        report, completed = self._execute(expected_value=[1, True])
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], [True, 1.0])

    def test_length_mismatch_fails(self) -> None:
        for expected in ([True], [True, 1, "x"]):
            with self.subTest(expected=expected):
                report, completed = self._execute(expected_value=expected)
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["field_check"]["actual"], [True, 1.0])

    def test_empty_array_matches_only_empty_array(self) -> None:
        report, completed = self._execute(
            expected_value=[], response_body=b'{"items":[]}'
        )
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], [])

        for body in (b'{"items":[1]}', b'{"items":{}}', b'{"items":null}'):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=[], response_body=body
                )
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report["field_check"]["passed"])

    def test_element_strict_type_rules(self) -> None:
        # (期望, 实际正文, 是否通过)：true≠1、"1"≠1、null 只匹配 null、1 与 1.0 相等
        scenarios = [
            ([True], b'{"items":[1]}', False),
            ([1], b'{"items":[true]}', False),
            (["1"], b'{"items":[1]}', False),
            ([1], b'{"items":["1"]}', False),
            ([None], b'{"items":[0]}', False),
            ([None], b'{"items":[false]}', False),
            ([None], b'{"items":[null]}', True),
            ([1], b'{"items":[1.0]}', True),
            ([1.0], b'{"items":[1]}', True),
            (["a", False, 2.5, None], b'{"items":["a",false,2.5,null]}', True),
        ]
        for expected, body, should_pass in scenarios:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertEqual(report["field_check"]["passed"], should_pass)
                self.assertEqual(
                    completed.returncode, 0 if should_pass else 1
                )

    def test_big_integer_element_keeps_arbitrary_precision(self) -> None:
        big = 10**30
        report, completed = self._execute(
            expected_value=[big],
            response_body=b'{"items":[1' + b"0" * 30 + b"]}",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(report["field_check"]["expected"], [big])

        report, completed = self._execute(
            expected_value=[big],
            response_body=b'{"items":[1' + b"0" * 29 + b"]}",
        )
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["field_check"]["passed"])

    def test_array_expected_does_not_match_non_array_actual(self) -> None:
        for body in (
            b'{"items":true}',
            b'{"items":1}',
            b'{"items":"[true,1]"}',
            b'{"items":{}}',
            b'{"items":null}',
        ):
            with self.subTest(body=body):
                report, completed = self._execute(expected_value=[True, 1], response_body=body)
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report["field_check"]["passed"])

    def test_scalar_expected_does_not_match_array_actual(self) -> None:
        for expected in (True, 1, "[true,1]", None):
            with self.subTest(expected=expected):
                report, completed = self._execute(expected_value=expected)
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["field_check"]["actual"], [True, 1.0])

    def test_missing_field_reports_present_false_and_actual_null(self) -> None:
        report, completed = self._execute(
            expected_value=[True, 1], response_body=b'{"other":[true,1.0]}'
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], ASSERTION_FAILED)
        field_check = report["field_check"]
        self.assertIs(field_check["present"], False)
        self.assertIsNone(field_check["actual"])
        self.assertFalse(field_check["passed"])

    def test_status_mismatch_still_checks_field(self) -> None:
        report, completed = self._execute(
            expected_value=[True, 1], response_status=500
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertTrue(report["field_check"]["passed"])
        self.assertFalse(report["passed"])


class ArrayExpectedValueLoadingTests(_ArrayFlowTestCase):
    """加载阶段：标量数组合法；对象、嵌套数组与非有限数字元素拒绝。"""

    def _write_raw_case(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 NaN/1e400 等类型。"""
        fields = {
            "name": "items",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "items",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        case_path = os.path.join(self.tmpdir.name, "raw_case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return case_path

    def test_load_case_accepts_scalar_arrays(self) -> None:
        for raw_value, expected in [
            ("[]", []),
            ("[true, false]", [True, False]),
            ('[1, 1.5, "a", null]', [1, 1.5, "a", None]),
            ("[1e0]", [1.0]),
            # 大整数元素按任意精度接受
            ["[1" + "0" * 30 + "]", [10**30]],
        ]:
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(loaded["expected_value"], expected)

    def test_load_case_rejects_invalid_arrays(self) -> None:
        # 对象、嵌套数组与非有限数字（NaN/Infinity/-Infinity、1e400 溢出）
        # 作为元素或整体一律拒绝
        for raw_value in (
            "{}",
            "[{}]",
            '[{"a": 1}]',
            "[[]]",
            "[[1]]",
            "[1, [2]]",
            "[NaN]",
            "[Infinity]",
            "[-Infinity]",
            "[1e400]",
            "[1, -1E+400]",
        ):
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_invalid_arrays_without_sending_request(self) -> None:
        for raw_value in ("{}", "[{}]", "[[1]]", "[NaN]", "[1e400]"):
            with self.subTest(raw_value=raw_value):
                self.server.set_scenario(200, b'{"items":[true,1.0]}')
                case_path = self._write_raw_case(raw_value)
                completed = subprocess.run(
                    [sys.executable, "-m", "api_workbench", "run", case_path],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    timeout=10,
                )
                stderr = completed.stderr.decode("utf-8")
                self.assertEqual(completed.returncode, 2, stderr)
                self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
                self.assertTrue(
                    stderr.startswith(DIAGNOSTIC_PREFIX),
                    f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr!r}",
                )
                self.assertIn("expected_value", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(
                    self.server.get_count,
                    0,
                    f"expected_value={raw_value!r} 非法时不得发送 GET",
                )


if __name__ == "__main__":
    unittest.main()
