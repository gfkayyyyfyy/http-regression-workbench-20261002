"""小数舍入边界（JSON 数字解析精度）的端到端回归测试。

数字字段断言按 JSON 解析结果精确比较，本测试钉住 IEEE-754 双精度
舍入边界上的公开行为：

1. 期望 1（整数）与响应 ``{"count":1.0000000000000001}`` 应通过——
   该原始文本按双精度解析后恰为 1.0，与整数 1 相等；报告中
   field_check.expected 保持数字 1（int），actual 保持数字 1.0（float）；
2. 交换两侧的原始数字：期望 1.0（小数）与响应 ``{"count":1}`` 同样通过，
   expected 为数字 1.0（float），actual 为数字 1（int）；
3. 期望 1 与响应 ``{"count":1.0000000000000002}`` 必须失败——该文本解析为
   紧邻 1.0 上方的另一个双精度值；actual 原样保留解析后的数字，
   field_check.passed 与总 passed 为 false，error 为 assertion_failed，
   退出码为 1；
4. 期望 0 与响应 ``{"count":1e-400}`` 应通过——极小指数下溢为 0.0，
   仍是有限数值，不能归为 invalid_response；actual 为数字 0.0（float）。

全部场景通过临时端口上的受控 127.0.0.1 服务（200，路径 /count）与公开入口
``python -m api_workbench run case.json`` 验证从 UTF-8 用例文件、
本地 HTTP 响应到 stdout 报告的完整链路：每个场景只发送一次 GET，
stdout 有且仅有一个可按 UTF-8 解析的 JSON 报告，stderr 为空，
状态码检查通过，field_check.present 为 true，报告中的数字保持
JSON 数值类型（整数/小数不混淆）。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
用例文件写入临时目录并在结束时释放，不需要手工启动示例服务或访问公网。
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

from api_workbench.runner import ASSERTION_FAILED, INVALID_RESPONSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CASE_NAME = "number-precision"
FIELD_NAME = "count"
RESOURCE_PATH = "/count"


class _PrecisionServer(ThreadingHTTPServer):
    """按测试设定返回固定原始正文的受控服务（只服务 /count）。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PrecisionHandler)
        self.port = self.server_address[1]
        self.body = b"{}"
        self.get_count = 0
        self.requested_paths: list[str] = []
        self._lock = threading.Lock()

    def set_body(self, body: bytes) -> None:
        with self._lock:
            self.body = body
            self.get_count = 0
            self.requested_paths = []


class _PrecisionHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _PrecisionServer = self.server
        with server._lock:
            body = server.body
            server.get_count += 1
            server.requested_paths.append(self.path)
        # 所有场景的用例 url 都指向 /count；固定 200 与原始 JSON 正文
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


