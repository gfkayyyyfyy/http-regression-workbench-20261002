"""显式 JSON null 字段期望的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. expected_value 写成 JSON null 时允许加载：仅当响应对象中存在该键
   且值为 null 时字段检查通过；键缺失时 actual 为 null 但字段检查失败；
2. null 期望严格匹配：字符串 "null"、空字符串、false、0、数组、对象
   均不匹配 null，失败时 actual 保留实际 JSON 值及类型，报告中的
   字段期望保留 null；
3. 两项检查均通过时整体 passed 为 true、error 为 null、退出码 0；
   任一断言失败时 passed 为 false、error 为 assertion_failed、退出码 1；
   状态码不符仍执行字段检查；
4. expected_value 键缺失，或为数字、数组、对象及未加引号的
   NaN/Infinity/-Infinity 时 load_case 抛出 CaseError：不发送请求，
   stdout 为空，stderr 以 api_workbench: 开头并指出 expected_value，
   退出码 2，无 Traceback；
5. null 期望不改变既有分类：非法 JSON、数组或正文整体为 null 的响应仍为
   invalid_response（保留状态码检查结果，字段 actual 为 null 且检查失败），
   连接失败或超时仍为 request_failed（两项 actual 为 null、所有 passed
   为 false），两类错误均以退出码 1 返回；
6. 每次有效执行仅发送一次 GET。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765、不需要手工启动服务或访问公网。
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


class _NullFlowTestCase(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与单报告解析。

    用例固定为 field=value、expected_status=200、expected_value=null。
    """

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

    def _write_case(self, **overrides) -> str:
        case = {
            "name": "null",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "value",
            "expected_value": None,
        }
        case.update(overrides)
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
        """配置服务响应、写 null 期望用例、经公开入口执行并解析唯一报告。"""
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


class NullExpectedValueMatchingTests(_NullFlowTestCase):
    """null 期望：仅匹配实际 JSON null，报告字段期望保留 null。"""

    def test_null_expected_matches_null_actual(self) -> None:
        report, completed = self._execute(response_body=b'{"value":null}')
        self.assertIsNone(report["field_check"]["expected"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_missing_field_is_null_actual_but_field_check_fails(self) -> None:
        # 键缺失：actual 同为 null，但字段检查必须失败
        report, completed = self._execute(response_body=b"{}")
        self.assertIsNone(report["field_check"]["expected"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_status_mismatch_still_checks_null_field(self) -> None:
        # 状态码 500 ≠ 200，但字段值为 null：字段检查必须保持通过
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

    def test_non_null_actuals_are_preserved_and_fail(self) -> None:
        # "null"、""、false、0、数组、对象均不匹配 null；actual 保留原值与类型
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
                    type(actual), expected_type, f"actual 应保留 JSON 类型: {body!r}"
                )
                self.assertEqual(actual, expected_actual)
                self.assertIsNone(report["field_check"]["expected"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertFalse(report["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)


class NullExpectedErrorClassificationTests(_NullFlowTestCase):
    """null 期望下既有错误分类不变：invalid_response 与 request_failed。"""

    def test_invalid_bodies_are_invalid_response_with_null_expected(self) -> None:
        # 非法 JSON、数组、正文整体为 null：字段 actual 为 null 且检查失败，
        # 状态码检查结果仍保留
        for body in (b"not json", b"[1, 2]", b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                self.assertIsNone(report["field_check"]["expected"])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["status_check"]["actual"], 200)
                self.assertTrue(report["status_check"]["passed"])
                self.assertFalse(report["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_invalid_body_with_status_mismatch_keeps_status_result(self) -> None:
        # 状态码不符且正文非法：invalid_response，状态码检查如实记录失败
        report, completed = self._execute(
            response_status=500, response_body=b"not json"
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

        case_path = self._write_case(
            url=f"http://127.0.0.1:{refused_port}/resource"
        )
        completed = self._run(case_path)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["expected"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_timeout_is_request_failed_with_null_expected(self) -> None:
        # 服务收到 GET 后挂起不响应；用例缩短等待上限以免拖慢测试
        hanging = _HangingServer()
        hanging.start()
        self.addCleanup(hanging.stop)

        case_path = self._write_case(
            url=f"http://127.0.0.1:{hanging.port}/resource",
            timeout_seconds=0.2,
        )
        completed = self._run(case_path)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(hanging.get_count, 1, "超时后不应重试")


class _HangingServer:
    """收到 GET 后保持连接、不发送任何响应字节的受控服务（原始套接字）。"""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_count = 0
        self._stopping = threading.Event()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._connections.append(connection)
            self.get_count += 1
            # 不读取、不回复，保持连接直到客户端超时或测试结束

    def stop(self) -> None:
        self._stopping.set()
        for connection in self._connections:
            try:
                connection.close()
            except OSError:
                pass
        self._listener.close()
        self._thread.join(timeout=2)


class NullExpectedValueValidationTests(unittest.TestCase):
    """expected_value 接受显式 null；缺键与其他非法类型仍被拒绝。"""

    # 原始 JSON 文本 → 仍属非法的 expected_value（None 占位表示不写入该键）
    INVALID_RAW_VALUES = [
        ("missing", None),
        ("integer", "1"),
        ("float", "1.5"),
        ("array", "[]"),
        ("object", "{}"),
        ("nan", "NaN"),
        ("infinity", "Infinity"),
        ("negative_infinity", "-Infinity"),
    ]

    def setUp(self) -> None:
        # 本组通过 get_count 断言非法用例不发送请求，因此仍启动受控服务
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.server = _ScenarioServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_case(self, raw_expected_value: str | None) -> str:
        """按原始 JSON 文本写 expected_value，精确保留 null/NaN/[] 等类型。"""
        fields = {
            "name": "null",
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "value",
        }
        lines = [f'  "{key}": {json.dumps(value)}' for key, value in fields.items()]
        if raw_expected_value is not None:
            lines.append(f'  "expected_value": {raw_expected_value}')
        path = os.path.join(self.tmpdir.name, "case.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{\n" + ",\n".join(lines) + "\n}\n")
        return path

    def test_load_case_accepts_explicit_null(self) -> None:
        case_path = self._write_case("null")
        loaded = load_case(case_path)
        self.assertIn("expected_value", loaded)
        self.assertIsNone(loaded["expected_value"])

    def test_load_case_raises_case_error_for_invalid_values(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                case_path = self._write_case(raw_value)
                with self.assertRaises(CaseError) as context:
                    load_case(case_path)
                self.assertIn("expected_value", str(context.exception))

    def test_cli_rejects_invalid_values_without_sending_request(self) -> None:
        for label, raw_value in self.INVALID_RAW_VALUES:
            with self.subTest(label=label):
                self.server.set_scenario(200, b'{"value":null}')
                case_path = self._write_case(raw_value)
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
