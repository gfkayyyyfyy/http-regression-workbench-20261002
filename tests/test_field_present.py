"""field_check.present 三种结果（true / false / null）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证本次新增的 present 字段：

1. present = true：请求完整结束、正文通过现有严格 JSON 校验且顶层为对象时，
   field 指定的顶层键存在即为 true——值为 null、false、0、空字符串、数组或
   对象都不影响存在判断；field 按完整键名匹配，点号不表示嵌套路径。
   典型场景：期望状态码 200、字段 value、期望值 null，响应 200
   {"value":null} 时 present 为 true、整体通过、退出码 0。
2. present = false：合法对象中键缺失。200 响应 {} 时 present 为 false、
   actual 为 null、error 为 assertion_failed、退出码 1。
3. present = null：正文无效或顶层不是对象（维持 invalid_response，保留
   状态码检查）；连接失败、等待超时或未收满声明长度就断开（维持
   request_failed，两项 actual 为 null、所有 passed 为 false）。

present 不替代 passed、不参与总判定：状态码不符时仍照常计算 present，
两项检查各自保留结果。两类失败仍退出 1，stdout 只有一份 JSON，stderr 为空；
用例错误仍不发送请求，stdout 为空、stderr 以 api_workbench: 开头、退出码 2。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，单次 GET、
不跟随重定向、沿用现有三秒超时（超时场景用例内收窄到 0.1 秒），
报告不落盘、不依赖固定 8765、不需要访问公网。
"""

from __future__ import annotations

import json
import os
import socket
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
    REQUEST_TIMEOUT,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DIAGNOSTIC_PREFIX = "api_workbench:"

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 提前断连场景发送的正文（合法 JSON，但声明的 Content-Length 多出 1 字节）
SHORT_BODY = b'{"value":null}'


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
    server_version = "api_workbench-present-test/0.1"

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

    server_version = "api_workbench-present-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        time.sleep(5)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class _ShortBodyServer:
    """声明长度比实发正文多 1 字节、发完即正常关闭连接的受控服务（原始套接字）。"""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_count = 0
        self._lock = threading.Lock()
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
            with self._lock:
                self._connections.append(connection)
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(0.2)
            request = b""
            while b"\r\n\r\n" not in request and len(request) < 65536:
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    if self._stopping.is_set():
                        return
                    continue
                if not chunk:
                    return
                request += chunk
            if not request.startswith(b"GET "):
                return
            with self._lock:
                self.get_count += 1
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(SHORT_BODY) + 1}\r\n".encode("ascii")
                + b"\r\n"
                + SHORT_BODY
            )
            # 发完实际正文后立即正常关闭：客户端读不满声明长度
        except OSError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=2)


