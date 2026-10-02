"""可选 timeout_seconds 配置的校验与实际生效回归测试（仅标准库与受控本地服务）。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证源码已支持、但此前没有
回归覆盖的两点：

1. 配置校验：用例沿用 cases/health.json 格式（200 / status / ok），
   仅把 url 指向受控服务并增删 timeout_seconds。
   - 省略该字段时仍使用产品默认三秒，及时返回 {"status":"ok"} 的服务
     得到退出码 0、passed 为 true、error 为 null；
   - 显式给出 0.1、30、1、0.5（含端点、整数与小数）均被接受；
   - true、false、字符串 "1"、null、数组、对象一律拒绝，0、负数、
     0.09、30.1 以及非标准常量 NaN、Infinity、-Infinity 也拒绝。
   拒绝时退出码为 2、stdout 为空、stderr 以 ``api_workbench:`` 开头且
   包含 timeout_seconds、不出现 Traceback；服务收到的 GET 次数为零，
   无效配置不得被类型转换或回退为默认值。

2. 实际生效：timeout_seconds 为 0.5 时，服务收到 GET 后保持连接却不
   发送响应头，命令必须以约 0.5 秒（接受 0.3 至 2 秒窗口）结束，退出码
   1、stderr 为空、stdout 只有一个 request_failed JSON 报告，两项
   actual 均为 null、所有 passed 均为 false，名称、预期状态码、字段及
   预期值原样保留；服务只收到一次 GET，超时后没有重试。2 秒上限足以
   发现忽略配置而等待默认三秒的回退。该配置是每次网络阻塞等待的上限
   （HTTPConnection 的 socket timeout），不是整个命令的总时限。

服务使用系统分配的临时端口，用例写入临时文件，不依赖固定 8765、
不需要手工启动服务或访问公网；测试结束释放服务、端口、连接与临时文件，
重复执行不留下占用资源。
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

from api_workbench.runner import REQUEST_FAILED, REQUEST_TIMEOUT

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单次命令的硬上限：超过即判测试失败并终止该进程
COMMAND_HARD_LIMIT = 10.0

# 0.5 秒自定义超时的实测接受窗口：
# 下限排除连接失败等立即退出，上限排除回退默认三秒
CUSTOM_TIMEOUT_LOWER = 0.3
CUSTOM_TIMEOUT_UPPER = 2.0

# 类型无效：布尔、字符串、null、数组、对象（不得做类型转换或默认值回退）
INVALID_TYPE_CASES = [
    ("true", "true"),
    ("false", "false"),
    ("string", '"1"'),
    ("null", "null"),
    ("array", "[]"),
    ("object", "{}"),
]

# JSON 数字但超出 0.1–30（含端点）范围：0、负数、过小、过大
INVALID_RANGE_CASES = [
    ("zero", "0"),
    ("negative", "-1"),
    ("too_small", "0.09"),
    ("too_large", "30.1"),
]

# Python json 默认接受的非标准常量：加载用例时必须在区间校验处拒绝
INVALID_CONSTANT_CASES = [
    ("nan", "NaN"),
    ("infinity", "Infinity"),
    ("negative_infinity", "-Infinity"),
]

# 合法配置：下端点、上端点、整数与小数；另加省略（None）走默认三秒
VALID_TIMEOUTS = [0.1, 30, 1, 0.5]


def _write_case(raw_timeout: str | None, url: str) -> str:
    """生成与 cases/health.json 同构的临时用例，仅变更 url 与 timeout_seconds。

    raw_timeout 为 None 时省略该字段；否则按原始 JSON 文本写入，
    以精确保留 true/"1"/null/[]/NaN 等类型与字面量。
    """
    fields = {
        "name": "health",
        "url": url,
        "expected_status": 200,
        "field": "status",
        "expected_value": "ok",
    }
    lines = [
        f'  "{key}": {json.dumps(value, ensure_ascii=False)}'
        for key, value in fields.items()
    ]
    if raw_timeout is not None:
        lines.append(f'  "timeout_seconds": {raw_timeout}')
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("{\n" + ",\n".join(lines) + "\n}\n")
    return path


class _OkHandler(BaseHTTPRequestHandler):
    """记录 GET 次数的受控服务：固定及时返回 200 与 {"status":"ok"}。"""

    get_count = 0
    _lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls.get_count = 0

    @classmethod
    def count(cls) -> int:
        with cls._lock:
            return cls.get_count

    def do_GET(self) -> None:  # noqa: N802
        with self.__class__._lock:
            self.__class__.get_count += 1
        body = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class _HoldingServer:
    """收到 GET 后保持连接、不发送响应头的受控 127.0.0.1 服务（原始套接字）。

    额外记录服务端完整收到 GET 请求头的时刻，供测试以
    “服务收到 GET 后到命令结束”的口径衡量实际等待，
    避免把进程启动与建连时间计入超时。
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.get_count = 0
        self.get_received_at: float | None = None
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
                self.get_received_at = time.monotonic()

            # 不发送任何响应字节并保持连接，直到客户端因超时断开
            # （recv 返回空或报错）或测试结束
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


def _run_cli(case_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "api_workbench", "run", case_path],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=COMMAND_HARD_LIMIT,
    )


def _expected_passing_report() -> dict:
    """及时 200 + status=ok 时 runner 应产出的完整报告。"""
    return {
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
        },
    }


def _expected_request_failed_report() -> dict:
    """请求失败（超时）时 runner 应产出的完整报告。"""
    return {
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
        },
    }


