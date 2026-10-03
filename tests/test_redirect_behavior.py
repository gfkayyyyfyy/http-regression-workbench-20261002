"""重定向不跟随行为的端到端回归测试。

通过临时端口上的受控 127.0.0.1 服务与公开入口
``python -m api_workbench run case.json`` 验证：

1. 受控服务的 ``/redirect`` 返回 302、正文 ``{"status":"redirect"}``，
   ``Location`` 分别取相对路径 ``/target`` 与同一服务的完整 HTTP 地址；
   ``/target`` 返回 200 与 ``{"status":"ok"}``。
2. 每次执行只向 ``/redirect`` 发送一次 GET，``/target`` 的请求数为零
   （即不跟随重定向，无论 Location 是相对还是绝对形式）。
3. 用例期望状态码 200 时：报告保留实际状态码 302，状态码检查失败，
   字段检查通过且实际值为字符串 ``redirect``，总 passed 为 false，
   error 为 assertion_failed，退出码为 1。
4. 仅把 expected_status 改为 302 后：两项检查与总 passed 均为 true，
   error 为 null，退出码为 0，仍不访问 ``/target``。

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

from api_workbench.runner import ASSERTION_FAILED

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REDIRECT_BODY = b'{"status":"redirect"}'
TARGET_BODY = b'{"status":"ok"}'


class _RedirectServer(ThreadingHTTPServer):
    """提供 /redirect（302）与 /target（200）并按路径计数的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _RedirectHandler)
        self.port = self.server_address[1]
        # Location 形式："relative" -> /target；"absolute" -> 完整 HTTP 地址
        self.location_mode = "relative"
        self.path_counts = {"/redirect": 0, "/target": 0}
        self._lock = threading.Lock()

    def set_location_mode(self, mode: str) -> None:
        if mode not in ("relative", "absolute"):
            raise ValueError(f"未知 Location 形式: {mode!r}")
        with self._lock:
            self.location_mode = mode
            self.path_counts = {"/redirect": 0, "/target": 0}

    def record_hit(self, path: str) -> None:
        with self._lock:
            self.path_counts[path] = self.path_counts.get(path, 0) + 1

    def location_for(self, path: str) -> str:
        with self._lock:
            mode = self.location_mode
        if mode == "absolute":
            return f"http://127.0.0.1:{self.port}{path}"
        return path


class _RedirectHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-redirect-test/0.1"

    def _send_json(
        self, status_code: int, payload: bytes, extra_headers: tuple = ()
    ) -> None:
        self.send_response(status_code)
        for header, value in extra_headers:
            self.send_header(header, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _RedirectServer = self.server
        path = self.path.split("?", 1)[0]
        if path == "/redirect":
            server.record_hit("/redirect")
            location = server.location_for("/target")
            self._send_json(302, REDIRECT_BODY, (("Location", location),))
        elif path == "/target":
            server.record_hit("/target")
            self._send_json(200, TARGET_BODY)
        else:
            server.record_hit(path)
            self._send_json(404, b"{}")

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


def _expected_report(
    *,
    name: str,
    expected_status: int,
    status_actual: int,
    status_passed: bool,
    field_actual,
    field_present: bool | None,
    field_passed: bool,
    error: str | None,
) -> dict:
    """按 runner 的报告结构构造期望报告（整体内容比较，不依赖键顺序/缩进）。"""
    return {
        "name": name,
        "passed": bool(status_passed and field_passed),
        "error": error,
        "status_check": {
            "expected": expected_status,
            "actual": status_actual,
            "passed": status_passed,
        },
        "field_check": {
            "field": "status",
            "expected": "redirect",
            "actual": field_actual,
            "present": field_present,
            "passed": field_passed,
        },
    }


class RedirectNotFollowedTestCase(unittest.TestCase):
    """公共设施：临时端口受控服务、临时用例文件、子进程执行与单报告解析。"""

    # 子类/子测试用于区分诊断信息的 Location 形式标签
    location_mode = "relative"
    location_label = "相对 Location (/target)"

    def setUp(self) -> None:
        self.server = _RedirectServer()
        self.server.set_location_mode(self.location_mode)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _diag(self, detail: str) -> str:
        return f"[{self.location_label}] {detail}"

    def _run_case(self, *, name: str, expected_status: int) -> tuple[dict, int]:
        """写用例、经公开入口执行，返回 (解析后的报告, 退出码)。"""
        case = {
            "name": name,
            "url": f"http://127.0.0.1:{self.server.port}/redirect",
            "expected_status": expected_status,
            "field": "status",
            "expected_value": "redirect",
        }
        case_path = os.path.join(self.tmpdir.name, f"{name}.json")
        with open(case_path, "w", encoding="utf-8") as handle:
            json.dump(case, handle, ensure_ascii=False)

        completed = subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", case_path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

        stderr_text = completed.stderr.decode("utf-8")
        self.assertEqual(
            stderr_text, "", self._diag(f"stderr 必须为空: {stderr_text!r}")
        )
        self.assertNotIn("Traceback", stderr_text, self._diag("stderr 出现 Traceback"))

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        try:
            report, end = decoder.raw_decode(stdout_text)
        except json.JSONDecodeError as exc:
            self.fail(self._diag(f"stdout 不是可解析的 JSON 报告: {exc}: {stdout_text!r}"))
        # raw_decode 之后只允许空白：stdout 有且仅有一个可解析的 JSON 报告
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            self._diag(f"stdout 只能包含一个 JSON 报告: {stdout_text!r}"),
        )
        self.assertIsInstance(report, dict, self._diag("报告必须是 JSON 对象"))
        return report, completed.returncode

    def _assert_access_counts(self) -> None:
        """每次执行只访问 /redirect 一次，/target 为零（不跟随重定向）。"""
        counts = dict(self.server.path_counts)
        self.assertEqual(
            counts.get("/redirect", 0),
            1,
            self._diag(f"访问次数差异: /redirect 应恰好 1 次，实际 {counts}"),
        )
        self.assertEqual(
            counts.get("/target", 0),
            0,
            self._diag(f"访问次数差异: /target 应为 0 次（不得跟随重定向），实际 {counts}"),
        )

    def _check_redirect_report(
        self, *, name: str, expected_status: int, expected: dict, exit_code: int
    ) -> None:
        report, returncode = self._run_case(name=name, expected_status=expected_status)
        self.assertEqual(
            report,
            expected,
            self._diag(
                "报告内容差异:\n实际: "
                + json.dumps(report, ensure_ascii=False, sort_keys=True)
                + "\n期望: "
                + json.dumps(expected, ensure_ascii=False, sort_keys=True)
            ),
        )
        self.assertEqual(
            returncode,
            exit_code,
            self._diag(f"退出码应为 {exit_code}，实际 {returncode}"),
        )
        self._assert_access_counts()


