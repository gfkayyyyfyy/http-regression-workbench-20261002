"""响应正文提前结束（声明长度大于实发长度）的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证仅差一个字节的两个场景：

1. 提前结束：服务收到 GET 后返回 200 响应头，声明
   ``Content-Length: 16``，却只发送 15 个 ASCII 字节的正文
   ``{"status":"ok"}``，随后正常关闭连接。正文文本本身完整、
   可解析为 JSON，但传输长度未满足声明，http.client 抛出
   IncompleteRead：退出码为 1，error 为 request_failed（提前
   结束属于请求失败，既不是 invalid_response 也不是
   assertion_failed），顶层与两项检查的 passed 均为 false，
   两项 actual 均为 null，报告保留用例名称、字段名与期望值；
2. 对照：发送完全相同的 15 字节正文，仅把 Content-Length
   改为 15，发送完毕后同样关闭连接：退出码为 0，error 为
   null，两项 actual 分别为 200 与 "ok"，顶层及两项检查均通过。

两种场景的差异仅在声明长度。stdout 都只能包含一个可解析的
UTF-8 JSON 报告，stderr 为空且不出现 Traceback；服务每次执行
只收到一个 GET，没有重试。服务使用系统分配的临时端口，用例
写入临时文件，不依赖固定 8765、不需要手工启动服务或访问公网；
单次命令超过十秒即判测试失败并终止该进程，测试结束释放服务、
连接与临时文件，重复执行不留下占用端口的进程或临时文件。
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

from api_workbench.runner import REQUEST_FAILED

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 两个场景发送的正文完全相同：文本完整、本身就是合法 JSON，
# 但只有 15 个 ASCII 字节；差异只在响应头声明的 Content-Length。
BODY = b'{"status":"ok"}'

CASE_NAME_TRUNCATED = "body-early-close-truncated"
CASE_NAME_COMPLETE = "body-early-close-complete"


class _EarlyCloseServer:
    """收到 GET 后发送固定响应头与 BODY 再正常关闭连接的受控服务（原始套接字）。

    declared_length 为响应头中声明的 Content-Length；实发正文恒为
    BODY（15 字节）。declared_length=16 复现正文提前结束，
    declared_length=15 为长度吻合的对照场景。服务只服务一个请求即
    关闭该连接，并记录收到的 GET 次数以证明没有重试。
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
            # 先完整读取请求头，确认服务确实收到了 GET，
            # 避免把连接失败误当成正文提前结束
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

            # 两个场景响应头与正文完全一致，唯一差异是声明长度
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {self._declared_length}\r\n".encode("ascii")
                + b"Connection: close\r\n"
                b"\r\n" + BODY
            )
            # 正文已全部发出：半关闭发送方向，发出 FIN 正常结束连接，
            # 不重置、不挂起，让客户端读到明确的提前 EOF
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
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


def _request_failed_report(
    *, name: str, field: str, expected_value: str, expected_status: int
) -> dict:
    """按 runner 的 request_failed 报告结构构造期望报告（整体内容比较）。"""
    return {
        "name": name,
        "passed": False,
        "error": REQUEST_FAILED,
        "status_check": {
            "expected": expected_status,
            "actual": None,
            "passed": False,
        },
        "field_check": {
            "field": field,
            "expected": expected_value,
            "actual": None,
            "passed": False,
        },
    }


def _success_report(
    *, name: str, field: str, expected_value: str, expected_status: int
) -> dict:
    """对照场景的全部通过报告（error 为 null）。"""
    return {
        "name": name,
        "passed": True,
        "error": None,
        "status_check": {
            "expected": expected_status,
            "actual": expected_status,
            "passed": True,
        },
        "field_check": {
            "field": field,
            "expected": expected_value,
            "actual": expected_value,
            "passed": True,
        },
    }


class ResponseBodyEarlyCloseTests(unittest.TestCase):
    """正文提前结束与长度吻合对照：差异仅在 Content-Length 声明。"""

    def setUp(self) -> None:
        # 正文文本完整：15 个 ASCII 字节且本身可解析为目标 JSON
        self.assertEqual(len(BODY), 15)
        self.assertEqual(json.loads(BODY.decode("ascii")), {"status": "ok"})
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _execute(self, *, declared_length: int, name: str) -> tuple[dict, subprocess.CompletedProcess, _EarlyCloseServer]:
        """按声明长度启动服务、写用例、经公开入口执行，返回 (报告, 进程结果, 服务)。"""
        server = _EarlyCloseServer(declared_length)
        server.start()
        self.addCleanup(server.close)

        case = {
            "name": name,
            "url": f"http://127.0.0.1:{server.port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
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
        return report, completed, server

    def test_declared_16_sent_15_body_is_request_failed(self) -> None:
        """声明 16 字节实发 15 字节后正常关闭：提前结束即请求失败。"""
        started = time.monotonic()
        report, completed, server = self._execute(
            declared_length=16, name=CASE_NAME_TRUNCATED
        )
        elapsed = time.monotonic() - started

        expected = _request_failed_report(
            name=CASE_NAME_TRUNCATED,
            field="status",
            expected_value="ok",
            expected_status=200,
        )
        self.assertEqual(report, expected)
        # 正文虽可解析为 JSON，也不得改判 invalid_response 或继续字段断言
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertNotIn(report["error"], ("invalid_response", "assertion_failed"))
        self.assertEqual(report["name"], CASE_NAME_TRUNCATED)
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，提前结束后不得重试"
        )
        # 提前结束应当随服务关闭立即判定，而不是耗尽三秒读超时
        self.assertLess(elapsed, COMMAND_HARD_LIMIT)

    def test_declared_15_sent_15_same_body_passes(self) -> None:
        """同一正文声明 15 字节实发 15 字节：长度吻合，两项检查均通过。"""
        report, completed, server = self._execute(
            declared_length=15, name=CASE_NAME_COMPLETE
        )

        expected = _success_report(
            name=CASE_NAME_COMPLETE,
            field="status",
            expected_value="ok",
            expected_status=200,
        )
        self.assertEqual(report, expected)
        self.assertIsNone(report["error"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")


if __name__ == "__main__":
    unittest.main()
