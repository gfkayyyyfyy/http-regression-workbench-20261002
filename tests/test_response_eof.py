"""未声明正文长度、以连接关闭结束正文的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证响应既不发送
Content-Length 也不发送 Transfer-Encoding、正文以正常关闭连接
（Connection: close）结束时的既有行为。用例沿用 cases/health.json
的名称与断言（expected_status 200、字段 status、期望值 "ok"），
仅将 url 改为临时端口上的 /health。两种场景的响应头完全相同，
差异仅在正文：

1. 完整正文：发送 15 个 ASCII 字节 ``{"status":"ok"}`` 后正常关闭
   连接。正文可解析为合法 JSON 对象，结果应为退出码 0，error 为
   null，总体及两项检查的 passed 均为 true，状态码 actual 为 200，
   字段 actual 为 "ok"，present 为 true；
2. 截断正文：只去掉正文最后的右花括号，实发 ``{"status":"ok"``
   后同样正常关闭连接。连接是正常关闭而非传输错误，不能归为
   request_failed；正文无法解析为合法 JSON，结果应为退出码 1，
   error 为 invalid_response，状态码 actual 仍为 200 且该检查通过，
   字段 actual 与 present 均为 null，字段检查及总体 passed 为
   false——不得从正文前缀推断字段通过。

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

from api_workbench.runner import INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 完整正文：15 个 ASCII 字节，可解析为合法 JSON 对象
COMPLETE_BODY = b'{"status":"ok"}'
assert len(COMPLETE_BODY) == 15

# 截断正文：只去掉完整正文最后的右花括号，连接仍正常关闭
TRUNCATED_BODY = b'{"status":"ok"'
assert TRUNCATED_BODY == COMPLETE_BODY[:-1]


class _EofClosingServer:
    """不声明正文长度、以正常关闭连接结束正文的受控 127.0.0.1 服务。

    收到完整 GET 请求头后发送 200 响应头（Connection: close，
    不含 Content-Length 与 Transfer-Encoding）与构造参数给定的
    正文，随后正常关闭连接；监听保持到 close()，以便统计整个
    运行期间的 GET 次数（验证没有重试）。
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
                b"Connection: close\r\n"
                b"\r\n"
                + self._body
            )
            # 发送完毕后正常关闭连接：未声明长度时，连接关闭即正文结束
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


class _EofBodyFlowTestCase(unittest.TestCase):
    """公共设施：临时端口服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(self, *, body: bytes) -> tuple[dict, subprocess.CompletedProcess]:
        """按给定正文启动服务、写用例、经公开入口执行，返回 (报告, 进程结果)。"""
        server = _EofClosingServer(body)
        server.start()
        self.addCleanup(server.close)
        self.server = server

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        # 沿用 cases/health.json 的名称与断言，仅 url 指向临时端口
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


class CompleteBodyTests(_EofBodyFlowTestCase):
    """完整正文：发送 {"status":"ok"} 后以正常关闭连接结束正文。"""

    def test_complete_body_until_close_passes(self) -> None:
        report, completed = self._execute(body=COMPLETE_BODY)

        # 比较按解析后的对象进行，不依赖键序及缩进
        expected = {
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
        }
        self.assertEqual(report, expected)
        self.assertEqual(completed.returncode, 0)
        self.assertIsNone(report["error"])
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["field_check"]["present"])
        self.assertTrue(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )


class TruncatedBodyTests(_EofBodyFlowTestCase):
    """截断正文：只去掉最后的右花括号，连接仍正常关闭。"""

    def test_truncated_body_is_invalid_response(self) -> None:
        report, completed = self._execute(body=TRUNCATED_BODY)

        expected = {
            "name": "health",
            "passed": False,
            "error": INVALID_RESPONSE,
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
        }
        self.assertEqual(report, expected)
        self.assertEqual(completed.returncode, 1)
        # 连接正常关闭、状态码已完整收到：这是正文内容无效，
        # 不能归为 request_failed 或 assertion_failed
        self.assertEqual(report["error"], "invalid_response")
        self.assertNotIn(report["error"], ("request_failed", "assertion_failed"))
        # 报告原样保留用例名称、字段名称及期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        # 正文无法解析：不能从前缀推断字段通过，actual 与 present 均为 null
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(
            self.server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )


if __name__ == "__main__":
    unittest.main()
