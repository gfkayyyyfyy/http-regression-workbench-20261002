"""持续接收正文时 timeout_seconds 语义的回归测试。

docs/request_timeout.txt 第二节说明：timeout_seconds 限制的是每次网络
阻塞等待的上限，而不是整次请求的累计时限，多次等待之间不累加、没有
总预算。tests/test_body_timeout.py 的成功对照总耗时仍小于配置值，
无法区分"每次等待上限"与"全程累计时限"两种解释；本文件用
timeout_seconds 为 1 秒、正文分四段（10、2、2、1 字节）间隔 0.4 秒
持续到达的场景补齐这一回归覆盖——累计耗时约 1.2 秒，超过配置值本身，
只有按"每次阻塞等待上限"解释才能通过。两个场景均通过临时端口上的
受控 127.0.0.1 服务与公开入口 ``python -m api_workbench run case.json``
发送真实 GET：

1. 成功场景：服务收到 GET 后立即返回 200 响应头（声明
   Content-Length: 15），首段正文立即发出，后面每段间隔 0.4 秒，
   完整正文为 ``{"status":"ok"}``。每次阻塞等待约 0.4 秒，都在 1 秒
   上限之内，命令应以退出码 0 结束：error 为 null，状态码实际值为
   200，字段实际值为 "ok"，present 与所有 passed 均为 true。从首段
   发出到命令结束的实测时长应超过 1.1 秒——若把 1 秒配置解释成整次
   请求的累计时限，命令会在正文收齐前失败。服务记录每段发出的单调
   时间，测试据此确认各段确实按间隔发出，避免一次性发送完整正文也
   能满足预期；
2. 失败场景：沿用相同发送节奏，但发完第三段（共 14 字节）后保持
   连接，不发送最后一个字节。命令应从最后一次正文发送起，在 0.7 至
   2 秒内以退出码 1 结束：error 为 request_failed（不得归为
   invalid_response），即使已收到 200 响应头也不保留状态码实际值——
   两项 actual 及 field_check.present 均为 null，所有 passed 均为
   false。

两个场景都沿用 cases/health.json 的名称与断言，仅替换服务地址并
设置 timeout_seconds 为 1；用例名称、字段名和期望值在报告中原样
保留。stdout 只有一个可解析的 UTF-8 JSON 报告，stderr 为空，每个
场景只收到一次 GET，超时或成功后都没有重试。测试仅使用标准库与
系统分配的临时端口、临时用例文件，不依赖公网或手动启动的示例服务；
单次命令超过十秒即判失败并终止该进程，正常结束或断言失败时都释放
服务、连接与临时文件。可单独执行：
``python -m unittest discover -s tests -p test_chunked_body_timeout.py``。
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
CUSTOM_TIMEOUT = 1.0
# 相邻正文段之间的发送间隔（秒）：小于 1 秒上限，四段累计约 1.2 秒
CHUNK_INTERVAL = 0.4
# 成功场景从首段发出到命令结束的最短实测时长：
# 超过 1 秒配置值本身，才能排除"累计时限"的解释
MIN_TOTAL_ELAPSED = 1.1
# 确认各段按间隔发出的最小相邻间隔：留 0.1 秒调度余量，
# 一次性发出完整正文的实现无法满足
MIN_CHUNK_GAP = CHUNK_INTERVAL - 0.1
# 失败场景从最后一次正文发送到命令结束的等待窗口：
# 下限排除立即退出，上限发现忽略配置回退到三秒或按累计时限等待的实现
WAIT_MIN = 0.7
WAIT_MAX = 2.0

# 响应头声明正文 15 字节，按 10、2、2、1 字节分四段发出
DECLARED_CONTENT_LENGTH = 15
BODY_CHUNKS = [b'{"status":', b'"o', b'k"', b"}"]
FULL_BODY = b"".join(BODY_CHUNKS)  # {"status":"ok"}
# 失败场景只发前三段（共 14 字节），最后一个字节始终不发
STALL_CHUNK_COUNT = 3


class _ChunkedBodyServer:
    """收到 GET 后立即给 200 响应头、分段发送正文的受控 127.0.0.1 服务。

    mode="complete"：按 10、2、2、1 字节依次发出全部四段，首段立即
    发出，后面每段间隔 CHUNK_INTERVAL 秒，组成完整正文；
    mode="stall"：沿用相同节奏只发前三段，随后保持连接，不发送最后
    一个字节，直到客户端因超时断开或测试结束。

    记录 GET 次数与每段正文发出的单调时间，供测试确认发送节奏，
    并分别以首段/末段发出为起点衡量命令耗时。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("complete", "stall"):
            raise ValueError(f"未知场景: {mode!r}")
        self._mode = mode
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_count = 0
        self.chunk_sent_times: list[float] = []
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
            # 禁用 Nagle：每段必须立即发出，计时起点才精确
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

            # 立即发送 200 响应头（声明 15 字节正文）
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {DECLARED_CONTENT_LENGTH}\r\n".encode("ascii")
                + b"\r\n"
            )

            chunk_count = (
                len(BODY_CHUNKS)
                if self._mode == "complete"
                else STALL_CHUNK_COUNT
            )
            for index in range(chunk_count):
                if index > 0:
                    # 首段立即发出，后面每段间隔 0.4 秒：
                    # 每次阻塞等待都在 1 秒上限之内，累计约 1.2 秒
                    time.sleep(CHUNK_INTERVAL)
                connection.sendall(BODY_CHUNKS[index])
                with self._lock:
                    self.chunk_sent_times.append(time.monotonic())

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


