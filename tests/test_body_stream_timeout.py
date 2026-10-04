"""持续接收正文时 timeout_seconds 语义的端到端回归测试。

docs/request_timeout.txt 第二节第 1 点说明：timeout_seconds 限制的是
**每次**网络阻塞等待（连接、等待响应头、读取正文）的上限，不是整次
请求的累计截止时间；多次等待之间不累加、没有总预算。tests/
test_body_timeout.py 已覆盖「停顿后超时」与「短暂停顿后成功」两个
场景，但其成功场景总耗时仍小于配置的 0.5 秒，无法直接证伪「累计
时限」的错误解释。本文件补上一组持续接收正文的场景，把总耗时拉长到
配置值之上：服务收到 GET 后立即返回 200 响应头（Content-Length: 15，
完整正文 ``{"status":"ok"}``），再把正文按 10、2、2、1 字节分四段
发送，首段立即发出，其后每段间隔 0.4 秒，用例显式设置
timeout_seconds: 1。

1. 持续接收后完整送达（成功）：四段全部发出，从首段发出到命令结束
   实测超过 1.1 秒——若超时被错误实现为整次请求的累计一秒时限，命令
   不可能在持续接收超过一秒后仍以退出码 0 成功。报告 error 为 null，
   状态码实际值为 200，字段实际值为 "ok"，present 与所有 passed 均为
   true；
2. 末字节前挂起（失败）：沿用相同发送节奏，但发完第三段（14 字节）
   后保持连接、不发送最后一个字节。命令应从最后一次正文发送起 0.7 至
   2 秒内以退出码 1 结束：超时重新在「等待下一段」这一次阻塞调用上
   计满一秒。报告 error 为 request_failed，两项 actual 与
   field_check.present 均为 null，所有 passed 均为 false——即使 200
   响应头早已收到，也不保留状态码实际值。

两个场景都记录每段实际发出的单调时间并断言相邻段间隔至少 0.3 秒，
避免服务一次性发送完整正文也能满足总时长预期；stdout 都只有一个可
解析的 UTF-8 JSON 报告，stderr 为空，服务各只收到一次 GET，无重试。
用例沿用 cases/health.json 的名称与断言，仅替换服务地址并设置
timeout_seconds: 1。测试仅使用标准库与系统分配的临时端口、临时用例
文件，不依赖公网或手动启动示例服务；单次命令超过十秒即判失败并终止
该进程，正常结束或断言失败时都释放服务、连接与临时文件。可单独执行：
``python -m unittest discover -s tests -p test_body_stream_timeout.py``。
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
CUSTOM_TIMEOUT = 1
# 首段之后每段正文之间的间隔（秒）
CHUNK_INTERVAL = 0.4
# 相邻段实际发出间隔的下限断言：小于它视为没有真正分段
CHUNK_GAP_MIN = 0.3
# 成功场景：从首段发出到命令结束必须超过该值（三段间隔累计 1.2 秒）
SUCCESS_MIN_ELAPSED = 1.1
# 失败场景：从最后一次正文发送到命令结束的等待窗口：
# 下限排除连接失败等原因立即退出，上限发现忽略配置回退到三秒的情况
FAIL_WAIT_MIN = 0.7
FAIL_WAIT_MAX = 2.0

# 响应头声明正文 15 字节，按 10、2、2、1 分段：
# {"status": + "o + k" + }  => {"status":"ok"}
DECLARED_CONTENT_LENGTH = 15
BODY_CHUNKS = [b'{"status":', b'"o', b'k"', b"}"]
FULL_BODY = b"".join(BODY_CHUNKS)
# 失败场景只发前三段（14 字节），最后一个字节始终不发
STALL_AFTER_CHUNKS = 3


class _ChunkedBodyServer:
    """收到 GET 后立即给 200 响应头、按固定节奏分段发送正文的受控服务。

    mode="stream"：依次发出全部 BODY_CHUNKS，首段立即发出，其后每段
    间隔 CHUNK_INTERVAL 秒；
    mode="stall"：只发前 STALL_AFTER_CHUNKS 段，随后保持连接、不发送
    最后一个字节，直到客户端因超时断开或测试结束。

    记录 GET 次数与每段实际发出的单调时间，供测试核对分段节奏、
    并以首段/末段发出时间为起点衡量命令耗时。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("stream", "stall"):
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
            # 禁用 Nagle：每段必须立即独立发出，分段时间与节奏才精确
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.settimeout(0.2)
            request = b""
            # 先完整读取请求头，确认服务确实收到了 GET，
            # 避免把连接失败误当成正文等待
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
                if self._mode == "stream"
                else STALL_AFTER_CHUNKS
            )
            # 首段立即发出，其后每段间隔 CHUNK_INTERVAL 秒
            for index in range(chunk_count):
                if index > 0:
                    time.sleep(CHUNK_INTERVAL)
                connection.sendall(BODY_CHUNKS[index])
                with self._lock:
                    self.chunk_sent_times.append(time.monotonic())

            # 完整正文已发完（stream），或末字节前挂起（stall）：
            # 都保持连接，直到客户端断开（recv 返回空）或测试结束
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


