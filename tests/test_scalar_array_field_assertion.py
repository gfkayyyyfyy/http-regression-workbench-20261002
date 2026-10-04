"""标量数组期望（expected_value 为 JSON 数组）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 除既有标量外接受空数组，或仅含字符串、布尔值、有限 JSON
   数字与 null 的一维数组，元素可混合；数组按长度、顺序及各位置的值比较，
   不排序、不去重；元素沿用标量的严格类型规则——true 不匹配 1，
   "1" 不匹配 1，数字 1 与 1.0 互相匹配，null 只匹配 null；
   数组只匹配数组，[] 只匹配 []；
2. 验收场景：服务返回 200 与 {"items":[true,1.0]}，field=items、
   expected_value=[true,1] 时整体通过、退出码 0；仅把期望改为 [1,1]
   再执行，字段失败且实际数组 [true,1.0] 完整保留、退出码 1；
3. 报告结构不变，field_check.expected/actual 保留数组与各元素的 JSON
   类型；字段缺失时 present 为 false、actual 为 null；字段存在但类型、
   长度或元素不符时 present 为 true、actual 原样保留；
4. expected_value 缺失，或为含对象/嵌套数组/非有限数字的数组、含嵌套对象或
   数组成员的对象时，load_case 抛出 CaseError：不发送请求，stdout 为空，
   stderr 以 api_workbench: 开头并指出 expected_value，退出码 2，无 Traceback
   （仅含标量成员的对象合法，见 test_object_field_assertion.py）；
5. 数组期望不改变既有分类：非 JSON 对象的响应仍为 invalid_response
   （保留状态码检查结果），连接失败仍为 request_failed，退出码 1；
6. 状态码不符时仍检查字段；每次有效执行仅发送一次 GET；
   大整数元素按任意精度保留并逐位比较。

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
    INVALID_RESPONSE,
    REQUEST_FAILED,
    CaseError,
    load_case,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIAGNOSTIC_PREFIX = "api_workbench:"

BIG_INT_TEXT = "1" + "0" * 400


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

    def _write_raw_case(self, raw_expected_value: str) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 1、1.0、大整数等写法。"""
        fields = {
            "name": "array",
            "url": f"http://127.0.0.1:{self.server.port}/items",
            "expected_status": 200,
            "field": "items",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def _execute(
        self,
        *,
        expected_value=None,
        response_status: int = 200,
        response_body: bytes = b'{"items":[]}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例（name=array, field=items）、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": "array",
            "url": f"http://127.0.0.1:{self.server.port}/items",
            "expected_status": 200,
            "field": "items",
            "expected_value": expected_value,
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

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


class ArrayExpectedValueMatchingTests(_ArrayFlowTestCase):
    """数组期望：长度、顺序与逐位置严格类型匹配，报告保留数组与元素类型。"""

    def test_acceptance_mixed_bool_and_number_array_passes(self) -> None:
        # 验收场景：GET /items 返回 200 {"items":[true,1.0]}，
        # expected_value 为 [true,1]：true 匹配 true、1 匹配 1.0
        report, completed = self._execute(
            expected_value=[True, 1],
            response_body=b'{"items":[true,1.0]}',
        )
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "items")
        self.assertEqual(report["field_check"]["expected"], [True, 1])
        self.assertIs(type(report["field_check"]["expected"][0]), bool)
        self.assertIs(type(report["field_check"]["expected"][1]), int)
        self.assertEqual(report["field_check"]["actual"], [True, 1.0])
        self.assertIs(type(report["field_check"]["actual"][0]), bool)
        self.assertIs(type(report["field_check"]["actual"][1]), float)
        self.assertTrue(report["field_check"]["present"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_acceptance_change_first_element_to_number_fails_and_keeps_actual(self) -> None:
        # 验收场景的第二步：仅把期望改为 [1,1]，首元素 true ≠ 1，
        # 字段失败且实际数组 [true,1.0] 完整保留
        report, completed = self._execute(
            expected_value=[1, 1],
            response_body=b'{"items":[true,1.0]}',
        )
        self.assertTrue(report["field_check"]["present"])
        self.assertEqual(report["field_check"]["expected"], [1, 1])
        self.assertEqual(report["field_check"]["actual"], [True, 1.0])
        self.assertIs(type(report["field_check"]["actual"][0]), bool)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_empty_array_only_matches_empty_array(self) -> None:
        for body, passed in [
            (b'{"items":[]}', True),
            (b'{"items":[null]}', False),
            (b'{"items":[0]}', False),
            (b'{"items":[[]]}', False),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=[], response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["expected"], [])
                self.assertIs(report["field_check"]["passed"], passed)
                self.assertEqual(report["error"], ASSERTION_FAILED if not passed else None)
                self.assertEqual(completed.returncode, 0 if passed else 1)

    def test_empty_array_does_not_match_null_or_scalar_actual(self) -> None:
        for body in (b'{"items":null}', b'{"items":false}', b'{"items":""}', b'{"items":{}}'):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=[], response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(completed.returncode, 1)

    def test_order_and_length_matter_without_sorting_or_dedup(self) -> None:
        # 不排序、不去重：顺序与长度（含重复元素个数）都参与比较
        cases = [
            ([1, 2], b'{"items":[2,1]}', False),  # 顺序不同
            ([1, 1], b'{"items":[1]}', False),  # 长度不同，重复不折叠
            ([1], b'{"items":[1,1]}', False),
            ([1, 2, 3], b'{"items":[1,2]}', False),
            ([1, 1], b'{"items":[1,1.0]}', True),  # 1 与 1.0 相等
            (["a", "a"], b'{"items":["a","a"]}', True),
            (["a", "b"], b'{"items":["b","a"]}', False),
        ]
        for expected, body, passed in cases:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertEqual(
                    report["field_check"]["passed"],
                    passed,
                    f"期望 {expected} 对实际 {body!r} 应判定 passed={passed}",
                )
                self.assertEqual(completed.returncode, 0 if passed else 1)

    def test_element_types_are_strict_position_by_position(self) -> None:
        # 逐位置沿用严格类型规则
        cases = [
            ([True], b'{"items":[1]}', False),
            ([False], b'{"items":[0]}', False),
            (["1"], b'{"items":[1]}', False),
            ([1], b'{"items":["1"]}', False),
            ([1], b'{"items":[true]}', False),
            ([None], b'{"items":[false]}', False),
            ([None], b'{"items":[0]}', False),
            ([None], b'{"items":["null"]}', False),
            ([None], b'{"items":[null]}', True),
            ([True, "x", None, 1.5], b'{"items":[true,"x",null,1.5]}', True),
            ([True, "x", None, 1.5], b'{"items":[true,"x",null,1]}', False),
            ([True, "x", None, 1], b'{"items":[1,"x",null,1.0]}', False),
        ]
        for expected, body, passed in cases:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertEqual(report["field_check"]["passed"], passed)
                self.assertEqual(completed.returncode, 0 if passed else 1)

    def test_array_expected_does_not_match_scalar_or_object_actual(self) -> None:
        # 数组只匹配数组；actual 原样保留（含 JSON 类型）
        for body, expected_actual in [
            (b'{"items":1}', 1),
            (b'{"items":"a,b"}', "a,b"),
            (b'{"items":true}', True),
            (b'{"items":null}', None),
            (b'{"items":{"0":1}}', {"0": 1}),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=[1], response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["actual"], expected_actual)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_scalar_expected_does_not_match_array_actual(self) -> None:
        # 反向同样严格：标量期望不匹配数组实际
        for expected, body in [
            (1, b'{"items":[1]}'),
            ("a", b'{"items":["a"]}'),
            (True, b'{"items":[true]}'),
            (None, b'{"items":[null]}'),
        ]:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["actual"], json.loads(body)["items"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(completed.returncode, 1)

    def test_missing_field_is_null_actual_with_array_expected(self) -> None:
        report, completed = self._execute(
            expected_value=[1], response_body=b'{"other":[1]}'
        )
        self.assertFalse(report["field_check"]["present"], "字段缺失时 present 为 false")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(report["field_check"]["expected"], [1])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_checks_array_field(self) -> None:
        # 状态码 500 ≠ 200，但数组字段相符：字段检查必须保持通过
        report, completed = self._execute(
            expected_value=[True, 1],
            response_status=500,
            response_body=b'{"items":[true,1.0]}',
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], [True, 1.0])
        self.assertTrue(report["field_check"]["passed"], "数组相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_big_integer_elements_keep_precision(self) -> None:
        # 大整数元素按任意精度保留、逐位比较，不截断或舍入
        big_int = int(BIG_INT_TEXT)
        body = f'{{"items":[{BIG_INT_TEXT}]}}'.encode("utf-8")
        report, completed = self._execute(expected_value=[big_int], response_body=body)
        self.assertEqual(report["field_check"]["expected"], [big_int])
        self.assertEqual(report["field_check"]["actual"], [big_int])
        self.assertIs(type(report["field_check"]["actual"][0]), int)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

        # 末位不同的大整数不匹配，actual 完整保留
        other = BIG_INT_TEXT[:-1] + "1"
        body_other = f'{{"items":[{other}]}}'.encode("utf-8")
        report, completed = self._execute(
            expected_value=[big_int], response_body=body_other
        )
        self.assertEqual(report["field_check"]["actual"], [int(other)])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)


class ArrayExpectedErrorClassificationTests(_ArrayFlowTestCase):
    """数组期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_non_object_body_is_invalid_response_with_array_expected(self) -> None:
        for body in (b"not json", b"[1, 2]", b"1", b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value=[1], response_body=body
                )
                self.assertEqual(report["field_check"]["expected"], [1])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertIsNone(report["field_check"]["present"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_connection_failure_is_request_failed_with_array_expected(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        case = {
            "name": "array",
            "url": f"http://127.0.0.1:{refused_port}/items",
            "expected_status": 200,
            "field": "items",
            "expected_value": [True, 1],
        }
        case_path = os.path.join(self.tmpdir.name, "refused.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(report["field_check"]["expected"], [True, 1])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.stderr, b"")


class ArrayExpectedValueLoadingTests(_ArrayFlowTestCase):
    """expected_value 的加载校验：标量一维数组合法，嵌套结构与非法数组拒绝。"""

    # 原始 JSON 文本，全部应被 load_case 拒绝
    # 空对象 {} 现在是合法的对象期望（见 test_object_field_assertion.py）
    INVALID_RAW_VALUES = [
        ("array_with_object", "[1,{}]"),
        ("array_with_nested_array", "[1,[2]]"),
        ("nested_empty_array", "[[]]"),
        ("array_with_nan", "[1,NaN]"),
        ("array_with_infinity", "[Infinity]"),
        ("array_with_negative_infinity", "[-Infinity]"),
        ("array_with_overflow", "[1e400]"),
        ("nan", "NaN"),
        ("overflow_scalar", "1e400"),
        ("object_with_nested_object", '{"a":{}}'),
        ("object_with_array", '{"a":[]}'),
        ("object_with_overflow", '{"a":1e400}'),
    ]

    def test_load_case_accepts_scalar_arrays(self) -> None:
        for raw_value, expected in [
            ("[]", []),
            ('["a"]', ["a"]),
            ("[true,false]", [True, False]),
            ("[null]", [None]),
            ('[true,1,"x",null,1.5]', [True, 1, "x", None, 1.5]),
            ("[-0.0,1e0]", [-0.0, 1.0]),
            (f"[{BIG_INT_TEXT}]", [int(BIG_INT_TEXT)]),
        ]:
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(loaded["expected_value"], expected)
                self.assertIsInstance(loaded["expected_value"], list)

    def test_load_case_rejects_invalid_expected_values(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                case_path = self._write_raw_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_load_case_rejects_missing_expected_value(self) -> None:
        case = {
            "name": "array",
            "url": f"http://127.0.0.1:{self.server.port}/items",
            "expected_status": 200,
            "field": "items",
        }
        case_path = os.path.join(self.tmpdir.name, "missing.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle)
        with self.assertRaises(CaseError) as context:
            load_case(case_path)
        self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_invalid_expected_values_without_sending_request(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                self.server.set_scenario(200, b'{"items":[]}')
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

    def test_existing_scalar_expected_values_still_load(self) -> None:
        # 既有标量断言不受影响
        for raw_value in ('"ok"', "true", "1", "1.5", "null"):
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(
                    loaded["expected_value"], json.loads(raw_value)
                )


if __name__ == "__main__":
    unittest.main()
