"""field_check.present（字段存在性）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证报告中新增的
``field_check.present``（JSON 布尔值或 null，不替代 passed）：

1. present 为 true：请求完成且正文通过严格 JSON 校验、顶层为对象，
   field 指定的顶层键存在。值为 null、false、0、空字符串、数组或
   对象都不影响存在判断；值的匹配仍遵循原有规则（null 期望只在
   键存在且值为 null 时通过，其余值类型不符仍判断言失败）；
2. present 为 false：同样的响应条件下顶层键缺失，actual 为 null，
   error 为 assertion_failed，退出码 1；field 按完整键名匹配，
   点号不表示嵌套路径（{"a":{"b":1}} 对字段 "a.b" 判缺失，
   {"a.b":1} 判存在）；
3. present 为 null：正文无效或顶层不是对象时维持 invalid_response
   并保留状态码检查结果；连接失败、等待超时或未收满声明长度就
   断开时维持 request_failed，两项 actual 为 null、所有 passed
   为 false。这两类失败仍退出 1；
4. 状态码不符时仍照常计算 present，两项检查各自保留结果；
5. 每次有效执行只发送一次 GET；stdout 只有一份 JSON 报告，
   stderr 为空；用例错误行为不变（CaseError、退出码 2）。

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
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_workbench.runner import (
    ASSERTION_FAILED,
    INVALID_RESPONSE,
    REQUEST_FAILED,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0


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


class _EarlyCloseServer:
    """声明的 Content-Length 大于实发字节数、随后正常关闭连接的服务。"""

    def __init__(self, declared_length: int, body: bytes) -> None:
        self._declared_length = declared_length
        self._body = body
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
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
                return
            self._connections.append(connection)
            threading.Thread(
                target=self._respond, args=(connection,), daemon=True
            ).start()

    def _respond(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(2)
            # 读完请求头（以空行结束）再响应
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {self._declared_length}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            connection.sendall(head + self._body)
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
        self._thread.join(timeout=2)
        for connection in self._connections:
            try:
                connection.close()
            except OSError:
                pass


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
        extra: dict | None = None,
        *,
        port: int | None = None,
        field: str = "value",
    ) -> str:
        """写 field=value、expected_status=200、expected_value=null 的用例。"""
        case = {
            "name": "field-present",
            "url": f"http://127.0.0.1:{port or self.server.port}/resource",
            "expected_status": 200,
            "field": field,
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
            timeout=COMMAND_HARD_LIMIT,
        )

    def _execute(
        self,
        *,
        response_status: int = 200,
        response_body: bytes = b'{"value":null}',
        field: str = "value",
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """配置服务响应、写用例、经公开入口执行并解析唯一报告。"""
        self.server.set_scenario(response_status, response_body)
        completed = self._run(self._write_case(field=field))

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


class PresentTrueTests(_PresentFlowTestCase):
    """键存在时 present 为 true，与值的内容和匹配结果无关。"""

    def test_null_value_present_and_passes(self) -> None:
        # 规格示例：期望 null，响应 {"value":null} → present true、整体通过
        report, completed = self._execute(response_body=b'{"value":null}')
        self.assertIs(report["field_check"]["present"], True)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_falsy_values_still_present(self) -> None:
        # false、0、""、[]、{} 都是"键存在"：present 为 true，
        # 但均不匹配 null 期望，字段检查仍失败
        for body in (
            b'{"value":false}',
            b'{"value":0}',
            b'{"value":""}',
            b'{"value":[]}',
            b'{"value":{}}',
        ):
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                self.assertIs(report["field_check"]["present"], True)
                self.assertIsNotNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["error"], ASSERTION_FAILED)
                self.assertEqual(completed.returncode, 1)

    def test_dotted_field_matches_literal_key(self) -> None:
        # 点号不表示嵌套路径：{"a.b":null} 对字段 "a.b" 判存在
        report, completed = self._execute(
            response_body=b'{"a.b":null}', field="a.b"
        )
        self.assertIs(report["field_check"]["present"], True)
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(completed.returncode, 0)

    def test_status_mismatch_still_computes_present(self) -> None:
        # 状态码 500 ≠ 期望 200：present 照常计算，两项检查各自保留结果
        report, completed = self._execute(
            response_status=500, response_body=b'{"value":null}'
        )
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIs(report["field_check"]["present"], True)
        self.assertTrue(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class PresentFalseTests(_PresentFlowTestCase):
    """键缺失时 present 为 false，actual 为 null，字段检查失败。"""

    def test_missing_key_present_false(self) -> None:
        # 规格示例：期望 null，响应 {} → present false、actual null、退出码 1
        report, completed = self._execute(response_body=b"{}")
        self.assertIs(report["field_check"]["present"], False)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertTrue(report["status_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1)

    def test_nested_object_does_not_satisfy_dotted_field(self) -> None:
        # {"a":{"b":null}} 不含顶层键 "a.b"：点号不展开为嵌套路径
        report, completed = self._execute(
            response_body=b'{"a":{"b":null}}', field="a.b"
        )
        self.assertIs(report["field_check"]["present"], False)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)

    def test_missing_key_with_status_mismatch(self) -> None:
        # 状态码不符且键缺失：present 仍为 false，两项检查各自失败
        report, completed = self._execute(response_status=500, response_body=b"{}")
        self.assertEqual(report["status_check"]["actual"], 500)
        self.assertFalse(report["status_check"]["passed"])
        self.assertIs(report["field_check"]["present"], False)
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertEqual(completed.returncode, 1)


class PresentNullTests(_PresentFlowTestCase):
    """响应无法用于字段检查时 present 为 null，既有错误分类不变。"""

    def test_invalid_or_non_object_body_present_null(self) -> None:
        # 非法 JSON、顶层数组、正文整体为 null：present 为 null，
        # 维持 invalid_response，状态码检查结果保留
        for body in (b"this is {not valid json", b'["value",null]', b"null"):
            with self.subTest(body=body):
                report, completed = self._execute(response_body=body)
                self.assertIsNone(report["field_check"]["present"])
                self.assertIsNone(report["field_check"]["actual"])
                self.assertFalse(report["field_check"]["passed"])
                self.assertEqual(report["status_check"]["actual"], 200)
                self.assertTrue(report["status_check"]["passed"])
                self.assertFalse(report["passed"])
                self.assertEqual(report["error"], INVALID_RESPONSE)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(self.server.get_count, 1)

    def test_connection_failure_present_null(self) -> None:
        # 绑定一个端口后立即关闭，制造稳定的连接拒绝
        refused_server = ThreadingHTTPServer(("127.0.0.1", 0), _ScenarioHandler)
        refused_port = refused_server.server_address[1]
        refused_server.server_close()

        completed = self._run(self._write_case(port=refused_port))
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["field_check"]["present"])
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_timeout_present_null(self) -> None:
        # 服务收到 GET 后挂起；用例把每次阻塞上限收窄到 0.1 秒以保持测试轻快
        hanging_server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
        hanging_server.daemon_threads = True
        thread = threading.Thread(target=hanging_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._shutdown_server_extra, hanging_server, thread)

        completed = self._run(
            self._write_case(
                extra={"timeout_seconds": 0.1},
                port=hanging_server.server_address[1],
            )
        )
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["field_check"]["present"])
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    def test_early_body_close_present_null(self) -> None:
        # 声明 16 字节实发 15 字节后断开：未收满声明长度，request_failed
        body = b'{"value":null}'
        early_close = _EarlyCloseServer(declared_length=len(body) + 1, body=body)
        early_close.start()
        self.addCleanup(early_close.close)

        completed = self._run(self._write_case(port=early_close.port))
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["field_check"]["present"])
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])

    @staticmethod
    def _shutdown_server_extra(server, thread) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
