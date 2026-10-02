"""不跟随重定向行为的独立端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证 run 入口执行单用例 GET 时
**不跟随重定向**：

* ``GET /redirect`` 返回 302，正文为 ``{"status":"redirect"}``，
  ``Location`` 分别取相对路径 ``/target`` 与同一服务的完整 HTTP 地址；
* ``GET /target`` 返回 200 和 ``{"status":"ok"}``，但任何场景下都不应被访问；
* 期望状态码为 200 时：报告保留实际状态码 302、状态码检查失败、
  字段检查通过（actual 为字符串 "redirect"）、总 passed 为 false、
  error 为 assertion_failed、退出码为 1；
* 仅把 expected_status 改为 302 后：两项检查与总 passed 均为 true、
  error 为 null、退出码为 0，仍不访问 /target。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，
不依赖固定 8765、不需要手工启动服务或访问公网；测试结束后释放端口并
清理自身临时文件。可通过以下命令独立执行：

    python -m unittest discover -s tests -p test_redirect_behavior.py
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
from urllib.parse import urlsplit

from api_workbench.runner import ASSERTION_FAILED

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REDIRECT_BODY = json.dumps({"status": "redirect"}, separators=(",", ":")).encode(
    "utf-8"
)
TARGET_BODY = json.dumps({"status": "ok"}, separators=(",", ":")).encode("utf-8")
NOT_FOUND_BODY = b"{}"

# 两种 Location 形态：相对路径与同一服务的完整 HTTP 地址（诊断标签）
KIND_LABELS = {
    "relative": "相对 Location（/target）",
    "absolute": "绝对 Location（完整 HTTP 地址）",
}


class _RedirectServer(ThreadingHTTPServer):
    """``/redirect`` 返回 302、``/target`` 返回 200 的受控服务。

    分别统计两个路径的 GET 次数，用于证明重定向未被跟随。
    """

    daemon_threads = True

    def __init__(self, location_kind: str) -> None:
        super().__init__(("127.0.0.1", 0), _RedirectHandler)
        self.port = self.server_address[1]
        self.location_kind = location_kind
        self.redirect_count = 0
        self.target_count = 0
        self._lock = threading.Lock()


class _RedirectHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-redirect-test/0.1"

    def _send_json(self, status_code: int, body: bytes, location: str | None) -> None:
        self.send_response(status_code)
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _RedirectServer = self.server
        path = urlsplit(self.path).path

        if path == "/redirect":
            with server._lock:
                server.redirect_count += 1
            if server.location_kind == "relative":
                location = "/target"
            else:
                location = f"http://127.0.0.1:{server.port}/target"
            # 302 正文为 {"status":"redirect"}，Location 指向 /target
            self._send_json(302, REDIRECT_BODY, location)
            return

        if path == "/target":
            with server._lock:
                server.target_count += 1
            self._send_json(200, TARGET_BODY, None)
            return

        self._send_json(404, NOT_FOUND_BODY, None)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


def _expected_report(*, name: str, expected_status: int, status_passed: bool) -> dict:
    """按 runner 的报告结构构造期望报告（整体内容比较，不依赖键顺序/缩进）。

    两种期望状态码下字段检查都通过（正文 status 为字符串 "redirect"），
    总 passed 仅随状态码检查变化。
    """
    return {
        "name": name,
        "passed": bool(status_passed),
        "error": None if status_passed else ASSERTION_FAILED,
        "status_check": {
            "expected": expected_status,
            "actual": 302,
            "passed": status_passed,
        },
        "field_check": {
            "field": "status",
            "expected": "redirect",
            "actual": "redirect",
            "passed": True,
        },
    }


class RedirectBehaviorTests(unittest.TestCase):
    """每个场景核对完整报告、退出码与两个路径的访问次数。"""

    location_kind = "relative"

    def setUp(self) -> None:
        self.server = _RedirectServer(self.location_kind)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _run_case(self, expected_status: int) -> tuple[dict, subprocess.CompletedProcess]:
        """写临时用例并经公开入口执行，返回 (唯一解析出的报告, 进程结果)。"""
        kind = self.location_kind
        case_name = f"redirect-{kind}-expect-{expected_status}"
        case = {
            "name": case_name,
            "url": f"http://127.0.0.1:{self.server.port}/redirect",
            "expected_status": expected_status,
            "field": "status",
            "expected_value": "redirect",
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

        label = KIND_LABELS[kind]
        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(
            completed.stderr,
            b"",
            f"{label}：stderr 必须为空，实际为 {stderr_text!r}",
        )
        self.assertNotIn("Traceback", stderr_text)

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"{label}：stdout 只能包含一个 JSON 报告，实际为 {stdout_text!r}",
        )
        self.assertIsInstance(report, dict)
        return report, completed

    def _assert_redirect_scenario(self, expected_status: int) -> None:
        """完整报告 + 退出码 + /redirect、/target 访问次数三项同时核对。"""
        kind = self.location_kind
        label = KIND_LABELS[kind]
        case_name = f"redirect-{kind}-expect-{expected_status}"
        expect_pass = expected_status == 302

        report, completed = self._run_case(expected_status)
        expected = _expected_report(
            name=case_name,
            expected_status=expected_status,
            status_passed=expect_pass,
        )

        # 完整报告比较：任何字段（含 name、检查字段、两项 expected）与输入不一致
        # 都会在这里暴露
        self.assertEqual(
            report,
            expected,
            f"{label}：报告内容与预期不符（expected_status={expected_status}）",
        )

        # 输入必须按原样保留在报告中
        self.assertEqual(report["name"], case_name, f"{label}：name 必须按输入保留")
        self.assertEqual(
            report["status_check"]["expected"],
            expected_status,
            f"{label}：状态码 expected 必须按输入保留",
        )
        self.assertEqual(
            report["field_check"]["field"],
            "status",
            f"{label}：检查字段必须按输入保留",
        )
        self.assertEqual(
            report["field_check"]["expected"],
            "redirect",
            f"{label}：字段 expected 必须按输入保留",
        )

        # 实际状态码始终是 302；字段实际值始终是字符串 "redirect"
        self.assertEqual(
            report["status_check"]["actual"],
            302,
            f"{label}：不跟随重定向时实际状态码必须保留为 302",
        )
        self.assertEqual(
            report["field_check"]["actual"],
            "redirect",
            f"{label}：字段实际值必须是 302 正文中的字符串 redirect",
        )
        self.assertIsInstance(report["field_check"]["actual"], str)
        self.assertTrue(report["field_check"]["passed"])

        if expect_pass:
            self.assertTrue(
                report["status_check"]["passed"],
                f"{label}：expected_status=302 时状态码检查应通过",
            )
            self.assertTrue(report["passed"], f"{label}：总 passed 应为 true")
            self.assertIsNone(report["error"], f"{label}：通过时 error 应为 null")
            expected_exit_code = 0
        else:
            self.assertFalse(
                report["status_check"]["passed"],
                f"{label}：expected_status=200 时状态码检查必须失败",
            )
            self.assertFalse(report["passed"], f"{label}：总 passed 应为 false")
            self.assertEqual(
                report["error"],
                ASSERTION_FAILED,
                f"{label}：状态码不符时 error 应为 assertion_failed",
            )
            expected_exit_code = 1

        self.assertEqual(
            completed.returncode,
            expected_exit_code,
            f"{label}：退出码应为 {expected_exit_code}，"
            f"实际为 {completed.returncode}",
        )

        with self.server._lock:
            redirect_count = self.server.redirect_count
            target_count = self.server.target_count
        self.assertEqual(
            redirect_count,
            1,
            f"{label}：每次执行只应向 /redirect 发送一次 GET，"
            f"实际 {redirect_count} 次",
        )
        self.assertEqual(
            target_count,
            0,
            f"{label}：不得跟随重定向访问 /target，实际访问 {target_count} 次",
        )


class RelativeLocationRedirectTests(RedirectBehaviorTests):
    """Location 为相对路径 /target。"""

    location_kind = "relative"

    def test_relative_expect_200_status_fails_field_passes(self) -> None:
        self._assert_redirect_scenario(expected_status=200)

    def test_relative_expect_302_both_checks_pass(self) -> None:
        self._assert_redirect_scenario(expected_status=302)


class AbsoluteLocationRedirectTests(RedirectBehaviorTests):
    """Location 为同一服务的完整 HTTP 地址。"""

    location_kind = "absolute"

    def test_absolute_expect_200_status_fails_field_passes(self) -> None:
        self._assert_redirect_scenario(expected_status=200)

    def test_absolute_expect_302_both_checks_pass(self) -> None:
        self._assert_redirect_scenario(expected_status=302)


if __name__ == "__main__":
    unittest.main()