class RelativeLocationTests(RedirectNotFollowedTestCase):
    """Location 为相对路径 /target：不跟随重定向。"""

    location_mode = "relative"
    location_label = "相对 Location (/target)"

    def test_expect_200_keeps_actual_302_and_fails_status_check(self) -> None:
        expected = _expected_report(
            name="redirect-relative-expect-200",
            expected_status=200,
            status_actual=302,
            status_passed=False,
            field_actual="redirect",
            field_present=True,
            field_passed=True,
            error=ASSERTION_FAILED,
        )
        self._check_redirect_report(
            name="redirect-relative-expect-200",
            expected_status=200,
            expected=expected,
            exit_code=1,
        )

    def test_expect_302_passes_both_checks(self) -> None:
        expected = _expected_report(
            name="redirect-relative-expect-302",
            expected_status=302,
            status_actual=302,
            status_passed=True,
            field_actual="redirect",
            field_present=True,
            field_passed=True,
            error=None,
        )
        self._check_redirect_report(
            name="redirect-relative-expect-302",
            expected_status=302,
            expected=expected,
            exit_code=0,
        )


class AbsoluteLocationTests(RedirectNotFollowedTestCase):
    """Location 为同一服务的完整 HTTP 地址：同样不跟随重定向。"""

    location_mode = "absolute"
    location_label = "绝对 Location (http://127.0.0.1:<port>/target)"

    def test_expect_200_keeps_actual_302_and_fails_status_check(self) -> None:
        expected = _expected_report(
            name="redirect-absolute-expect-200",
            expected_status=200,
            status_actual=302,
            status_passed=False,
            field_actual="redirect",
            field_present=True,
            field_passed=True,
            error=ASSERTION_FAILED,
        )
        self._check_redirect_report(
            name="redirect-absolute-expect-200",
            expected_status=200,
            expected=expected,
            exit_code=1,
        )

    def test_expect_302_passes_both_checks(self) -> None:
        expected = _expected_report(
            name="redirect-absolute-expect-302",
            expected_status=302,
            status_actual=302,
            status_passed=True,
            field_actual="redirect",
            field_present=True,
            field_passed=True,
            error=None,
        )
        self._check_redirect_report(
            name="redirect-absolute-expect-302",
            expected_status=302,
            expected=expected,
            exit_code=0,
        )


if __name__ == "__main__":
    unittest.main()
