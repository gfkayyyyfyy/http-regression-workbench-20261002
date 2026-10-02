"""等待响应超时的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证同一三秒超时行为的两个阶段：

1. 响应头阶段：服务收到 GET 后保持连接，却一直不发送响应头，
   客户端应因既有三秒超时结束；
2. 正文阶段：服务立即发送 200 响应头（声明 Content-Length: 15），
   却只发送不完整正文 ``b'{"status": '`` 并保持连接，
   客户端应因等待剩余正文超时结束。

两种情况下退出码均为 1，stderr 为空，stdout 只含一个可解析的
UTF-8 JSON 报告：error 为 request_failed（正文读取超时即使已经
收到 200，也不归入 invalid_response 或 assertion_failed），报告保留
用例名称、期望状态码、字段名和期望值，两项 actual 均为 null，
顶层及两项检查的 passed 均为 false。每次执行服务只收到一次 GET，
超时后没有重试。

测试保留产品原本的三秒设置（不替换请求结果、不缩短产品超时）；
时间判断允许正常调度误差，但单次命令超过十秒仍未结束即判失败
并终止该进程。服务使用系统分配的临时端口，用例写入临时文件，
不依赖固定 8765、不需要手工启动服务或访问公网；测试结束释放
服务、连接与临时文件，重复执行不留下占用端口的进程。
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

from api_workbench.runner import REQUEST_FAILED, REQUEST_TIMEOUT

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0
# 时间判断允许的正常调度误差：不要求恰好在第三秒退出
TIMING_TOLERANCE = 0.5

# 正文阶段场景发送的不完整正文（Content-Length 声明 15，实发 11 字节）
PARTIAL_BODY = b'{"status": '


class _HangingServer:
    """收到 GET 后按场景挂起的受控 127.0.0.1 服务（原始套接字）。

    mode="no_headers"：读取请求后保持连接，不发送任何响应字节；
    mode="partial_body"：发送 200 响应头与 PARTIAL_BODY 后保持连接。
    两种模式都保持连接直到客户端因超时断开或测试结束。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("no_headers", "partial_body"):
            raise ValueError(f"未知场景: {mode!r}")
        self._mode = mode
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
            # 避免把连接失败误当成响应超时
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

            if self._mode == "partial_body":
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    b"Content-Length: 15\r\n"
                    b"\r\n" + PARTIAL_BODY
                )

            # 保持连接，直到客户端因超时断开（recv 返回空）或测试结束
            while not self._stopping.is_set():
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
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


def _expected_timeout_report(
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


class _TimeoutFlowTestCase(unittest.TestCase):
    """公共设施：临时端口挂起服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(self, *, mode: str, name: str) -> tuple[dict, subprocess.CompletedProcess]:
        """按场景启动挂起服务、写用例、经公开入口执行，返回 (报告, 进程结果)。"""
        # 产品原本的三秒设置必须保留，不得通过缩短超时制造通过
        self.assertEqual(
            REQUEST_TIMEOUT, 3.0, "产品的请求超时设置必须保持为 3 秒"
        )

        server = _HangingServer(mode)
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

        started = time.monotonic()
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
        elapsed = time.monotonic() - started

        # 必须确实等到产品的三秒超时（允许调度误差），
        # 而非连接失败等原因立即退出
        self.assertGreaterEqual(
            elapsed,
            REQUEST_TIMEOUT - TIMING_TOLERANCE,
            f"命令 {elapsed:.2f} 秒即结束，未体现三秒超时",
        )

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

    def _assert_timeout_report(self, report: dict, completed, name: str) -> None:
        expected = _expected_timeout_report(
            name=name, field="status", expected_value="ok", expected_status=200
        )
        self.assertEqual(report, expected)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertNotIn(report["error"], ("invalid_response", "assertion_failed"))
        self.assertEqual(report["name"], name)
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
            self.server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )


class HeadersTimeoutTests(_TimeoutFlowTestCase):
    """响应头阶段超时：服务保持连接却一直不发送响应头。"""

    def test_no_response_headers_times_out_with_request_failed(self) -> None:
        report, completed = self._execute(mode="no_headers", name="timeout-no-headers")
        self._assert_timeout_report(report, completed, "timeout-no-headers")


class BodyTimeoutTests(_TimeoutFlowTestCase):
    """正文阶段超时：已收到 200 响应头，剩余正文一直不到达。"""

    def test_incomplete_body_times_out_with_request_failed(self) -> None:
        report, completed = self._execute(mode="partial_body", name="timeout-partial-body")
        self._assert_timeout_report(report, completed, "timeout-partial-body")
        # 已收到 200 仍必须归入 request_failed，且状态码 actual 不因
        # 已收到响应头而记录为 200（报告按请求失败整体处理）
        self.assertEqual(report["error"], "request_failed")
        self.assertIsNone(report["status_check"]["actual"])


if __name__ == "__main__":
    unittest.main()