class _ChunkedBodyFlowTestCase(unittest.TestCase):
    """公共设施：临时用例文件、经公开入口执行命令与单 JSON 报告解析。"""

    def setUp(self) -> None:
        # 产品默认三秒超时必须保持不变；本文件场景依赖用例显式配置 1 秒
        self.assertEqual(
            REQUEST_TIMEOUT, 3.0, "产品的默认请求超时设置必须保持为 3 秒"
        )
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmpdir = tmpdir.name

    def _write_case(self, port: int) -> str:
        """按 cases/health.json 同构格式写临时用例。

        仅将 url 指向受控服务的临时端口，并加入 timeout_seconds: 1；
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

    def _assert_chunk_pacing(self, server: _ChunkedBodyServer, count: int) -> None:
        """确认各段确实按间隔发出，而非一次性发送完整正文。"""
        times = server.chunk_sent_times
        self.assertEqual(
            len(times), count, f"服务应发出 {count} 段正文: {times!r}"
        )
        for index in range(1, len(times)):
            gap = times[index] - times[index - 1]
            self.assertGreaterEqual(
                gap,
                MIN_CHUNK_GAP,
                f"第 {index} 段与第 {index + 1} 段间隔仅 {gap:.2f} 秒，"
                f"未按约 {CHUNK_INTERVAL} 秒的节奏分段发出",
            )


class ChunkedBodyTimeoutTests(_ChunkedBodyFlowTestCase):
    """持续接收正文：1 秒超时约束每次阻塞等待，而非整次请求的累计时限。"""

    def test_chunked_body_within_per_wait_timeout_passes(self) -> None:
        # 用例构造的完整正文必须与声明的 Content-Length 一致，
        # 分段计划必须确实覆盖 10、2、2、1 字节四段
        self.assertEqual(FULL_BODY, b'{"status":"ok"}')
        self.assertEqual(len(FULL_BODY), DECLARED_CONTENT_LENGTH)
        self.assertEqual([len(chunk) for chunk in BODY_CHUNKS], [10, 2, 2, 1])

        server = _ChunkedBodyServer("complete")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, ended = self._run_command(case_path)

        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
        self._assert_chunk_pacing(server, len(BODY_CHUNKS))
        # 从首段发出到命令结束的累计耗时约 1.2 秒，必须超过 1 秒配置值
        # 本身：若把 timeout_seconds 解释成整次请求的累计时限，正文不可能
        # 收齐，命令会以 request_failed 失败而非成功
        elapsed = ended - server.chunk_sent_times[0]
        self.assertGreater(
            elapsed,
            MIN_TOTAL_ELAPSED,
            f"从首段发出到命令结束仅 {elapsed:.2f} 秒，"
            "未体现持续接收正文超过配置值本身仍能成功的语义",
        )

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

    def test_stalled_after_third_chunk_times_out_per_wait(self) -> None:
        # 失败场景只发前三段（共 14 字节），最后一个字节始终不发
        self.assertLess(STALL_CHUNK_COUNT, len(BODY_CHUNKS))
        stalled_bytes = sum(len(chunk) for chunk in BODY_CHUNKS[:STALL_CHUNK_COUNT])
        self.assertEqual(stalled_bytes, DECLARED_CONTENT_LENGTH - 1)

        server = _ChunkedBodyServer("stall")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, ended = self._run_command(case_path)

        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )
        self._assert_chunk_pacing(server, STALL_CHUNK_COUNT)
        # 从最后一次正文发送到命令结束：1 秒配置应落在 0.7 至 2 秒之间；
        # 若实现忽略配置回退到三秒，等待会超过两秒上限
        wait = ended - server.chunk_sent_times[-1]
        self.assertGreaterEqual(
            wait,
            WAIT_MIN,
            f"最后一次正文发送后 {wait:.2f} 秒命令即结束，"
            "未体现 1 秒正文等待超时",
        )
        self.assertLessEqual(
            wait,
            WAIT_MAX,
            f"最后一次正文发送后等待 {wait:.2f} 秒，超出 1 秒配置的合理误差，"
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
        # 不得保留状态码实际值，也不得归为 invalid_response
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


if __name__ == "__main__":
    unittest.main()
