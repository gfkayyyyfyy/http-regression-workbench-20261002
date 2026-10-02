"""等待响应超时的端到端回归测试（覆盖超时行为的两个阶段）。

通过临时端口上的受控 127.0.0.1 原始 TCP 服务与公开入口
``python -m api_workbench run case.json`` 验证产品既有的三秒超时：

1. 服务收到 GET 后保持连接、始终不发送响应头：客户端应在等待
   响应头时超时结束；
2. 服务立即返回 ``200`` 响应头并声明 ``Content-Length: 15``，
   却只发送未完整正文 ``{"status": ``（11 字节）后保持连接：
   客户端应在等待剩余正文时超时结束——即使已经收到 200。

两种情况下结果一致：退出码为 1，stderr 为空，stdout 只含一个
可解析的 UTF-8 JSON 报告；error 为 request_failed（不得归入
invalid_response 或 assertion_failed）；报告保留用例名称、
期望状态码、字段名与期望值，两项 actual 均为 null，顶层及两项
检查的 passed 均为 false。整个执行服务只收到一次 GET，
超时后不重试。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不替换请求结果、不缩短产品超时（REQUEST_TIMEOUT 保持 3 秒），
不需要手工启动服务、不访问公网或第三方包。
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

# 产品既有超时必须保持 3 秒：测试不得通过缩短超时来制造通过
PRODUCT_TIMEOUT_SECONDS = 3.0
# 正常调度误差下，超时退出至少应等待这么久，用以区分“立即连接失败”
LOWER_BOUND_SECONDS = 2.5
# 单次命令的硬上限：超过仍未结束则判失败并终止进程
HARD_DEADLINE_SECONDS = 10.0
# 等待服务确认收到请求的上限
REQUEST_OBSERVED_TIMEOUT = 5.0

# 场景二：声明 15 字节正文，却只发送 11 字节前缀后保持连接
PARTIAL_BODY = b'{"status": '
assert len(PARTIAL_BODY) == 11
DECLARED_CONTENT_LENGTH = 15


class _HoldingServer:
    """接受 GET 后按场景保持连接的受控服务（原始 TCP，非 HTTP 框架）。

    mode="no_headers"：读完请求后不发送任何响应字节，一直保持连接；
    mode="partial_body"：立即发送 200 响应头（Content-Length: 15）与
    11 字节未完整正文，随后保持连接。

    通过请求计数与 ``request_received`` 事件让测试确认服务确实收到
    了 GET，避免把连接失败误判为响应超时。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("no_headers", "partial_body"):
            raise ValueError(f"未知超时场景: {mode!r}")
        self.mode = mode
        self._lock = threading.Lock()
        self.get_count = 0
        self.requested_paths: list[str] = []
        self.request_received = threading.Event()

        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(8)
        self.port = self._listen.getsockname()[1]

        self._stop = threading.Event()
        self._client_sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="holding-server-accept", daemon=True
        )

    def start(self) -> None:
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        self._listen.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # 监听套接字已关闭
            with self._lock:
                self._client_sockets.append(conn)
            thread = threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _handle(self, conn: socket.socket) -> None:
        try:
            # 读取完整请求头（GET 无正文），短超时轮询以便服务停止时及时退出
            conn.settimeout(0.2)
            data = b""
            while b"\r\n\r\n" not in data:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    if self._stop.is_set():
                        return
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                data += chunk
                first_line_end = data.find(b"\r\n")
                if first_line_end != -1 and not data[:first_line_end].startswith(b"GET "):
                    return  # 非 GET，不在本测试预期内，直接结束

            first_line = data[: data.find(b"\r\n")].decode("iso-8859-1")
            parts = first_line.split(" ")
            path = parts[1] if len(parts) >= 2 else "?"
            with self._lock:
                self.get_count += 1
                self.requested_paths.append(path)
            self.request_received.set()

            if self.mode == "partial_body":
                try:
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json; charset=utf-8\r\n"
                        b"Content-Length: "
                        + str(DECLARED_CONTENT_LENGTH).encode("ascii")
                        + b"\r\nConnection: close\r\n\r\n"
                        + PARTIAL_BODY
                    )
                except OSError:
                    return

            # 保持连接（不补齐正文、不关闭），直到测试结束释放
            while not self._stop.wait(0.2):
                pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        """释放监听套接字与全部客户端连接，join 工作线程。"""
        self._stop.set()
        try:
            self._listen.close()
        except OSError:
            pass
        self._accept_thread.join(timeout=2)
        with self._lock:
            clients = list(self._client_sockets)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)