class NumberParsePrecisionTests(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与单报告解析。"""

    def setUp(self) -> None:
        self.server = _PrecisionServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_raw_case(self, raw_expected_value: str) -> str:
        """按原始 JSON 文本写用例文件，精确保留期望值的数字写法（1 与 1.0 不同）。

        文件按 UTF-8 编码写出；expected_value 不经 Python 数字中转，
        使 1.0 走 parse_float、1 走整数分支，与手工书写的用例完全一致。
        """
        path = os.path.join(self.tmpdir.name, "case.json")
        text = (
            "{\n"
            f'  "name": {json.dumps(CASE_NAME)},\n'
            f'  "url": "http://127.0.0.1:{self.server.port}{RESOURCE_PATH}",\n'
            '  "expected_status": 200,\n'
            f'  "field": {json.dumps(FIELD_NAME)},\n'
            f'  "expected_value": {raw_expected_value}\n'
            "}\n"
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _run(self, raw_expected_value: str, raw_response_body: bytes):
        """配置服务的原始响应正文、写原始文本用例，经公开入口执行一次。

        返回 (严格 UTF-8 解析出的单个报告, 进程结果)。
        """
        self.server.set_body(raw_response_body)
        case_path = self._write_raw_case(raw_expected_value)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        # stderr 必须为空（含无 Traceback）
        self.assertEqual(
            completed.stderr,
            b"",
            f"stderr 必须为空: {completed.stderr.decode('utf-8', 'replace')!r}",
        )

        # stdout 必须可按 UTF-8 严格解析，且有且仅有一个 JSON 报告
        stdout_text = completed.stdout.decode("utf-8")
        report, end = json.JSONDecoder().raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _assert_single_get_to_count(self) -> None:
        self.assertEqual(self.server.get_count, 1, "整个运行只应发送一次 GET")
        self.assertEqual(
            self.server.requested_paths,
            [RESOURCE_PATH],
            "唯一一次 GET 必须发往用例指定的 /count",
        )

    def _assert_common_report_inputs(
        self, report: dict, *, expected_value, expected_type: type
    ) -> None:
        """所有场景共用：用例名、字段名、期望值对应输入；状态码检查通过；
        field_check.present 为 true；期望值保持输入侧的 JSON 数值类型。"""
        self.assertEqual(report["name"], CASE_NAME)
        self.assertEqual(report["status_check"]["expected"], 200)
        self.assertEqual(report["status_check"]["actual"], 200)
        self.assertIs(report["status_check"]["passed"], True)
        self.assertEqual(report["field_check"]["field"], FIELD_NAME)
        self.assertIs(
            type(report["field_check"]["expected"]),
            expected_type,
            "报告中的期望值必须保持原始 JSON 数值类型",
        )
        self.assertEqual(report["field_check"]["expected"], expected_value)
        self.assertIs(
            report["field_check"]["present"],
            True,
            "响应为合法对象且 count 键存在，present 必须为 true",
        )

    def test_expected_integer_one_matches_rounded_up_tail(self) -> None:
        # 期望 1（原始文本 "1"，int），响应 count 为 1.0000000000000001：
        # 该文本按双精度舍入后恰为 1.0，精确比较相等，断言通过。
        report, completed = self._run("1", b'{"count":1.0000000000000001}')

        self._assert_common_report_inputs(
            report, expected_value=1, expected_type=int
        )
        # actual 保持 JSON 小数类型：解析结果就是 1.0
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 1.0)
        self.assertIs(report["field_check"]["passed"], True)
        self.assertIs(report["passed"], True)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self._assert_single_get_to_count()

    def test_swapped_raw_numbers_float_expected_integer_actual(self) -> None:
        # 交换两边的原始数字：期望 1.0（原始文本 "1.0"，float），
        # 响应 count 为整数 1；同样通过，两侧类型各自保留。
        report, completed = self._run("1.0", b'{"count":1}')

        self._assert_common_report_inputs(
            report, expected_value=1.0, expected_type=float
        )
        self.assertIs(type(report["field_check"]["actual"]), int)
        self.assertEqual(report["field_check"]["actual"], 1)
        self.assertIs(report["field_check"]["passed"], True)
        self.assertIs(report["passed"], True)
        self.assertIsNone(report["error"])
        self.assertEqual(completed.returncode, 0)
        self._assert_single_get_to_count()

    def test_distinct_double_neighbor_fails_assertion(self) -> None:
        # 期望 1，响应 count 为 1.0000000000000002：它解析为紧邻 1.0
        # 上方的另一个双精度值，精确比较不等——断言失败而非无效响应。
        report, completed = self._run("1", b'{"count":1.0000000000000002}')

        self._assert_common_report_inputs(
            report, expected_value=1, expected_type=int
        )
        # actual 原样保留解析后的数字（仍是有限 JSON 小数）
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 1.0000000000000002)
        self.assertNotEqual(report["field_check"]["actual"], 1)
        self.assertIs(report["field_check"]["passed"], False)
        self.assertIs(report["passed"], False)
        self.assertEqual(report["error"], ASSERTION_FAILED)
        self.assertNotEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 1)
        self._assert_single_get_to_count()

    def test_underflow_to_zero_matches_integer_zero(self) -> None:
        # 期望 0，响应 count 为 1e-400：指数下溢得到 0.0，仍是有限数值，
        # 正文合法，必须按数值相等通过，绝不能归为 invalid_response。
        report, completed = self._run("0", b'{"count":1e-400}')

        self._assert_common_report_inputs(
            report, expected_value=0, expected_type=int
        )
        self.assertIs(type(report["field_check"]["actual"]), float)
        self.assertEqual(report["field_check"]["actual"], 0.0)
        self.assertIs(report["field_check"]["passed"], True)
        self.assertIs(report["passed"], True)
        self.assertIsNone(report["error"])
        self.assertNotEqual(report["error"], INVALID_RESPONSE)
        self.assertEqual(completed.returncode, 0)
        self._assert_single_get_to_count()


if __name__ == "__main__":
    unittest.main()
