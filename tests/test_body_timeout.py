"""正文等待阶段应用自定义 timeout_seconds 的端到端回归测试。

默认三秒超时下的正文阶段场景见 tests/test_timeout_behavior.py，
自定义超时在响应头等待阶段的生效情况见
tests/test_timeout_seconds_config.py。本文件补齐正文等待阶段使用
0.5 秒自定义超时的两个可执行场景，均通过临时端口上的受控
127.0.0.1 服务与公开入口 ``python -m api_workbench run case.json``
发送真实 GET：

1. 正文等待超时：服务收到 GET 后立即发送 200 响应头（声明
   Content-Length: 15），只发送正文前缀 ``{"status":``（10 字节），
   随后保持连接、不补齐正文。命令应在正文等待超时后以退出码 1
   结束：即使已经收到 200 响应头，整体仍按请求失败处理——error 为
   request_failed（不得归为 invalid_response），两项 actual 与
   field_check.present 均为 null，顶层及两项检查的 passed 均为 false，
   用例名称、字段名和两项期望值原样保留。以服务发完前缀到命令结束
   计时，接受 0.3 至 2 秒：窗口之外判失败，从而能发现忽略配置、
   回退到默认三秒的实现；
2. 成功对照：沿用相同配置与响应头，服务发出前缀后等待 0.1 秒，再
   补发 ``"ok"}``，组成完整正文 ``{"status":"ok"}``（恰好 15 字节）。
   命令退出码为 0，error 为 null，两项 actual 分别为 200 与 "ok"，
   所有 passed 为 true，field_check.present 为 true。

两个场景的 stdout 都只有一个可解析的 UTF-8 JSON 报告，stderr 为空，
服务各只收到一次 GET，超时或成功后都没有重试。用例沿用
cases/health.json 的字段，仅将 url 指向受控服务的临时端口并加入
timeout_seconds: 0.5。测试仅使用标准库与系统分配的临时端口、临时
用例文件，不依赖固定 8765、不需要访问公网；单次命令超过十秒即判
失败并终止该进程，测试结束释放服务、连接与临时文件，重复执行不
留下占用端口的进程。可单独执行：
``python -m unittest discover -s tests -p test_body_timeout.py``。
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

# 用例显式配置的每次网络阻塞等待上限（秒）
CUSTOM_TIMEOUT = 0.5
# 以服务发完正文前缀到命令结束的等待窗口：
# 下限排除连接失败等原因立即退出，上限发现忽略配置回退到三秒的情况
WAIT_MIN = 0.3
WAIT_MAX = 2.0
# 成功对照中前缀与剩余正文之间的间隔（秒）
COMPLETE_DELAY = 0.1

# 响应头声明正文 15 字节：先发 10 字节前缀并挂起，成功对照再补发 5 字节
DECLARED_CONTENT_LENGTH = 15
BODY_PREFIX = b'{"status":'
BODY_SUFFIX = b'"ok"}'
FULL_BODY = BODY_PREFIX + BODY_SUFFIX  # {"status":"ok"}


class _PartialBodyServer:
    """收到 GET 后立即给 200 响应头、只发部分正文的受控 127.0.0.1 服务。

    mode="stall"：发出 BODY_PREFIX 后一直保持连接，不补齐剩余正文，
    直到客户端因超时断开或测试结束；
    mode="complete"：发出 BODY_PREFIX 后等待 COMPLETE_DELAY 秒，再发出
    BODY_SUFFIX 补齐正文。

    记录 GET 次数，以及每次发完前缀的单调时间，供测试以前缀发完为
    起点衡量正文等待时长。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("stall", "complete"):
            raise ValueError(f"未知场景: {mode!r}")
        self._mode = mode
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_count = 0
        self.prefix_sent_times: list[float] = []
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
            # 禁用 Nagle：前缀必须立即发出，计时起点才精确
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(0.2)
            request = b""
            # 先完整读取请求头，确认服务确实收到了 GET，
            # 避免把连接失败误当成正文等待超时
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

            # 立即发送 200 响应头（声明 15 字节正文），再只发 10 字节前缀
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {DECLARED_CONTENT_LENGTH}\r\n".encode("ascii")
                + b"\r\n"
            )
            connection.sendall(BODY_PREFIX)
            with self._lock:
                self.prefix_sent_times.append(time.monotonic())

            if self._mode == "complete":
                # 成功对照：前缀之后停顿 0.1 秒（仍在 0.5 秒超时之内），
                # 再补齐完整正文
                time.sleep(COMPLETE_DELAY)
                connection.sendall(BODY_SUFFIX)

            # 保持连接，直到客户端断开（recv 返回空）或测试结束
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


