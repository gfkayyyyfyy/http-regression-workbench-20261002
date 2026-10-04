"""分块传输结束标记边界的端到端回归测试。

与 test_response_eof.py（无正文长度头、以连接关闭界定正文）和
test_early_body_close.py（声明 Content-Length 但实发不足）互补：
本文件覆盖响应声明 ``Transfer-Encoding: chunked`` 且不带
Content-Length 时，分块正文的**结束标记**是否完整的既有行为。
受控 127.0.0.1 服务在收完完整 GET 请求后返回
``HTTP/1.1 200 OK`` 响应头（含 Transfer-Encoding: chunked，不含
Content-Length），正文统一为 15 字节的 ``{"status":"ok"}``，按
前 10 字节、后 5 字节拆成两个数据块，块长分别写成十六进制 ``A``
与 ``5``，块长行和块数据都以 CRLF 结束。两个场景发送的两个数据
块完全相同，差异只在结束标记：

1. 正常结束：两个数据块之后追加零长度结束块 ``0\\r\\n\\r\\n``，再正常
   关闭连接。分块分帧完整结束，去帧后的正文是完整 JSON 对象，
   公开 run 入口应退出 0，error 为 null，总体及两项检查的 passed
   均为 true，状态码 actual 为 200，字段 actual 为 "ok"，
   present 为 true；
2. 缺少结束块：两个数据块发完后**省略** ``0\\r\\n\\r\\n`` 直接正常关闭
   连接。即使把已收到的两个数据块拼在一起已是完整 JSON
   ``{"status":"ok"}``，分块分帧仍未按协议正常结束
   （http.client 在读取阶段抛出 IncompleteRead），应按传输失败
   处理：退出 1，error 为 request_failed——不能归为
   invalid_response，也不能因为拼接内容恰好完整就判定成功；
   两项 actual 与字段 present 均为 null，总体及两项检查的 passed
   均为 false。

两种场景均经真实本地 HTTP 响应和公开命令
``python -m api_workbench run case.json`` 核对：每次运行服务只
收到一次 GET（无重试）；stdout 可按 UTF-8 解码且只含一个完整
JSON 对象（不依赖键序与缩进），stderr 为空、不出现 Traceback；
报告保留用例名称（沿用 cases/health.json 的 "health"）与各项
期望值。单次命令超过十秒仍未结束即判失败并终止该进程。

服务使用系统分配的临时端口，用例写入临时文件（除 url 指向
受控临时端口的 /health 外，名称与断言均沿用
cases/health.json），不需要手工启动 serve 或访问公网；测试
结束释放服务、连接与临时文件，重复执行不留下占用端口的进程。
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

# 两个场景共用的去帧后正文：15 个 ASCII 字节，可解析为合法 JSON 对象
BODY = b'{"status":"ok"}'
assert len(BODY) == 15

# 按前 10 字节、后 5 字节拆成两个数据块；块长分别写成十六进制 A 与 5
CHUNK_ONE = BODY[:10]
CHUNK_TWO = BODY[10:]
assert len(CHUNK_ONE) == 0xA == 10
assert len(CHUNK_TWO) == 0x5 == 5

# 零长度结束块：块长 0 后再跟一个（收尾的）空 CRLF
LAST_CHUNK = b"0\r\n\r\n"


class _ChunkedServer:
    """发送分块响应后正常关闭连接的受控 127.0.0.1 服务（原始套接字）。

    收到 GET 后返回 HTTP/1.1 200 响应头（含
    Transfer-Encoding: chunked，不带 Content-Length），随后发送
    两个数据块（A/10 字节与 5/5 字节，块长行与块数据均以 CRLF
    结束）；当 terminated 为真时在数据块后追加零长度结束块
    ``0\\r\\n\\r\\n``，否则省略结束块。两种情况下最后都正常关闭连接。
    监听保持到 close()，以便统计整个运行期间的 GET 次数
    （验证没有重试）。
    """

    def __init__(self, terminated: bool) -> None:
        self._terminated = terminated
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
            # 先完整读取请求头（以空行结束），确认服务确实收到了完整 GET
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

            # 分块响应：声明 Transfer-Encoding: chunked，不声明 Content-Length
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"A\r\n" + CHUNK_ONE + b"\r\n"
                b"5\r\n" + CHUNK_TWO + b"\r\n"
            )
            if self._terminated:
                # 正常结束：零长度结束块标记分块正文结束
                response += LAST_CHUNK
            # terminated 为假时省略结束块：两个数据块本身完整，
            # 但没有 0\r\n\r\n，随后直接关闭连接
            connection.sendall(response)
        except OSError:
            pass
        finally:
            # 两种场景都在发送后正常关闭连接：差异仅在结束标记是否已发送
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


class _ChunkedFlowTestCase(unittest.TestCase):
    """公共设施：临时端口服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(
        self, *, terminated: bool
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """按是否发送结束块启动服务、写临时用例、经公开入口执行。

        用例沿用 cases/health.json 的名称与断言（name=health、
        expected_status=200、field=status、expected_value="ok"），
        仅将 url 改为受控临时端口上的 /health。
        """
        server = _ChunkedServer(terminated)
        server.start()
        self.addCleanup(server.close)
        self.server = server

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        case = {
            "name": "health",
            "url": f"http://127.0.0.1:{server.port}/health",
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

        # stderr 必须为空、不出现 Traceback
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)

        # stdout 按 UTF-8 解码后有且仅有一个完整 JSON 对象；
        # raw_decode 解析不依赖键序与缩进，其后只允许空白
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


class TerminatedChunkedTests(_ChunkedFlowTestCase):
    """两个数据块后追加 ``0\\r\\n\\r\\n``：分块正常结束，整体通过。"""

    def test_chunked_body_with_last_chunk_passes(self) -> None:
        report, completed = self._execute(terminated=True)

        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(report["error"])

        # 报告保留用例名称与期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 状态检查：分块分帧正常结束，状态码可用
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])

        # 字段检查：去帧后正文完整可解析，字段存在且值匹配
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertIs(report["field_check"]["present"], True)
        self.assertTrue(report["field_check"]["passed"])

        self.assertTrue(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )

        # 完整报告结构比对（dict 相等不依赖 JSON 键序与缩进）
        self.assertEqual(
            report,
            {
                "name": "health",
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
                    "present": True,
                },
            },
        )


class UnterminatedChunkedTests(_ChunkedFlowTestCase):
    """两个数据块后省略 ``0\\r\\n\\r\\n`` 即关闭：request_failed。"""

    def test_chunked_body_without_last_chunk_is_request_failed(self) -> None:
        report, completed = self._execute(terminated=False)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertEqual(report["error"], "request_failed")
        # 分帧未正常结束属于传输失败：即使拼接内容已是完整 JSON，
        # 也不能归为 invalid_response 或判定成功
        self.assertNotIn(
            report["error"], ("invalid_response", "assertion_failed")
        )

        # 报告保留用例名称与期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 传输失败：状态码不保留，两项 actual 与字段 present 均为 null
        self.assertIsNone(report["status_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])

        self.assertFalse(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )

        # 完整报告结构比对（dict 相等不依赖 JSON 键序与缩进）
        self.assertEqual(
            report,
            {
                "name": "health",
                "passed": False,
                "error": "request_failed",
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
                    "present": None,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
