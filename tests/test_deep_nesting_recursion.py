"""响应正文嵌套过深触发解析递归上限时的报告回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 完整收到、但嵌套深度超出当前 Python 运行时 json 解析递归上限的正文
   （``status`` 位于正文开头且值已符合期望，``extra`` 把数字 0 包在
   数千层单元素数组内）一律判为 invalid_response：退出码 1、stderr 为空、
   stdout 仍只有单个可按 UTF-8 解码的 JSON 报告，不出现 Traceback；
   报告保留用例名称、字段名与两项期望值，顶层 passed 与
   field_check.passed 为 false，field_check.actual 与 present 为 null，
   status_check.actual 保留实际状态码并按与 expected_status 的比较
   决定 status_check.passed——即使目标字段位于正文开头且值相符，
   也不能跳过整份正文解析而判为成功；
2. 状态码不符（404）时仍为 invalid_response，仅状态码检查随之失败，
   与 request_failed（连接失败/超时/正文未收完整）保持区分；
3. 同样的字段布局、``extra`` 仅为两层嵌套数组时正常解析，
   两项检查通过、退出码 0（浅层嵌套语义不变）。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765、不需要手工启动服务或访问公网。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_workbench.runner import INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 嵌套深度起点：在 CPython 3.13 及更早版本的默认递归设置下，
# 2000 层已超出 json 解析的递归上限（默认限制 1000）。
_BASE_DEPTH = 2000
# 探测上限：防止未来运行时取消递归限制后探测循环无终止
_MAX_PROBE_DEPTH = 1 << 22


def _depth_exceeding_json_recursion_limit() -> int:
    """返回一个必然超出当前解释器 json 解析递归上限的嵌套深度。

    从 2000 层起在本进程内直接探测 json.loads（与子进程同一解释器、
    同样的默认递归设置）：若已抛出 RecursionError 则直接采用；
    否则（如 CPython 3.14 起 C 扫描器按真实栈空间而非递归计数约束，
    2000 层仍可解析）按倍数加深直至确认抛出，再留一倍余量，
    保证子进程中的解析必然触发 RecursionError。探测的是运行时实际
    行为，不向被测工具引入任何固定深度配置，也不改动递归上限。
    """
    depth = _BASE_DEPTH
    while depth <= _MAX_PROBE_DEPTH:
        try:
            json.loads("[" * depth + "0" + "]" * depth)
        except RecursionError:
            return depth * 2
        depth *= 2
    raise unittest.SkipTest("当前运行时的 json 解析无递归上限，无法构造深嵌套正文")


def _deep_body(depth: int) -> bytes:
    """构造 {"status":"ok","extra":[[[…0…]]]} 形式的完整 UTF-8 正文。

    status 在正文开头且值已符合期望；extra 把数字 0 包在 depth 层
    单元素数组内，使整份正文的解析超出递归上限。
    """
    return (
        b'{"status":"ok","extra":'
        + b"[" * depth
        + b"0"
        + b"]" * depth
        + b"}"
    )


class _ScenarioServer(ThreadingHTTPServer):
    """按测试设定返回固定状态码与原始正文的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ScenarioHandler)
        self.port = self.server_address[1]
        self.status_code = 200
        self.body = b"{}"
        self.get_count = 0
        self._lock = threading.Lock()

    def set_scenario(self, status_code: int, body: bytes) -> None:
        with self._lock:
            self.status_code = status_code
            self.body = body
            self.get_count = 0


class _ScenarioHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _ScenarioServer = self.server
        with server._lock:
            status_code = server.status_code
            body = server.body
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server._lock:
            server.get_count += 1

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class DeepNestingRecursionTests(unittest.TestCase):
    """深嵌套正文经公开入口的端到端行为。"""

    def setUp(self) -> None:
        self.server = _ScenarioServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.deep_depth = _depth_exceeding_json_recursion_limit()

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _run_case(self, *, name: str, response_status: int, response_body: bytes):
        """配置服务响应、写用例、经公开入口执行，返回 (解析后的报告, 进程结果)。"""
        self.server.set_scenario(response_status, response_body)
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/resource",
            "expected_status": 200,
            "field": "status",
            "expected_value": "ok",
        }
        case_path = os.path.join(self.tmpdir.name, "case.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

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

    def _assert_invalid_response_report(
        self,
        report: dict,
        completed: subprocess.CompletedProcess,
        *,
        name: str,
        status_actual: int,
        status_passed: bool,
    ) -> None:
        """整份报告与期望逐字段相等；状态码检查结果按实际比较保留。"""
        expected = {
            "name": name,
            "passed": False,
            "error": INVALID_RESPONSE,
            "status_check": {
                "expected": 200,
                "actual": status_actual,
                "passed": status_passed,
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
        # 用例的名称、字段名与两项期望值必须保留在报告中
        self.assertEqual(report["name"], name)
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["field_check"]["field"], "status")
        self.assertEqual(report["field_check"]["expected"], "ok")
        self.assertEqual(report["status_check"]["actual"], status_actual)
        self.assertIs(report["status_check"]["passed"], status_passed)
        self.assertIsNone(report["field_check"]["actual"])
        self.assertIsNone(report["field_check"]["present"])
        self.assertFalse(report["field_check"]["passed"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")

    def test_deep_nesting_with_matching_status_is_invalid_response(self) -> None:
        # status 位于正文开头且值已为 "ok"：仍不得跳过整份正文解析判成功
        report, completed = self._run_case(
            name="deep-nesting-status-match",
            response_status=200,
            response_body=_deep_body(self.deep_depth),
        )
        self._assert_invalid_response_report(
            report,
            completed,
            name="deep-nesting-status-match",
            status_actual=200,
            status_passed=True,
        )

    def test_deep_nesting_with_mismatched_status_keeps_status_result(self) -> None:
        # 状态 404 ≠ 期望 200：错误类别仍为 invalid_response，
        # 状态码检查保留实际值并判失败，不退化为 request_failed
        report, completed = self._run_case(
            name="deep-nesting-status-mismatch",
            response_status=404,
            response_body=_deep_body(self.deep_depth),
        )
        self._assert_invalid_response_report(
            report,
            completed,
            name="deep-nesting-status-mismatch",
            status_actual=404,
            status_passed=False,
        )

    def test_shallow_nesting_still_passes(self) -> None:
        # 同样的字段布局，extra 仅为两层嵌套数组：正常解析，两项检查通过
        report, completed = self._run_case(
            name="shallow-nesting-ok",
            response_status=200,
            response_body=b'{"status":"ok","extra":[[0]]}',
        )
        expected = {
            "name": "shallow-nesting-ok",
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
        self.assertTrue(report["passed"])
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")


if __name__ == "__main__":
    unittest.main()