class _PresentFlowTestCase(unittest.TestCase):
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

    def _write_case(
        self,
        *,
        port: int | None = None,
        field: str = "value",
        expected_value=None,
        expected_status: int = 200,
        extra: dict | None = None,
    ) -> str:
        """写默认 field=value、期望值 null、期望状态码 200 的用例。"""
        case = {
            "name": "field-present",
            "url": f"http://127.0.0.1:{port or self.server.port}/resource",
            "expected_status": expected_status,
            "field": field,
            "expected_value": expected_value,
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
            timeout=COMMAND_HARD_LIMIT,
        )

    def _execute(
        self, *, response_status: int = 200, response_body: bytes = b'{"value":null}'
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


class FieldPresentTrueTests(_PresentFlowTestCase):
    """present = true：合法对象中顶层键存在，值本身是什么不影响存在判断。"""

    def test_null_value_with_null_expected_passes_overall(self) -> None:
        # 规格点名场景：200 + {"value":null} → present true、整体通过、退出码 0
        report, completed = self._execute(response_body=b'{"value":null}')
        field_check = report["field_check"]
        self.assertIs(field_check["present"], True)
        self.assertIsNone(field_check["actual"])
        self.assertIsNone(field_check["expected"])
        self.assertTrue(field_check["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_present_true_for_null_false_zero_empty_string_array_and_object(self) -> None:
        # null、false、0、""、[]、{} 都不影响存在判断；present 一律 true。
        # 期望值统一为 null：除 null 外其余值的字段匹配仍按原规则失败，
        # 但 present 与 passed 互相独立，actual 保留原 JSON 值与类型。
        scenarios = [
            (b'{"value":null}', None, type(None), True),
            (b'{"value":false}', False, bool, False),
            (b'{"value":0}', 0, int, False),
            (b'{"value":""}', "", str, False),
            (b'{"value":[]}', [], list, False),
            (b'{"value":{}}', {}, dict, False),
        ]
        for body, value, value_type, field_passed in scenarios:
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                field_check = report["field_check"]
                self.assertIs(field_check["present"], True)
                self.assertIs(type(field_check["actual"]), value_type)
                self.assertEqual(field_check["actual"], value)
                self.assertIs(field_check["passed"], field_passed)
                self.assertTrue(report["status_check"]["passed"])
                self.assertEqual(completed.returncode, 0 if field_passed else 1)
                self.assertEqual(self.server.get_count, 1)

    def test_present_true_when_value_mismatches(self) -> None:
        # 键存在但值与期望不符：present 为 true，passed 仍为 false
        report, completed = self._execute(response_body=b'{"value":42}')
        field_check = report["field_check"]
        self.assertIs(field_check["present"], True)
        self.assertEqual(field_check["actual"], 42)
        self.assertFalse(field_check["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_dotted_field_is_matched_as_whole_key(self) -> None:
        # 点号不表示嵌套路径：field 按完整键名匹配
        present_case = self._write_case(field="a.b")
        self.server.set_scenario(200, b'{"a.b":null}')
        report, completed = self._run_present(present_case)
        self.assertIs(report["field_check"]["present"], True)
        self.assertTrue(report["field_check"]["passed"])
        self.assertEqual(completed.returncode, 0)

        # 仅存在嵌套对象 {"a":{"b":...}} 时，完整键名 "a.b" 缺失
        nested_case = self._write_case(field="a.b")
        self.server.set_scenario(200, b'{"a":{"b":null}}')
        report, completed = self._run_present(nested_case)
        self.assertIs(report["field_check"]["present"], False)
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def _run_present(self, case_path: str) -> tuple[dict, subprocess.CompletedProcess]:
        completed = self._run(case_path)
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        stdout_text = completed.stdout.decode("utf-8")
        report, end = json.JSONDecoder().raw_decode(stdout_text)
        self.assertEqual(stdout_text[end:].strip(), "")
        return report, completed


class FieldPresentFalseTests(_PresentFlowTestCase):
    """present = false：合法 JSON 对象中目标顶层键缺失。"""

    def test_missing_key_is_present_false_with_null_actual_and_exit_1(self) -> None:
        # 规格点名场景：200 + {} → present false、actual null、
        # error assertion_failed、退出码 1（即使期望值也是 null）
        report, completed = self._execute(response_body=b"{}")
        field_check = report["field_check"]
        self.assertIs(field_check["present"], False)
        self.assertIsNone(field_check["actual"])
        self.assertIsNone(field_check["expected"])
        self.assertFalse(field_check["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_missing_key_with_status_mismatch_keeps_both_checks(self) -> None:
        # 状态码 500 ≠ 期望 200 且键缺失：两项检查结果各自保留
        report, completed = self._execute(response_status=500, response_body=b"{}")
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        field_check = report["field_check"]
        self.assertIs(field_check["present"], False)
        self.assertIsNone(field_check["actual"])
        self.assertFalse(field_check["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_status_mismatch_with_present_key_keeps_both_checks(self) -> None:
        # 状态码不符但键存在且值匹配：状态检查失败、字段检查仍通过，
        # present 照常计算为 true
        report, completed = self._execute(
            response_status=500, response_body=b'{"value":null}'
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        field_check = report["field_check"]
        self.assertIs(field_check["present"], True)
        self.assertIsNone(field_check["actual"])
        self.assertTrue(field_check["passed"], "字段值为 null 且匹配期望时字段检查通过")
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class FieldPresentNullInvalidResponseTests(_PresentFlowTestCase):
    """present = null：正文无效或顶层不是对象，维持 invalid_response。"""

    def test_invalid_or_non_object_body_present_null(self) -> None:
        # 非法 JSON、顶层数组、正文整体为 null：present 一律为 null，
        # 状态码检查结果保留，字段 actual 为 null 且检查失败
        for body in (b"this is {not valid json", b'["value",null]', b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                field_check = report["field_check"]
                self.assertIsNone(field_check["present"])
                self.assertIsNone(field_check["actual"])
                self.assertFalse(field_check["passed"])
                self.assertEqual(report["status_check"]["actual"], 200)
                self.assertTrue(report["status_check"]["passed"])
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
        self.assertIsNone(report["field_check"]["present"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)


class FieldPresentNullRequestFailedTests(_PresentFlowTestCase):
    """present = null：连接失败、超时或未收满声明长度就断开。"""

    def _assert_request_failed_report(
        self, completed: subprocess.CompletedProcess
    ) -> dict:
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b"", "失败报告走 stdout，stderr 必须为空")
        self.assertNotIn(b"Traceback", completed.stderr)
        stdout_text = completed.stdout.decode("utf-8")
        report, end = json.JSONDecoder().raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        field_check = report["field_check"]
        self.assertIsNone(field_check["present"])
        self.assertIsNone(field_check["actual"])
        self.assertFalse(field_check["passed"])
        self.assertFalse(report["passed"])
        return report

    def test_connection_failure_present_null(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        completed = self._run(self._write_case(port=refused_port))
        report = self._assert_request_failed_report(completed)
        self.assertIsNone(report["field_check"]["expected"])

    def test_timeout_present_null(self) -> None:
        # 产品默认超时仍为三秒；用例内收窄到 0.1 秒保持测试轻快
        self.assertEqual(REQUEST_TIMEOUT, 3.0, "产品的请求超时设置必须保持为 3 秒")
        hanging_server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
        hanging_server.daemon_threads = True
        thread = threading.Thread(target=hanging_server.serve_forever, daemon=True)
        thread.start()
        try:
            completed = self._run(
                self._write_case(
                    port=hanging_server.server_address[1],
                    extra={"timeout_seconds": 0.1},
                )
            )
            self._assert_request_failed_report(completed)
        finally:
            hanging_server.shutdown()
            hanging_server.server_close()
            thread.join(timeout=2)

    def test_early_body_close_present_null(self) -> None:
        # 声明的 Content-Length 比实发正文多 1 字节，发完即断开：
        # 正文虽可解析为 {"value":null}，传输未满足声明，按 request_failed
        # 处理，present 为 null 而不是 true，且没有重试
        server = _ShortBodyServer()
        server.start()
        try:
            completed = self._run(self._write_case(port=server.port))
            self._assert_request_failed_report(completed)
            self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET，不得重试")
        finally:
            server.close()


class CaseErrorStillRejectedTests(_PresentFlowTestCase):
    """present 引入后用例错误路径不变：不发送请求、退出码 2、诊断前缀不变。"""

    def test_invalid_case_exits_2_without_request(self) -> None:
        # expected_status 写成字符串属于用例错误
        case_path = self._write_case(expected_status="200")  # type: ignore[arg-type]
        self.server.set_scenario(200, b'{"value":null}')
        completed = self._run(case_path)

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"", "用例错误时 stdout 必须为空")
        stderr = completed.stderr.decode("utf-8")
        self.assertTrue(
            stderr.startswith(DIAGNOSTIC_PREFIX),
            f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr!r}",
        )
        self.assertIn("expected_status", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.server.get_count, 0, "用例错误时不得发送 GET")


if __name__ == "__main__":
    unittest.main()
