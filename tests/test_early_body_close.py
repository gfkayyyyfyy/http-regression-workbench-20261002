"""响应正文提前结束的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证正文长度与声明不符、
连接随即正常关闭时的行为，两种场景发送的正文完全相同
（15 个 ASCII 字节 ``{"status":"ok"}``），差异仅在声明的 Content-Length：

1. 声明长度不足：Content-Length: 16，实发 15 字节后正常关闭连接。
   正文文本虽完整、可解析为 JSON，传输长度却未满足声明，
   结果应为退出码 1，error 为 request_failed（不归入
   invalid_response 或 assertion_failed），顶层及两项检查的
   passed 均为 false，两项 actual 均为 null，报告原样保留
   用例名称、期望状态码、字段名与期望值；
2. 对照场景：Content-Length: 15，实发 15 字节后同样关闭连接。
   结果应为退出码 0，error 为 null，两项 actual 分别为 200 和
   "ok"，顶层及两项检查均通过。

两种场景的 stdout 都只能包含一个可解析的 UTF-8 JSON 报告，
stderr 为空、不出现 Traceback；每次执行服务只收到一次 GET，
没有重试。单次命令超过十秒仍未结束即判失败并终止该进程。

服务使用系统分配的临时端口，用例写入临时文件，不占用示例服务的
固定 8765、不需要手工启动服务或访问公网；测试结束释放服务、
连接与临时文件，重复执行不留下占用端口的进程。
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

from api_workbench.runner import REQUEST_FAILED

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 两种场景发送的正文完全相同：15 个 ASCII 字节，可解析为合法 JSON
BODY = b'{"status":"ok"}'
assert len(BODY) == 15


class _ClosingServer:
    """发送固定响应后正常关闭连接的受控 127.0.0.1 服务（原始套接字）。

    收到 GET 后发送 200 响应头（Content-Length 取构造参数
    declared_length）与 BODY，随后正常关闭连接；监听保持到 close()，
    以便统计整个运行期间的 GET 次数（验证没有重试）。
    """

    def __init__(self, declared_length: int) -> None:
        self._declared_length = declared_length
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
        self._handlers: list[threading.Thread] = []
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
            handler = threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            )
            with self._lock:
                self._handlers.append(handler)
            handler.start()

    def _handle(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(0.2)
            request = b""
            # 先完整读取请求头，确认服务确实收到了 GET
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
                + f"Content-Length: {self._declared_length}\r\n".encode("ascii")
                + b"\r\n"
                + BODY
            )
            # 发送完毕后正常关闭连接：声明长度不足时客户端读到的是
            # 提前结束的正文，声明长度吻合时则是一次完整响应
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
            handlers = list(self._handlers)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=2)
        for handler in handlers:
            handler.join(timeout=2)


class _EarlyCloseFlowTestCase(unittest.TestCase):
    """公共设施：临时端口服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(
        self, *, declared_length: int, name: str
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """按声明长度启动服务、写用例、经公开入口执行，返回 (报告, 进程结果)。"""
        server = _ClosingServer(declared_length)
        server.start()
        self.addCleanup(server.close)
        self.server = server

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{server.port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case_path = os.path.join(tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        try:
            completed = subprocess.run(
                [sys.executable, "-m", "api_workbench", "run", case_path],
                cwd=PROJECT_ROOT,
                capture_output=True,
                timeout=COMMAND_HARD_LIMIT,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run 超时前已终止该子进程
            self.fail(f"单次命令超过 {COMMAND_HARD_LIMIT} 秒仍未结束")

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed


class ShortBodyTests(_EarlyCloseFlowTestCase):
    """声明长度不足：Content-Length: 16，实发 15 字节后正常关闭连接。"""

    def test_short_body_is_request_failed(self) -> None:
        report, completed = self._execute(
            declared_length=16, name="early-close-short-body"
        )

        # 正文文本虽完整可解析，传输长度未满足声明，整体按请求失败处理
        expected = {
            "name": "early-close-short-body",
            "passed": False,
            "error": REQUEST_FAILED,
            "status_check": {
                "expected": 200,
                "actual": None,
                "passed": False,
            },
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": None,
                "passed": False,
            },
        }
        self.assertEqual(report, expected)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], "request_failed")
        self.assertNotIn(report["error"], ("invalid_response", "assertion_failed"))
        # 报告原样保留用例名称、字段名称及期望值
        self.assertEqual(report["name"], "early-close-short-body")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )


class ExactLengthTests(_EarlyCloseFlowTestCase):
    """对照场景：Content-Length: 15，实发 15 字节后同样关闭连接。"""

    def test_exact_length_body_passes(self) -> None:
        report, completed = self._execute(
            declared_length=15, name="early-close-exact-length"
        )

        expected = {
            "name": "early-close-exact-length",
            "passed": True,
            "error": None,
            "status_check": {
                "expected": 200,
                "actual": 200,
                "passed": True,
            },
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": "ok",
                "passed": True,
            },
        }
        self.assertEqual(report, expected)
        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(report["error"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )


if __name__ == "__main__":
    unittest.main()
