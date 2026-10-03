"""显式 JSON null 期望（expected_value: null）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 写成 null 时允许加载（load_case 不抛错），省略该键仍属错误；
   数字、数组、对象及未加引号的 NaN/Infinity/-Infinity 继续被拒绝：
   不发送请求，stdout 为空，stderr 以 api_workbench: 开头并指出
   expected_value，退出码 2，无 Traceback；
2. null 期望只在键存在且值为 null 时通过：响应 200 + {"value":null} 整体通过；
   键缺失（{}）时 actual 为 null 但字段检查失败；状态码 500 + {"value":null}
   时字段通过、整体失败；
3. 字符串 "null"、空字符串、false、0、数组和对象均不匹配 null，
   失败时 actual 保留实际 JSON 值及类型，报告中的字段期望保留 null；
4. 非法 JSON、数组或正文整体为 null 仍返回 invalid_response，保留状态码
   检查结果，字段 actual 为 null 且检查失败；
5. 连接失败或超时仍返回 request_failed，两项 actual 为 null、所有
   passed 为 false，退出码 1；
6. 每次有效执行仅发送一次 GET。

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
import time
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


class _HangingHandler(BaseHTTPRequestHandler):
    """收到 GET 后迟迟不响应，用于触发客户端超时。"""

    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        time.sleep(5)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class _NullFlowTestCase(unittest.TestCase):
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

    def _write_case(self, extra: dict | None = None, *, port: int | None = None) -> str:
        """写 field=value、expected_status=200、expected_value=null 的用例。"""
        case = {
            "name": "null-value",
            "url": f"http://127.0.0.1:{port or self.server.port}/resource",
            "expected_status": 200,
            "field": "value",
            "expected_value": None,
        }
        if extra:
            case.update(extra)
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)
        return case_path

    def _run(self, case_path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _execute(
        self,
        *,
        response_status: int = 200,
        response_body: bytes = b'{"value":null}',
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写 null 期望用例、经公开入口执行。"""
        self.server.set_scenario(response_status, response_body)
        completed = self._run(self._write_case())

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


class NullExpectedMatchingTests(_NullFlowTestCase):
    """null 期望：只在键存在且值为 null 时通过，报告期望保留 null。"""

    def test_null_expected_matches_null_value(self) -> None:
        report, completed = self._execute(response_body=b'{"value":null}')
        self.assertIsNone(report["field_check"]["expected"], "报告中的字段期望保留 null")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_missing_key_fails_even_with_null_expected(self) -> None:
        # 键缺失时 actual 同样为 null，但字段检查必须失败
        report, completed = self._execute(response_body=b"{}")
        self.assertIsNone(report["field_check"]["expected"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_still_passes_null_field_check(self) -> None:
        # 状态码 500 ≠ 期望 200，但字段值为 null：字段检查通过、整体失败
        report, completed = self._execute(
            response_status=500, response_body=b'{"value":null}'
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertTrue(report["field_check"]["passed"], "字段值为 null 时字段检查仍通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_non_null_values_do_not_match_null_expected(self) -> None:
        # "null"、""、false、0、[]、{} 均不匹配 null；actual 保留原值与类型
        scenarios = [
            (b'{"value":"null"}', "null", str),
            (b'{"value":""}', "", str),
            (b'{"value":false}', False, bool),
            (b'{"value":0}', 0, int),
            (b'{"value":[]}', [], list),
            (b'{"value":{}}', {}, dict),
        ]
        for body, expected_actual, expected_type in scenarios:
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                actual = report["field_check"]["actual"]
                self.assertIs(
                    type(actual), expected_type, f"actual 必须保留 JSON 类型: {body!r}"
                )
                self.assertEqual(actual, expected_actual)
                self.assertIsNotNone(actual, f"actual 不得被吞成 null: {body!r}")
                self.assertIsNone(report["field_check"]["expected"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_zero_actual_is_not_confused_with_false_or_null(self) -> None:
        # 数字 0 原样保留且保持整数类型（同时排除被当作布尔 False 或 null）
        report, completed = self._execute(response_body=b'{"value":0}')
        actual = report["field_check"]["actual"]
        self.assertIs(type(actual), int)
        self.assertIsNot(actual, False)
        self.assertEqual(actual, 0)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 1)


class NullExpectedErrorClassificationTests(_NullFlowTestCase):
    """null 期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_invalid_or_non_object_body_is_invalid_response(self) -> None:
        # 非法 JSON、顶层数组、正文整体为 null：一律 invalid_response，
        # 状态码检查结果保留，字段 actual 为 null 且检查失败
        for body in (b"this is {not valid json", b'["value",null]', b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                self.assertIsNone(report["field_check"]["expected"])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["status_check"]["actual"], 200)
                self.assertTrue(
                    report["status_check"]["passed"], "收到的状态码检查结果必须保留"
                )
                self.assertFalse(report["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_invalid_body_with_mismatched_status_keeps_status_result(self) -> None:
        report, completed = self._execute(
            response_status=500, response_body=b"<<<still not json>>>"
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)

    def test_connection_failure_is_request_failed_with_null_expected(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        completed = self._run(self._write_case(port=refused_port))
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["expected"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_timeout_is_request_failed_with_null_expected(self) -> None:
        # 服务收到 GET 后挂起；用例把每次阻塞上限收窄到 0.1 秒以保持测试轻快
        hanging_server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
        hanging_server.daemon_threads = True
        thread = threading.Thread(target=hanging_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._shutdown_hanging, hanging_server, thread)

        completed = self._run(
            self._write_case(
                extra={"timeout_seconds": 0.1}, port=hanging_server.server_address[1]
            )
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    @staticmethod
    def _shutdown_hanging(server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class NullExpectedValueLoadingTests(_NullFlowTestCase):
    """加载阶段：null 允许、缺键拒绝，其余非法类型继续拒绝且不发请求。"""

    def _write_raw_case(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 null/NaN/[] 等类型。"""
        fields = {
            "name": "null-value",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "value",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "raw_case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def test_load_case_accepts_explicit_null(self) -> None:
        case_path = self._write_case()
        loaded = load_case(case_path)
        self.assertIn("expected_value", loaded)
        self.assertIsNone(loaded["expected_value"])

    def test_load_case_rejects_missing_key(self) -> None:
        case_path = self._write_raw_case(None)  # None 占位表示不写入该字段
        with self.assertRaises(CaseError) as context:
            load_case(case_path)
        self.assertIn("expected_value", str(context.exception))

    def test_load_case_rejects_other_invalid_types(self) -> None:
        # 数字、数组、对象及未加引号的 NaN/Infinity/-Infinity 继续被拒绝
        for raw_value in ("1", "1.5", "[]", "{}", "NaN", "Infinity", "-Infinity"):
            with self.subTest(raw_value=raw_value):
                case_path = self._write_raw_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_missing_key_without_sending_request(self) -> None:
        self.server.set_scenario(200, b'{"value":null}')
        case_path = self._write_raw_case(None)
        completed = self._run(case_path)
        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(completed.returncode, 2, stderr)
        self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
        self.assertTrue(
            stderr.startswith(DIAGNOSTIC_PREFIX),
            f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr!r}",
        )
        self.assertIn("expected_value", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.server.get_count, 0, "缺键时不得发送 GET")

    def test_cli_rejects_other_invalid_types_without_sending_request(self) -> None:
        for raw_value in ("1", "1.5", "[]", "{}", "NaN", "Infinity", "-Infinity"):
            with self.subTest(raw_value=raw_value):
                self.server.set_scenario(200, b'{"value":null}')
                case_path = self._write_raw_case(raw_value)
                completed = self._run(case_path)
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
