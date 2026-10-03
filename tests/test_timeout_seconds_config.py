"""可选 timeout_seconds 配置的校验与生效回归测试。

源码已支持用例中的可选 ``timeout_seconds``（每次网络阻塞等待的上限，
未提供时沿用默认三秒，只接受 0.1 至 30 的有限 JSON 数字）。本文件
通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 合法配置被接受：省略时仍为三秒；显式提供 0.1、30、1、0.5 时
   均被接受。及时返回 ``{"status":"ok"}`` 的服务得到退出码 0、
   passed 为 true、error 为 null；
2. 非法配置被拒绝：true、false、字符串 "1"、null、数组、对象、
   0、负数、0.09、30.1 以及 NaN、Infinity、-Infinity。退出码为 2，
   stdout 为空，stderr 以 ``api_workbench:`` 开头并包含
   ``timeout_seconds``，不出现 Traceback；服务收到的请求数为零，
   不把无效配置转换或回退为默认值；
3. 自定义超时真实生效：配置 0.5 秒，服务收到 GET 后保持连接但
   不发送响应头，命令以退出码 1 结束，stderr 为空，stdout 只有
   一个可解析的 JSON 报告（error 为 request_failed，两项 actual
   均为 null，所有 passed 均为 false，用例名称与期望值原样保留）。
   以服务收到 GET 到命令结束的等待为准，接受 0.3 至 2 秒——
   若实现忽略配置回退到三秒，等待会超出两秒上限而被发现；
   服务只收到一次 GET，超时后没有重试。

用例沿用 cases/health.json 的格式，仅 url 指向受控服务的临时端口。
测试使用系统分配的临时端口与临时用例文件，不依赖固定 8765、
不需要手工启动服务或访问公网；测试结束释放服务、连接与临时文件，
重复执行不留下占用端口的进程。
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
from http.server import BaseHTTPRequestHandler, HTTPServer

from api_workbench.runner import (
    REQUEST_FAILED,
    REQUEST_TIMEOUT,
    CaseError,
    load_case,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 0.5 秒自定义超时的等待窗口（服务收到 GET 到命令结束）：
# 下限排除连接失败等原因立即退出，上限发现回退到三秒的情况
CUSTOM_TIMEOUT = 0.5
CUSTOM_WAIT_MIN = 0.3
CUSTOM_WAIT_MAX = 2.0

# 用例中省略 timeout_seconds 键的哨兵（None 本身是要被拒绝的显式 null）
_OMIT = object()

# 应被接受的显式配置（含省略时的默认值）
ACCEPTED_TIMEOUTS = [_OMIT, 0.1, 30, 1, 0.5]

# 应被拒绝的配置：类型不符（布尔、字符串、null、数组、对象）、
# 越界（0、负数、0.09、30.1）与非有限数字（NaN、±Infinity）
REJECTED_TIMEOUTS = [
    True,
    False,
    "1",
    None,
    [],
    {},
    0,
    -1,
    -0.5,
    0.09,
    30.1,
    float("nan"),
    float("inf"),
    float("-inf"),
]


def _write_case(tmpdir: str, url: str, timeout=_OMIT, name: str = "health") -> str:
    """按 cases/health.json 同构格式写临时用例，仅在 timeout 非省略时加入该键。

    NaN / Infinity 经 json.dump 写成未加引号的字面量（非标准 JSON），
    与手工编辑用例文件的效果一致。
    """
    case = {
        "name": name,
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    if timeout is not _OMIT:
        case["timeout_seconds"] = timeout
    case_path = os.path.join(tmpdir, "case.json")
    with open(case_path, "w", encoding="utf-8") as handle:
        json.dump(case, handle, ensure_ascii=False)
    return case_path


class _CountingHandler(BaseHTTPRequestHandler):
    """记录 GET 次数并及时返回 {"status":"ok"} 的受控健康检查服务。"""

    get_count = 0
    _lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
        body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class _HealthServer:
    """在 127.0.0.1 临时端口上运行受控健康检查服务的上下文管理。"""

    def __init__(self) -> None:
        # 计数器是处理器类属性，跨实例共享；每个服务实例启动前重置，
        # 避免不同测试类之间累积
        _CountingHandler.get_count = 0
        self.httpd = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_HealthServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def get_count(self) -> int:
        return _CountingHandler.get_count


class _HangingServer:
    """收到 GET 后保持连接但不发送响应头的受控 127.0.0.1 服务（原始套接字）。

    记录每次收到 GET 的单调时间，供测试以服务收到请求为起点衡量等待时长。
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_times: list[float] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._connections: list[socket.socket] = []
        self._handlers: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def get_count(self) -> int:
        with self._lock:
            return len(self.get_times)

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
                self.get_times.append(time.monotonic())

            # 保持连接且不发送任何响应字节，
            # 直到客户端因超时断开（recv 返回空）或测试结束
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