class TimeoutSecondsValidationTests(unittest.TestCase):
    """拒绝/接受规则经公开 CLI 端到端验证，每个测试类共用一个临时服务。"""

    def setUp(self) -> None:
        _OkHandler.reset()
        self.httpd = HTTPServer(("127.0.0.1", 0), _OkHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.port}/health"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _assert_rejected(self, raw_timeout: str, label: str) -> None:
        case_path = _write_case(raw_timeout, self.url)
        try:
            completed = _run_cli(case_path)
        finally:
            os.unlink(case_path)

        stderr = completed.stderr.decode("utf-8")
        self.assertEqual(
            completed.returncode, 2, f"{label}: 非法配置应以退出码 2 拒绝: {stderr!r}"
        )
        self.assertEqual(completed.stdout, b"", f"{label}: 非法用例不得输出 JSON 报告")
        self.assertTrue(
            stderr.startswith("api_workbench:"),
            f"{label}: stderr 应沿用 api_workbench: 前缀: {stderr!r}",
        )
        self.assertIn("timeout_seconds", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(
            _OkHandler.count(),
            0,
            f"{label}: timeout_seconds 非法时不得发送任何 GET",
        )

    def test_invalid_types_rejected(self) -> None:
        for label, raw_timeout in INVALID_TYPE_CASES:
            with self.subTest(label=label):
                _OkHandler.reset()
                self._assert_rejected(raw_timeout, label)

    def test_out_of_range_numbers_rejected(self) -> None:
        for label, raw_timeout in INVALID_RANGE_CASES:
            with self.subTest(label=label):
                _OkHandler.reset()
                self._assert_rejected(raw_timeout, label)

    def test_nonstandard_constants_rejected(self) -> None:
        # 用例文件加载本身允许 NaN/Infinity，因此必须由 timeout_seconds
        # 的有限性/区间校验拒绝，而不是让进程崩溃（无 Traceback）
        for label, raw_timeout in INVALID_CONSTANT_CASES:
            with self.subTest(label=label):
                _OkHandler.reset()
                self._assert_rejected(raw_timeout, label)

    def test_valid_timeouts_accepted_against_prompt_server(self) -> None:
        # 产品默认设置必须保持三秒，省略字段时走该默认值
        self.assertEqual(
            REQUEST_TIMEOUT, 3.0, "产品的请求超时默认设置必须保持为 3 秒"
        )
        values: list[tuple[str, str | None]] = [
            (f"explicit-{value}", str(value)) for value in VALID_TIMEOUTS
        ]
        values.append(("omitted-default", None))

        for label, raw_timeout in values:
            with self.subTest(label=label):
                _OkHandler.reset()
                case_path = _write_case(raw_timeout, self.url)
                try:
                    completed = _run_cli(case_path)
                finally:
                    os.unlink(case_path)

                stderr = completed.stderr.decode("utf-8")
                self.assertEqual(stderr, "", f"{label}: 合法用例 stderr 应为空: {stderr!r}")
                self.assertNotIn("Traceback", stderr)
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{label}: 及时成功的响应应以退出码 0 结束",
                )
                report = json.loads(completed.stdout.decode("utf-8"))
                self.assertEqual(
                    report, _expected_passing_report(), f"{label}: 报告结构不符"
                )
                self.assertIsNone(report["error"])
                self.assertTrue(report["passed"])
                self.assertTrue(report["status_check"]["passed"])
                self.assertTrue(report["field_check"]["passed"])
                self.assertEqual(
                    _OkHandler.count(),
                    1,
                    f"{label}: 每次执行应恰好发送一次 GET",
                )


class CustomTimeoutEffectTests(unittest.TestCase):
    """0.5 秒配置对挂起服务真实生效：约 0.5 秒后 request_failed，仅一次 GET。"""

    def test_half_second_timeout_caps_header_wait(self) -> None:
        server = _HoldingServer()
        server.start()
        self.addCleanup(server.close)

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        case_path = _write_case("0.5", f"http://127.0.0.1:{server.port}/resource")

        try:
            completed = _run_cli(case_path)
        except subprocess.TimeoutExpired:
            self.fail(f"单次命令超过 {COMMAND_HARD_LIMIT} 秒仍未结束")
        finally:
            os.unlink(case_path)
        finished_at = time.monotonic()

        # 以服务完整收到 GET 的时刻为等待起点（不含进程启动与建连）
        self.assertEqual(
            server.get_count, 1, "整个运行只应发送一次 GET，超时后不得重试"
        )
        self.assertIsNotNone(server.get_received_at, "服务必须确实收到过 GET")
        elapsed = finished_at - server.get_received_at
        self.assertGreaterEqual(
            elapsed,
            CUSTOM_TIMEOUT_LOWER,
            f"命令 {elapsed:.2f} 秒即结束，未体现 0.5 秒阻塞等待上限",
        )
        self.assertLessEqual(
            elapsed,
            CUSTOM_TIMEOUT_UPPER,
            f"命令等待了 {elapsed:.2f} 秒，疑似忽略配置而回退到默认三秒",
        )

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(stderr_text, "", f"stderr 必须为空: {stderr_text!r}")
        self.assertNotIn("Traceback", stderr_text)
        self.assertEqual(completed.returncode, 1)

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertEqual(report, _expected_request_failed_report())
        self.assertEqual(report["error"], REQUEST_FAILED)
        self.assertIsNone(report["status_check"]["actual"])
        self.assertIsNone(report["field_check"]["actual"])
        self.assertFalse(report["status_check"]["passed"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        # 名称、预期状态码、字段及预期值原样保留
        self.assertEqual(report["name"], "health")
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")

        # 超时后不得重试：进程已结束，给服务一点时间确认没有第二次 GET
        time.sleep(0.2)
        self.assertEqual(server.get_count, 1, "超时后不得重试发送第二次 GET")


if __name__ == "__main__":
    unittest.main()
