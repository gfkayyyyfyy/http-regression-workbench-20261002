"""正文等待阶段使用自定义 timeout_seconds 的端到端回归测试。

现有测试已覆盖默认三秒的正文超时与自定义配置下的响应头超时；
本文件补充正文等待阶段：用例沿用 cases/health.json 的字段，仅将
url 指向受控 127.0.0.1 服务的临时端口，并设 timeout_seconds 为 0.5。
服务收到 GET 后立即发送 200 响应头（声明 Content-Length: 15）与
正文前缀 ``{"status":``（9 字节），随后分两种场景：

1. 正文等待超时：服务发完前缀后保持连接、不补齐正文。命令应在
   正文等待超时后以退出码 1 结束，error 为 request_failed——
   即使已收到 200 响应头也整体按请求失败处理，不把正文不完整
   归为 invalid_response；两项 actual 与 field_check.present
   均为 null，所有 passed 为 false，用例名称、字段名与两项
   期望值原样保留。以服务发完前缀到命令结束计时，接受 0.3 至
   2 秒：下限排除连接失败等原因立即退出，上限发现忽略配置而
   回退到三秒的情况；
2. 成功对照：相同配置与响应头，服务发完前缀后等 0.1 秒再发送
   ``"ok"}``，组成完整的 ``{"status":"ok"}``。命令应以退出码 0
   结束，error 为 null，两项 actual 分别为 200 和 "ok"，
   field_check.present 为 true，所有 passed 为 true。

两种场景的 stdout 都只能包含一个可解析的 UTF-8 JSON 报告，
stderr 为空、不出现 Traceback；每次执行服务只收到一次 GET，
没有重试。单次命令超过十秒仍未结束即判失败并终止该进程。

服务使用系统分配的临时端口，用例写入临时文件，不占用示例服务的
固定 8765、不需要手工启动服务或访问公网；测试结束释放服务、
连接与临时文件，重复执行不留下占用端口的进程。产品行为、公开
入口、默认超时与校验规则均保持不变。
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

# 用例的自定义超时与正文等待窗口（服务发完前缀到命令结束）：
# 下限排除连接失败等原因立即退出，上限发现忽略配置回退到三秒的情况
CUSTOM_TIMEOUT = 0.5
BODY_WAIT_MIN = 0.3
BODY_WAIT_MAX = 2.0

# 成功对照中服务发完前缀后补齐正文的延迟，远小于 0.5 秒超时
SUCCESS_COMPLETION_DELAY = 0.1

# 声明的正文长度与前缀：前缀 9 字节，余下 ``"ok"}`` 6 字节，
# 拼合为完整的 15 字节 ``{"status":"ok"}``
DECLARED_LENGTH = 15
BODY_PREFIX = b'{"status":'
BODY_REMAINDER = b'"ok"}'
assert len(BODY_PREFIX) + len(BODY_REMAINDER) == DECLARED_LENGTH

RESPONSE_HEADERS = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json; charset=utf-8\r\n"
    + f"Content-Length: {DECLARED_LENGTH}\r\n".encode("ascii")
    + b"\r\n"
)


class _PartialBodyServer:
    """发送 200 响应头与正文前缀后受控行为的 127.0.0.1 服务（原始套接字）。

    收到 GET 后立即发送声明 Content-Length: 15 的响应头与 9 字节前缀
    ``{"status":``，记录发完前缀的单调时间；随后按构造参数：

    - complete=False：保持连接、不补齐正文，直到客户端因正文等待
      超时断开（recv 返回空）或测试结束；
    - complete=True：等待 SUCCESS_COMPLETION_DELAY 秒后发送余下的
      ``"ok"}``，组成完整正文，随后正常关闭连接。

    监听保持到 close()，以便统计整个运行期间的 GET 次数（验证没有重试）。
    """

    def __init__(self, *, complete: bool) -> None:
        self._complete = complete
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

            # 立即发送 200 响应头与正文前缀，记录发完前缀的时间，
            # 供测试以此为起点衡量正文等待时长
            connection.sendall(RESPONSE_HEADERS + BODY_PREFIX)
            with self._lock:
                self.prefix_sent_times.append(time.monotonic())

            if self._complete:
                # 成功对照：稍作延迟后补齐正文并正常关闭连接
                time.sleep(SUCCESS_COMPLETION_DELAY)
                connection.sendall(BODY_REMAINDER)
                return

            # 超时场景：保持连接、不补齐正文，直到客户端因正文等待
            # 超时断开（recv 返回空）或测试结束
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
    """公共设施：临时端口服务、临时用例文件、子进程执行与单报告解析。"""

    def _execute(
        self, *, complete: bool
    ) -> tuple[dict, subprocess.CompletedProcess, _PartialBodyServer, float]:
        """启动服务、写用例、经公开入口执行。

        返回 (报告, 进程结果, 服务, 命令结束的单调时间)。
        """
        server = _PartialBodyServer(complete=complete)
        server.start()
        self.addCleanup(server.close)

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        # 沿用 cases/health.json 的字段，仅 url 指向受控服务的临时端口，
        # 并设 timeout_seconds 为 0.5
        case = {
            "name": "health",
            "url": f"http://127.0.0.1:{server.port}/health",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
            "timeout_seconds": CUSTOM_TIMEOUT,
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
        ended = time.monotonic()

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
        return report, completed, server, ended


class BodyWaitTimeoutTests(_BodyTimeoutFlowTestCase):
    """正文等待超时：服务发完前缀后保持连接、不补齐正文。"""

    def test_incomplete_body_is_request_failed_with_custom_timeout(self) -> None:
        report, completed, server, ended = self._execute(complete=False)

        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )
        # 以服务发完前缀到命令结束的等待为准：0.5 秒配置应落在
        # 0.3 至 2 秒之间；若实现忽略配置回退到三秒，等待会超过两秒
        self.assertEqual(len(server.prefix_sent_times), 1)
        wait = ended - server.prefix_sent_times[0]
        self.assertGreaterEqual(
            wait,
            BODY_WAIT_MIN,
            f"命令在服务发完前缀后 {wait:.2f} 秒即结束，未体现 0.5 秒正文超时",
        )
        self.assertLessEqual(
            wait,
            BODY_WAIT_MAX,
            f"服务发完前缀后等待 {wait:.2f} 秒，超出 0.5 秒配置的合理误差，"
            "疑似忽略配置回退到三秒",
        )

        self.assertEqual(completed.returncode, 1)
        # 即使已收到 200 响应头，正文不完整也整体按请求失败处理，
        # 不归入 invalid_response 或 assertion_failed
        expected = {
            "name": "health",
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
                "present": None,
            },
        }
        self.assertEqual(report, expected)
        self.assertEqual(report["error"], "request_failed")
        self.assertNotIn(report["error"], ("invalid_response", "assertion_failed"))
        # 报告原样保留用例名称、字段名称及两项期望值
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])


class BodyCompletedTests(_BodyTimeoutFlowTestCase):
    """成功对照：服务发完前缀后等 0.1 秒补齐正文，组成完整响应。"""

    def test_completed_body_within_timeout_passes(self) -> None:
        report, completed, server, _ = self._execute(complete=True)

        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，不得重试"
        )
        self.assertEqual(completed.returncode, 0)
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
        self.assertIsNone(report["error"])
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertTrue(report["status_check"]["passed"])
        self.assertEqual(report["field_check"]["actual"], "ok")
        self.assertTrue(report["field_check"]["present"])
        self.assertTrue(report["field_check"]["passed"])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