class _CommandTestCase(unittest.TestCase):
    """公共设施：临时用例目录与经公开入口执行命令。"""

    def setUp(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmpdir = tmpdir.name

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


class AcceptedTimeoutTests(_CommandTestCase):
    """合法配置：省略或显式提供 0.1/30/1/0.5 均被接受并正常执行。"""

    def test_load_case_resolves_effective_timeout(self) -> None:
        with _HealthServer() as server:
            url = f"http://127.0.0.1:{server.port}/health"
            for timeout in ACCEPTED_TIMEOUTS:
                with self.subTest(timeout=timeout):
                    case_path = _write_case(self.tmpdir, url, timeout)
                    case = load_case(case_path)
                    expected = REQUEST_TIMEOUT if timeout is _OMIT else float(timeout)
                    self.assertEqual(case["timeout_seconds"], expected)
                    self.assertIsInstance(case["timeout_seconds"], float)

    def test_omitted_timeout_keeps_three_second_default(self) -> None:
        self.assertEqual(
            REQUEST_TIMEOUT, 3.0, "产品的默认请求超时设置必须保持为 3 秒"
        )
        with _HealthServer() as server:
            case_path = _write_case(
                self.tmpdir, f"http://127.0.0.1:{server.port}/health"
            )
            case = load_case(case_path)
            self.assertEqual(case["timeout_seconds"], 3.0)

    def test_accepted_timeouts_pass_against_healthy_service(self) -> None:
        with _HealthServer() as server:
            url = f"http://127.0.0.1:{server.port}/health"
            for timeout in ACCEPTED_TIMEOUTS:
                with self.subTest(timeout=timeout):
                    case_path = _write_case(self.tmpdir, url, timeout)
                    completed, _ = self._run_command(case_path)
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stderr.decode("utf-8"),
                    )
                    self.assertEqual(completed.stderr, b"")
                    report = json.loads(completed.stdout.decode("utf-8"))
                    self.assertIsNone(report["error"])
                    self.assertTrue(report["passed"])
                    self.assertEqual(report["status_check"]["actual"], 200)
                    self.assertTrue(report["status_check"]["passed"])
                    self.assertEqual(report["field_check"]["actual"], "ok")
                    self.assertTrue(report["field_check"]["passed"])
            self.assertEqual(
                server.get_count,
                len(ACCEPTED_TIMEOUTS),
                "每个合法用例应各发送一次 GET",
            )


class RejectedTimeoutTests(_CommandTestCase):
    """非法配置：命令拒绝执行，不发送任何请求，不做转换或默认值回退。"""

    def test_load_case_raises_case_error(self) -> None:
        with _HealthServer() as server:
            url = f"http://127.0.0.1:{server.port}/health"
            for timeout in REJECTED_TIMEOUTS:
                with self.subTest(timeout=repr(timeout)):
                    case_path = _write_case(self.tmpdir, url, timeout)
                    with self.assertRaises(CaseError) as context:
                        load_case(case_path)
                    self.assertIn("timeout_seconds", str(context.exception))
            self.assertEqual(server.get_count, 0, "非法用例不得触发任何请求")

    def test_invalid_timeouts_rejected_as_subprocess(self) -> None:
        with _HealthServer() as server:
            url = f"http://127.0.0.1:{server.port}/health"
            for timeout in REJECTED_TIMEOUTS:
                with self.subTest(timeout=repr(timeout)):
                    case_path = _write_case(self.tmpdir, url, timeout)
                    completed, _ = self._run_command(case_path)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stdout, b"", "非法用例不得输出报告")
                    stderr = completed.stderr.decode("utf-8")
                    self.assertTrue(
                        stderr.startswith("api_workbench:"),
                        f"stderr 应沿用 api_workbench: 前缀: {stderr!r}",
                    )
                    self.assertIn("timeout_seconds", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(
                        server.get_count,
                        0,
                        f"配置 {timeout!r} 非法时不得建立连接、不得回退默认值",
                    )


class CustomTimeoutEnforcementTests(_CommandTestCase):
    """真实生效：0.5 秒配置决定挂起请求的等待上限，而非沿用三秒。"""

    def test_custom_timeout_bounds_wait_for_stalled_response(self) -> None:
        server = _HangingServer()
        server.start()
        self.addCleanup(server.close)

        case_path = _write_case(
            self.tmpdir,
            f"http://127.0.0.1:{server.port}/resource",
            CUSTOM_TIMEOUT,
            name="timeout-custom-half-second",
        )
        completed, ended = self._run_command(case_path)

        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )
        # 以服务收到 GET 到命令结束的等待为准：0.5 秒配置应落在
        # 0.3 至 2 秒之间；若实现忽略配置回退到三秒，等待会超过两秒
        wait = ended - server.get_times[0]
        self.assertGreaterEqual(
            wait,
            CUSTOM_WAIT_MIN,
            f"命令在服务收到 GET 后 {wait:.2f} 秒即结束，未体现 0.5 秒超时",
        )
        self.assertLessEqual(
            wait,
            CUSTOM_WAIT_MAX,
            f"服务收到 GET 后等待 {wait:.2f} 秒，超出 0.5 秒配置的合理误差，"
            "疑似忽略配置回退到三秒",
        )

        self.assertEqual(completed.returncode, 1)
        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(stderr, "", f"stderr 必须为空: {stderr!r}")
        self.assertNotIn("Traceback", stderr)

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        expected = {
            "name": "timeout-custom-half-second",
            "passed": False,
            "error": REQUEST_FAILED,
            "status_check": {"expected": 200, "actual": None, "passed": False},
            "field_check": {
                "field": "status",
                "expected": "ok",
                "actual": None,
                "present": None,
                "passed": False,
            },
        }
        self.assertEqual(report, expected)


if __name__ == "__main__":
    unittest.main()
