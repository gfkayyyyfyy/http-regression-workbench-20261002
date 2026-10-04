"""对象字段完整相等断言（expected_value 为 JSON 对象）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 除既有标量与标量一维数组外接受对象（含空对象），
   每个成员值只允许字符串、布尔值、有限 JSON 数字或 null，成员键不限制、
   顺序无关；实际字段必须是对象、键集合完全相同且每个成员值符合既有
   严格标量比较规则才通过：true 不匹配 1，字符串不转数字，1 与 1.0 相等，
   null 只匹配 null，空对象只匹配空对象；多键、少键、值不同或实际类型
   不符均判 assertion_failed；
2. 验收场景：服务返回 200 与 {"meta":{"limit":1.0,"enabled":true}}，
   field=meta、expected_value={"enabled":true,"limit":1} 时整体通过、
   退出码 0；仅把期望的 enabled 改为 1 再执行，字段失败且实际对象完整
   保留、退出码 1；
3. field 仍按完整顶层键名查找（点号不表示路径）；对象成员键也按原文比较；
   报告结构不变，field_check.expected/actual 保留完整对象 JSON 结构及
   各成员的类型；字段缺失时 present 为 false、actual 为 null；
4. expected_value 含嵌套结构（数组成员或嵌套对象成员）或非有限数字成员
   （NaN、Infinity、-Infinity、1e400）时，load_case 抛出 CaseError：
   不发送请求，stdout 为空，stderr 以 api_workbench: 开头并指出
   expected_value，退出码 2，无 Traceback；字符串形式（"NaN" 等）合法；
5. 对象期望不改变既有分类：非 JSON 对象的响应仍为 invalid_response
   （保留状态码检查结果），连接失败仍为 request_failed，退出码 1；
6. 状态码不符时仍检查字段；每次有效执行仅发送一次 GET；
   大整数成员按任意精度保留并逐位比较。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765/8766、不需要手工启动服务或访问公网。
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


class _ObjectFlowTestCase(unittest.TestCase):
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
            "name": "object",
            "url": f"http://127.0.0.1:{self.server.port}/object",
            "expected_status": 200,
            "field": "meta",
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
        field: str = "meta",
        response_status: int = 200,
        response_body: bytes = b'{"meta":{}}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例（name=object，默认 field=meta）、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": "object",
            "url": f"http://127.0.0.1:{self.server.port}/object",
            "expected_status": 200,
            "field": field,
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


class ObjectExpectedValueMatchingTests(_ObjectFlowTestCase):
    """对象期望：键集合全等、键序无关、逐成员严格类型匹配，报告保留对象结构。"""

    def test_acceptance_meta_object_passes(self) -> None:
        # 验收场景：GET /object 返回 200 {"meta":{"limit":1.0,"enabled":true}}，
        # expected_value 为 {"enabled":true,"limit":1}（键序不同）：
        # true 匹配 true、1 匹配 1.0
        expected = {"enabled": True, "limit": 1}
        report, completed = self._execute(
            expected_value=expected,
            response_body=b'{"meta":{"limit":1.0,"enabled":true}}',
        )
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "meta")
        self.assertEqual(report["field_check"]["expected"], expected)
        self.assertIs(type(report["field_check"]["expected"]["enabled"]), bool)
        self.assertIs(type(report["field_check"]["expected"]["limit"]), int)
        self.assertEqual(
            report["field_check"]["actual"], {"limit": 1.0, "enabled": True}
        )
        self.assertIs(type(report["field_check"]["actual"]["limit"]), float)
        self.assertIs(type(report["field_check"]["actual"]["enabled"]), bool)
        self.assertTrue(report["field_check"]["present"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_acceptance_change_enabled_to_number_fails_and_keeps_actual(self) -> None:
        # 验收场景第二步：仅把期望的 enabled 改为 1，true ≠ 1，
        # 字段失败且实际对象 {"limit":1.0,"enabled":true} 完整保留
        report, completed = self._execute(
            expected_value={"enabled": 1, "limit": 1},
            response_body=b'{"meta":{"limit":1.0,"enabled":true}}',
        )
        self.assertTrue(report["field_check"]["present"])
        self.assertEqual(
            report["field_check"]["expected"], {"enabled": 1, "limit": 1}
        )
        self.assertEqual(
            report["field_check"]["actual"], {"limit": 1.0, "enabled": True}
        )
        self.assertIs(type(report["field_check"]["actual"]["enabled"]), bool)
        self.assertIs(type(report["field_check"]["actual"]["limit"]), float)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_empty_object_only_matches_empty_object(self) -> None:
        for body, passed in [
            (b'{"meta":{}}', True),
            (b'{"meta":{"x":null}}', False),
            (b'{"meta":{"a":1}}', False),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value={}, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["expected"], {})
                self.assertIs(report["field_check"]["passed"], passed)
                self.assertEqual(report["error"], ASSERTION_FAILED if not passed else None)
                self.assertEqual(completed.returncode, 0 if passed else 1)

    def test_empty_object_does_not_match_other_actual_types(self) -> None:
        for body in (
            b'{"meta":null}',
            b'{"meta":false}',
            b'{"meta":""}',
            b'{"meta":[]}',
            b'{"meta":0}',
        ):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value={}, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(completed.returncode, 1)

    def test_key_order_does_not_matter(self) -> None:
        # 键顺序无关：成员书写顺序不同但键集合与各值相同即通过
        report, completed = self._execute(
            expected_value={"a": 1, "b": "x", "c": True, "d": None},
            response_body=b'{"meta":{"d":null,"c":true,"b":"x","a":1.0}}',
        )
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

    def test_extra_and_missing_keys_fail(self) -> None:
        cases = [
            # 期望键集合必须与实际完全相同：多键、少键都失败
            ({"a": 1}, b'{"meta":{"a":1,"b":2}}'),  # 实际多键
            ({"a": 1, "b": 2}, b'{"meta":{"a":1}}'),  # 实际少键
            ({"a": 1}, b'{"meta":{"b":1}}'),  # 完全不同的键
            ({"a": 1, "b": 2}, b'{"meta":{"a":1,"b":2,"c":3}}'),
        ]
        for expected, body in cases:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(
                    report["field_check"]["actual"], json.loads(body)["meta"]
                )
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_member_value_types_are_strict(self) -> None:
        # 每个成员值沿用既有严格标量规则
        cases = [
            ({"enabled": True}, b'{"meta":{"enabled":1}}', False),
            ({"enabled": False}, b'{"meta":{"enabled":0}}', False),
            ({"enabled": 1}, b'{"meta":{"enabled":true}}', False),
            ({"v": "1"}, b'{"meta":{"v":1}}', False),
            ({"v": 1}, b'{"meta":{"v":"1"}}', False),
            ({"v": None}, b'{"meta":{"v":false}}', False),
            ({"v": None}, b'{"meta":{"v":0}}', False),
            ({"v": None}, b'{"meta":{"v":"null"}}', False),
            ({"v": None}, b'{"meta":{"v":null}}', True),
            ({"v": 1}, b'{"meta":{"v":1.0}}', True),
            ({"v": -0.0}, b'{"meta":{"v":0}}', True),
            ({"v": "Infinity"}, b'{"meta":{"v":"Infinity"}}', True),
            (
                {"b": True, "s": "x", "n": None, "i": 1},
                b'{"meta":{"b":true,"s":"x","n":null,"i":1.0}}',
                True,
            ),
            (
                {"b": True, "s": "x", "n": None, "i": 1},
                b'{"meta":{"b":true,"s":"x","n":null,"i":2}}',
                False,
            ),
        ]
        for expected, body, passed in cases:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertEqual(report["field_check"]["passed"], passed)
                self.assertEqual(completed.returncode, 0 if passed else 1)

    def test_member_keys_are_literal_no_dot_path(self) -> None:
        # 对象成员键按原文比较；顶层 field 的点号也不表示路径
        report, completed = self._execute(
            expected_value={"b": 1},
            field="a.b",
            response_body=b'{"a":{"b":{"b":1}},"a.b":{"b":1.0}}',
        )
        self.assertTrue(report["field_check"]["present"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

        # 点号键不存在时不深入嵌套对象查找
        report, completed = self._execute(
            expected_value={"b": 1},
            field="a.b",
            response_body=b'{"a":{"b":{"b":1}}}',
        )
        self.assertFalse(report["field_check"]["present"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_nested_or_array_member_in_actual_fails_but_is_kept(self) -> None:
        # 期望值只可能含标量成员；实际值的同名成员为数组/嵌套对象时类型不符，
        # 失败但实际对象完整保留
        for body, expected_actual in [
            (b'{"meta":{"v":[1]}}', {"v": [1]}),
            (b'{"meta":{"v":{"w":1}}}', {"v": {"w": 1}}),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value={"v": 1}, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["actual"], expected_actual)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_object_expected_does_not_match_scalar_or_array_actual(self) -> None:
        # 对象只匹配对象；actual 原样保留（含 JSON 类型）
        for body, expected_actual in [
            (b'{"meta":1}', 1),
            (b'{"meta":"x"}', "x"),
            (b'{"meta":true}', True),
            (b'{"meta":null}', None),
            (b'{"meta":[]}', []),
            (b'{"meta":[1]}', [1]),
        ]:
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value={"v": 1}, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(report["field_check"]["actual"], expected_actual)
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_scalar_expected_does_not_match_object_actual(self) -> None:
        # 反向同样严格：标量/数组期望不匹配对象实际
        for expected, body in [
            (1, b'{"meta":{"v":1}}'),
            ("a", b'{"meta":{"v":"a"}}'),
            (True, b'{"meta":{"v":true}}'),
            (None, b'{"meta":{}}'),
            ([1], b'{"meta":{}}'),
        ]:
            with self.subTest(expected=expected, body=body):
                report, completed = self._execute(
                    expected_value=expected, response_body=body
                )
                self.assertTrue(report["field_check"]["present"])
                self.assertEqual(
                    report["field_check"]["actual"], json.loads(body)["meta"]
                )
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(completed.returncode, 1)

    def test_missing_field_is_null_actual_with_object_expected(self) -> None:
        report, completed = self._execute(
            expected_value={"a": 1}, response_body=b'{"other":{"a":1}}'
        )
        self.assertFalse(report["field_check"]["present"], "字段缺失时 present 为 false")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertEqual(report["field_check"]["expected"], {"a": 1})
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_empty_object_expected_with_missing_field_fails(self) -> None:
        # 顶层键缺失时即使期望为空对象也判失败：actual 为 null 而非 {}
        report, completed = self._execute(
            expected_value={}, response_body=b'{"other":{}}'
        )
        self.assertFalse(report["field_check"]["present"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_checks_object_field(self) -> None:
        # 状态码 500 ≠ 200，但对象字段相符：字段检查必须保持通过
        report, completed = self._execute(
            expected_value={"enabled": True, "limit": 1},
            response_status=500,
            response_body=b'{"meta":{"limit":1.0,"enabled":true}}',
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(
            report["field_check"]["actual"], {"limit": 1.0, "enabled": True}
        )
        self.assertTrue(report["field_check"]["passed"], "对象相符时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_big_integer_members_keep_precision(self) -> None:
        # 大整数成员按任意精度保留、逐位比较，不截断或舍入
        big_int = int(BIG_INT_TEXT)
        body = f'{{"meta":{{"n":{BIG_INT_TEXT}}}}}'.encode("utf-8")
        report, completed = self._execute(expected_value={"n": big_int}, response_body=body)
        self.assertEqual(report["field_check"]["expected"], {"n": big_int})
        self.assertEqual(report["field_check"]["actual"], {"n": big_int})
        self.assertIs(type(report["field_check"]["actual"]["n"]), int)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

        # 末位不同的大整数不匹配，actual 完整保留
        other = BIG_INT_TEXT[:-1] + "1"
        body_other = f'{{"meta":{{"n":{other}}}}}'.encode("utf-8")
        report, completed = self._execute(
            expected_value={"n": big_int}, response_body=body_other
        )
        self.assertEqual(report["field_check"]["actual"], {"n": int(other)})
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)


class ObjectExpectedErrorClassificationTests(_ObjectFlowTestCase):
    """对象期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_non_object_body_is_invalid_response_with_object_expected(self) -> None:
        for body in (b"not json", b"[1, 2]", b"1", b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(
                    expected_value={"a": 1}, response_body=body
                )
                self.assertEqual(report["field_check"]["expected"], {"a": 1})
                self.assertIsNone(report["field_check"]["actual"])
                self.assertIsNone(report["field_check"]["present"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_connection_failure_is_request_failed_with_object_expected(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        case = {
            "name": "object",
            "url": f"http://127.0.0.1:{refused_port}/object",
            "expected_status": 200,
            "field": "meta",
            "expected_value": {"enabled": True, "limit": 1},
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
        self.assertEqual(
            report["field_check"]["expected"], {"enabled": True, "limit": 1}
        )
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.stderr, b"")


class ObjectExpectedValueLoadingTests(_ObjectFlowTestCase):
    """expected_value 的加载校验：标量成员对象合法，嵌套结构与非有限成员拒绝。"""

    # 原始 JSON 文本，全部应被 load_case 拒绝
    INVALID_RAW_VALUES = [
        ("nested_object", '{"a":{"b":1}}'),
        ("nested_empty_object", '{"a":{}}'),
        ("array_member", '{"a":[1]}'),
        ("empty_array_member", '{"a":[]}'),
        ("deeply_nested", '{"a":{"b":{"c":1}}}'),
        ("member_nan", '{"x":NaN}'),
        ("member_infinity", '{"x":Infinity}'),
        ("member_negative_infinity", '{"x":-Infinity}'),
        ("member_overflow", '{"x":1e400}'),
        ("member_negative_overflow", '{"x":-1E+400}'),
        ("second_member_nested", '{"a":1,"b":[2]}'),
        ("nan", "NaN"),
        ("overflow_scalar", "1e400"),
        ("array_with_object", "[{}]"),
    ]

    def test_load_case_accepts_scalar_member_objects(self) -> None:
        for raw_value, expected in [
            ("{}", {}),
            ('{"a":"x"}', {"a": "x"}),
            ('{"a":true,"b":false}', {"a": True, "b": False}),
            ('{"a":null}', {"a": None}),
            ('{"b":true,"i":1,"s":"x","n":null,"f":1.5}',
             {"b": True, "i": 1, "s": "x", "n": None, "f": 1.5}),
            ('{"z":-0.0,"a":1e0}', {"z": -0.0, "a": 1.0}),
            ("{ \"a.b\" : 1 }", {"a.b": 1}),
            ('{"s":"NaN","inf":"Infinity"}', {"s": "NaN", "inf": "Infinity"}),
            (f'{{"big":{BIG_INT_TEXT}}}', {"big": int(BIG_INT_TEXT)}),
        ]:
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(loaded["expected_value"], expected)
                self.assertIsInstance(loaded["expected_value"], dict)

    def test_load_case_rejects_invalid_expected_values(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                case_path = self._write_raw_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_load_case_rejects_missing_expected_value(self) -> None:
        case = {
            "name": "object",
            "url": f"http://127.0.0.1:{self.server.port}/object",
            "expected_status": 200,
            "field": "meta",
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
                self.server.set_scenario(200, b'{"meta":{}}')
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

    def test_existing_scalar_and_array_expected_values_still_load(self) -> None:
        # 既有标量与数组断言不受影响
        for raw_value in ('"ok"', "true", "1", "1.5", "null", "[]", '[1,true]'):
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                loaded = load_case(case_path)
                self.assertEqual(
                    loaded["expected_value"], json.loads(raw_value)
                )


if __name__ == "__main__":
    unittest.main()