def _expected_timeout_report(name: str) -> dict:
    """按 runner 的 request_failed 报告结构构造期望报告。"""
    return {
        "name": name,
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


class _TimeoutFlowTestCase(unittest.TestCase):
    """公共设施：受控保持服务、临时用例文件、带硬截止的子进程执行。"""

    hold_mode = "no_headers"
    scenario_label = "等待响应头超时"

    def setUp(self) -> None:
        self.server = _HoldingServer(self.hold_mode)
        self.server.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.stop()

    def _diag(self, detail: str) -> str:
        return f"[{self.scenario_label}] {detail}"

    def _start_case(self, name: str) -> tuple[subprocess.Popen, float]:
        """写用例并经公开入口启动子进程（不等待结束），返回 (进程, 起始时刻)。"""
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case_path = os.path.join(self.tmpdir.name, f"{name}.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        process = subprocess.Popen(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process, time.monotonic()

    def _finish_case(
        self, process: subprocess.Popen, started: float
    ) -> tuple[dict, int, bytes, float]:
        """等待子进程结束（10 秒硬截止），返回 (报告, 退出码, 原始 stderr, 耗时)。"""
        try:
            stdout, stderr = process.communicate(timeout=HARD_DEADLINE_SECONDS)
        except subprocess.TimeoutExpired:
            # 超过十秒仍未结束：终止进程并判失败，绝不遗留占用端口的进程
            process.kill()
            process.communicate()
            self.fail(
                self._diag(
                    f"命令超过 {HARD_DEADLINE_SECONDS:.0f} 秒仍未结束，已终止该进程"
                )
            )
        elapsed = time.monotonic() - started
        return self._parse_report(stdout), process.returncode, stderr, elapsed

    def _parse_report(self, stdout: bytes) -> dict:
        text = stdout.decode("utf-8")  # 非法 UTF-8 会直接抛错使测试失败
        decoder = json.JSONDecoder()
        try:
            report, end = decoder.raw_decode(text)
        except json.JSONDecodeError as exc:
            self.fail(self._diag(f"stdout 不是可解析的 JSON 报告: {exc}: {text!r}"))
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            text[end:].strip(),
            "",
            self._diag(f"stdout 只能包含一个 JSON 报告: {text!r}"),
        )
        self.assertIsInstance(report, dict, self._diag("报告必须是 JSON 对象"))
        return report

    def _check_timeout_outcome(self, name: str) -> None:
        # 先启动子进程，再确认服务确实收到了 GET，避免连接失败被误判为超时
        process, started = self._start_case(name)
        received = self.server.request_received.wait(REQUEST_OBSERVED_TIMEOUT)
        self.assertTrue(
            received,
            self._diag("服务未收到任何请求：无法证明本次结果是响应超时而非连接失败"),
        )

        report, returncode, stderr, elapsed = self._finish_case(process, started)

        stderr_text = stderr.decode("utf-8")
        self.assertEqual(
            stderr_text, "", self._diag(f"stderr 必须为空: {stderr_text!r}")
        )
        self.assertNotIn("Traceback", stderr_text, self._diag("stderr 出现 Traceback"))
        self.assertEqual(
            returncode, 1, self._diag(f"超时场景退出码应为 1，实际 {returncode}")
        )

        expected = _expected_timeout_report(name)
        self.assertEqual(
            report,
            expected,
            self._diag(
                "报告内容差异:\n实际: "
                + json.dumps(report, ensure_ascii=False, sort_keys=True)
                + "\n期望: "
                + json.dumps(expected, ensure_ascii=False, sort_keys=True)
            ),
        )
        # 逐项强调超时语义：即使收到过 200，正文读取超时也不是
        # invalid_response / assertion_failed，两项 actual 均为 null
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertNotEqual(report["error"], "invalid_response")
        self.assertNotEqual(report["error"], "assertion_failed")
        self.assertFalse(report["passed"])
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 整个执行只发出一次 GET，超时后没有重试
        self.assertEqual(
            self.server.get_count,
            1,
            self._diag(
                f"服务应只收到一次 GET，实际 {self.server.get_count} 次，"
                f"路径 {self.server.requested_paths!r}"
            ),
        )

        # 产品超时保持 3 秒；允许正常调度误差，但必须确实等待过，
        # 而不是因连接问题立刻失败（且硬截止 10 秒已在 _run_case 守护）
        self.assertEqual(
            REQUEST_TIMEOUT,
            PRODUCT_TIMEOUT_SECONDS,
            "产品 REQUEST_TIMEOUT 必须保持 3 秒，测试不得缩短产品超时",
        )
        self.assertGreaterEqual(
            elapsed,
            LOWER_BOUND_SECONDS,
            self._diag(
                f"耗时 {elapsed:.2f}s 过短：未等待 {PRODUCT_TIMEOUT_SECONDS:.0f} 秒"
                "产品超时即返回，疑似连接失败而非响应超时"
            ),
        )
        self.assertLess(
            elapsed,
            HARD_DEADLINE_SECONDS,
            self._diag(f"耗时 {elapsed:.2f}s 超过硬截止"),
        )


class NoResponseHeadersTimeoutTests(_TimeoutFlowTestCase):
    """收到 GET 后从不发送响应头：等待响应头阶段超时。"""

    hold_mode = "no_headers"
    scenario_label = "服务不发送响应头"

    def test_waiting_for_headers_times_out_as_request_failed(self) -> None:
        self._check_timeout_outcome("timeout-no-headers")


class PartialBodyReadTimeoutTests(_TimeoutFlowTestCase):
    """已发 200 响应头但正文不完整：等待剩余正文阶段同样超时。"""

    hold_mode = "partial_body"
    scenario_label = "正文未发送完整（Content-Length: 15，仅 11 字节）"

    def test_waiting_for_remaining_body_times_out_as_request_failed(self) -> None:
        self._check_timeout_outcome("timeout-partial-body")


if __name__ == "__main__":
    unittest.main()