class _BodyTimeoutFlowTestCase(unittest.TestCase):
    """公共设施：临时用例文件、经公开入口执行命令与单 JSON 报告解析。"""

    def setUp(self) -> None:
        # 产品默认三秒超时必须保持不变；本文件场景依赖用例显式配置 0.5
        self.assertEqual(
            REQUEST_TIMEOUT, 3.0, "产品的默认请求超时设置必须保持为 3 秒"
        )
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmpdir = tmpdir.name

    def _write_case(self, port: int) -> str:
        """按 cases/health.json 同构格式写临时用例。

        仅将 url 指向受控服务的临时端口，并加入 timeout_seconds: 0.5；
        用例名称、字段名、期望值与期望状态码原样保留。
        """
        case = {
            "name": "health",
            "url": f"http://127.0.0.1:{port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
            "timeout_seconds": CUSTOM_TIMEOUT,
        }
        case_path = os.path.join(self.tmpdir, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)
        return case_path

    def _run_command(self, case_path: str) -> tuple[subprocess.CompletedProcess, float]:
        """经公开入口执行用例，返回 (进程结果, 命令结束的单调时间)。"""
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
        return completed, time.monotonic()

    def _single_report(self, completed: subprocess.CompletedProcess) -> dict:
        """断言 stderr 为空，且 stdout 是唯一的 UTF-8 JSON 报告，返回该报告。"""
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)
        # 严格按 UTF-8 解码：stdout 必须是一个 UTF-8 JSON 报告
        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report


class CustomBodyTimeoutTests(_BodyTimeoutFlowTestCase):
    """正文等待阶段：0.5 秒自定义超时与及时补齐正文的成功对照。"""

    def test_incomplete_body_times_out_with_custom_timeout(self) -> None:
        # 用例构造的完整正文必须与声明的 Content-Length 一致，
        # 前缀必须确实不完整，场景才有意义
        self.assertEqual(len(FULL_BODY), DECLARED_CONTENT_LENGTH)
        self.assertLess(len(BODY_PREFIX), DECLARED_CONTENT_LENGTH)

        server = _PartialBodyServer("stall")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, ended = self._run_command(case_path)

        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )
        # 以服务发完前缀到命令结束计时：0.5 秒配置应落在 0.3 至 2 秒之间；
        # 若实现忽略配置回退到三秒，等待会超过两秒上限
        wait = ended - server.prefix_sent_times[0]
        self.assertGreaterEqual(
            wait,
            WAIT_MIN,
            f"服务发完前缀后 {wait:.2f} 秒命令即结束，未体现 0.5 秒正文等待超时",
        )
        self.assertLessEqual(
            wait,
            WAIT_MAX,
            f"服务发完前缀后等待 {wait:.2f} 秒，超出 0.5 秒配置的合理误差，"
            "疑似忽略配置回退到三秒",
        )

        self.assertEqual(completed.returncode, 1)
        report = self._single_report(completed)
        expected = {
            "name": "health",
            "passed": False,
            "error": REQUEST_FAILED,
            "status_check": {"expected": 200, "actual": None, "passed": False},
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": None,
                "passed": False,
                "present": None,
            },
        }
        self.assertEqual(report, expected)
        # 即使已收到 200 响应头，也必须整体按请求失败处理，
        # 不得把正文不完整归为 invalid_response
        self.assertEqual(report["error"], "request_failed")
        self.assertNotIn(
            report["error"], ("invalid_response", "assertion_failed")
        )
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        # 用例名称、字段名与两项期望值原样保留
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

    def test_completed_body_after_short_delay_passes(self) -> None:
        # 成功对照：前缀 + 0.1 秒后补发剩余正文，恰好组成完整 15 字节正文
        self.assertEqual(FULL_BODY, b'{"status":"ok"}')
        self.assertEqual(len(FULL_BODY), DECLARED_CONTENT_LENGTH)

        server = _PartialBodyServer("complete")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, _ = self._run_command(case_path)

        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
        self.assertEqual(completed.returncode, 0)
        report = self._single_report(completed)
        expected = {
            "name": "health",
            "passed": True,
            "error": None,
            "status_check": {"expected": 200, "actual": 200, "passed": True},
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": "ok",
                "passed": True,
                "present": True,
            },
        }
        self.assertEqual(report, expected)
        self.assertIsNone(report["error"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertIs(report["field_check"]["present"], True)


if __name__ == "__main__":
    unittest.main()
