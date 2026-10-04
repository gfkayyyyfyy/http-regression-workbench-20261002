"""未声明正文长度、以连接正常关闭界定正文结束的端到端回归测试。

与 test_early_body_close.py 互补：该文件覆盖响应声明了
Content-Length 但实发长度不足（归为 request_failed）的场景，
本文件覆盖响应**不声明** Content-Length、也不使用
Transfer-Encoding: chunked，仅以 ``Connection: close`` 后连接
正常关闭（EOF）来标记正文结束的既有 HTTP/1.1 行为。受控
127.0.0.1 服务在收完完整请求头后发送 ``HTTP/1.1 200 OK`` 响应头
（含 Connection: close，不含任何正文长度/分帧头）与正文，
随后正常关闭连接；两个场景的响应头完全相同，差异只在正文：

1. 正文为完整的 15 字节 ``{"status":"ok"}``：EOF 界定出的正文
   可解析为 JSON 对象，公开 run 入口应退出 0，error 为 null，
   总体及两项检查的 passed 均为 true，状态码 actual 为 200，
   字段 actual 为 "ok"，present 为 true；
2. 正文只去掉最后的右花括号，实发 14 字节 ``{"status":"ok"``：
   传输本身正常完成（EOF 正常结束正文，不属于传输失败），但
   正文不是合法 JSON，应退出 1，error 为 invalid_response——
   不能归为 request_failed；状态码 actual 仍为 200 且状态检查
   通过，字段 actual 与 present 均为 null，字段检查与总体
   passed 为 false，也不能仅凭正文前缀 ``"status":"ok"`` 推断
   字段检查通过。

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

from api_workbench.runner import INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 场景一：完整正文，15 个 ASCII 字节，可解析为合法 JSON 对象
BODY_COMPLETE = b'{"status":"ok"}'
assert len(BODY_COMPLETE) == 15

# 场景二：只去掉最后的右花括号，14 个 ASCII 字节，JSON 不完整
BODY_TRUNCATED = b'{"status":"ok"'
assert len(BODY_TRUNCATED) == 14


class _EofServer:
    """不声明正文长度、发送后正常关闭连接的受控 127.0.0.1 服务。

    收到 GET 后发送 200 响应头（Connection: close，不含
    Content-Length 与 Transfer-Encoding）与构造参数指定的正文，
    随后正常关闭连接：客户端据此以 EOF 界定正文结束。
    监听保持到 close()，以便统计整个运行期间的 GET 次数
    （验证没有重试）。
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
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
            # 先完整读取请求头（以空行结束），确认服务确实收到了 GET
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

            # 不发送 Content-Length，也不发送 Transfer-Encoding：
            # 正文边界只由随后的连接正常关闭（EOF）界定
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                + self._body
            )
        except OSError:
            pass
        finally:
            # 发送完毕后正常关闭连接：对客户端而言正文随 EOF 结束，
            # 这是正常的分帧结束而非传输中断
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


class _EofFlowTestCase(unittest.TestCase):
    """公共设施：临时端口服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(
        self, *, body: bytes
    ) -> tuple[dict, subprocess.CompletedProcess]:
        """按给定正文启动服务、写临时用例、经公开入口执行，返回 (报告, 进程结果)。

        用例沿用 cases/health.json 的名称与断言（name=health、
        expected_status=200、field=status、expected_value="ok"），
        仅将 url 改为受控临时端口上的 /health。
        """
        server = _EofServer(body)
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


class CompleteBodyTests(_EofFlowTestCase):
    """完整正文 ``{"status":"ok"}`` 随连接正常关闭结束：整体通过。"""

    def test_complete_body_delimited_by_eof_passes(self) -> None:
        report, completed = self._execute(body=BODY_COMPLETE)

        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(report["error"])

        # 报告保留用例名称与期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 状态检查：EOF 正常分帧，状态码可用
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])

        # 字段检查：完整正文可解析，字段存在且值匹配
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


class TruncatedBodyTests(_EofFlowTestCase):
    """正文缺少最后的右花括号 ``{"status":"ok"``：invalid_response。"""

    def test_truncated_body_delimited_by_eof_is_invalid_response(self) -> None:
        report, completed = self._execute(body=BODY_TRUNCATED)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(report["error"], "invalid_response")
        # EOF 正常结束正文，属于内容无效而非传输失败
        self.assertNotEqual(report["error"], "request_failed")
        self.assertNotIn(
            report["error"], ("request_failed", "assertion_failed")
        )

        # 报告保留用例名称与期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 响应头完整收到：状态码 actual 仍为 200，状态检查通过
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])

        # 正文不是合法 JSON：不能从正文前缀推断字段，
        # actual 与 present 均为 null，字段检查失败
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
                "error": "invalid_response",
                "status_check": {
                    "expected": 200,
                    "actual": 200,
                    "passed": True,
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