class _BodyStreamFlowTestCase(unittest.TestCase):
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

    def _assert_chunks_spaced(self, sent_times: list[float], count: int) -> None:
        """断言各段确实分段、按间隔发出，而非一次性发送完整正文。"""
        self.assertEqual(
            len(sent_times),
            count,
            f"服务应实际发出 {count} 段正文，实际记录 {len(sent_times)} 段",
        )
        for previous, current in zip(sent_times, sent_times[1:]):
            gap = current - previous
            self.assertGreaterEqual(
                gap,
                CHUNK_GAP_MIN,
                f"相邻正文段仅间隔 {gap:.2f} 秒，未按 {CHUNK_INTERVAL} 秒"
                "节奏分段，疑似一次性发送完整正文",
            )


class BodyStreamTimeoutTests(_BodyStreamFlowTestCase):
    """持续接收正文：每次等待各自计超时而非整次请求的累计时限。"""

    def test_progressing_body_exceeds_configured_timeout_and_passes(self) -> None:
        # 用例构造的完整正文必须与声明的 Content-Length 及分段一致
        self.assertEqual(FULL_BODY, b'{"status":"ok"}')
        self.assertEqual(len(FULL_BODY), DECLARED_CONTENT_LENGTH)
        self.assertEqual(
            [len(chunk) for chunk in BODY_CHUNKS], [10, 2, 2, 1]
        )
        self.assertEqual(sum(map(len, BODY_CHUNKS)), DECLARED_CONTENT_LENGTH)

        server = _ChunkedBodyServer("stream")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, ended = self._run_command(case_path)

        # 整个运行只发送一次 GET，成功后也没有重试
        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
        # 四段确实按 0.4 秒节奏分段发出，避免一次性发送也能满足总时长
        self._assert_chunks_spaced(server.chunk_sent_times, len(BODY_CHUNKS))

        # 核心语义：从首段发出到命令结束超过 1.1 秒（大于 1 秒配置值）。
        # 若 timeout_seconds 被解释为整次请求的累计截止时间，持续接收
        # 超过一秒后必然失败，不可能以退出码 0 成功
        elapsed = ended - server.chunk_sent_times[0]
        self.assertGreater(
            elapsed,
            SUCCESS_MIN_ELAPSED,
            f"从首段发出到命令结束仅 {elapsed:.2f} 秒，未超过 1 秒配置值，"
            "无法证明超时按每次等待分别约束而非累计时限",
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
        # 用例名称、字段名与两项期望值原样保留
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

    def test_stall_before_final_byte_times_out_per_wait(self) -> None:
        # 失败场景只发前三段（14 字节），末字节始终不发
        sent_length = sum(map(len, BODY_CHUNKS[:STALL_AFTER_CHUNKS]))
        self.assertEqual(sent_length, DECLARED_CONTENT_LENGTH - 1)

        server = _ChunkedBodyServer("stall")
        server.start()
        self.addCleanup(server.close)

        case_path = self._write_case(server.port)
        completed, ended = self._run_command(case_path)

        # 整个运行只发送一次 GET，超时后不得重试
        self.assertEqual(server.get_count, 1, "整个运行只应发送一次 GET")
        # 前三段同样必须按节奏分段发出，场景才是「持续接收后在末字节前
        # 挂起」而非一开始就不发正文
        self._assert_chunks_spaced(
            server.chunk_sent_times, STALL_AFTER_CHUNKS
        )

        # 从最后一次正文发送起计时：等待下一段的这一次阻塞调用各自计满
        # 1 秒超时，约 1 秒后结束；接受 0.7 至 2 秒窗口，窗口之外判失败
        wait = ended - server.chunk_sent_times[-1]
        self.assertGreaterEqual(
            wait,
            FAIL_WAIT_MIN,
            f"最后一段发出后 {wait:.2f} 秒命令即结束，未体现 1 秒正文等待超时",
        )
        self.assertLessEqual(
            wait,
            FAIL_WAIT_MAX,
            f"最后一段发出后等待 {wait:.2f} 秒，超出 1 秒配置的合理误差，"
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
        # 即使 200 响应头早已收到，正文未收满即超时，整体按请求失败
        # 处理，不归为 invalid_response，也不保留状态码实际值
        self.assertEqual(report["error"], "request_failed")
        self.assertNotIn(report["error"], ("invalid_response", "assertion_failed"))
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
