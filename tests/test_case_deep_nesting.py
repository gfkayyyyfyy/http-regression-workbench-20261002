"""用例文件 JSON 嵌套过深触发 RecursionError 时的回归测试。

以 cases/health.json 为基础保留全部必需字段，追加 extra 字段，验证：

1. extra 为数千层单元素数组包裹数字 0（或同深度的多层对象）时，解析
   阶段触发 RecursionError：直接调用 load_case 得到 CaseError，诊断
   包含传入路径并指出 JSON 嵌套过深；深层内容位于顶层数据（整份文件
   就是深层数组）时按同一约定拒绝，不跳过深层内容继续执行已读字段；
2. 经公开入口 ``python -m api_workbench run case.json`` 执行同一文件：
   退出码 2，stdout 为空（不生成执行报告），stderr 以 ``api_workbench:``
   开头、包含路径与原因、无 Traceback，且不建立任何 HTTP 连接
   （受控服务收到的 GET 数为零）——即使 name、url 与断言字段均合法；
3. 兼容对照：extra 改为浅层嵌套（两层数组）时被忽略，本地示例服务
   返回 200 与 {"status":"ok"} 时仍只发送一次 GET，输出一份成功
   JSON 报告并以 0 退出。

仅使用 Python 标准库 unittest；服务使用系统分配的临时端口，不修改
运行时递归上限，也不新增固定嵌套层数限制。
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

from api_workbench.runner import CaseError, load_case

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_CASE_PATH = os.path.join(PROJECT_ROOT, "cases", "health.json")

DIAGNOSTIC_PREFIX = "api_workbench:"

# 2000 层嵌套远超 Python 默认递归上限（1000），纯 Python 扫描器在解析
# 阶段必然抛出 RecursionError；两层数组则远在上限之内，作为兼容对照。
DEPTH = 2000
DEEP_ARRAY = "[" * DEPTH + "0" + "]" * DEPTH
DEEP_OBJECT = '{"k":' * DEPTH + "0" + "}" * DEPTH
SHALLOW_ARRAY = "[[0]]"


class _HealthServer(ThreadingHTTPServer):
    """仅在 GET /health 上返回 200 与 {"status":"ok"} 的受控服务。"""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _HealthHandler)
        self.port = self.server_address[1]
        self.get_count = 0
        self._lock = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self.get_count += 1


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "api_workbench-test/0.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server 命名约定)
        server: _HealthServer = self.server
        server.record()
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # 保持 stderr 干净
        return


def _health_case_text(port: int, extra: str | None) -> str:
    """以 cases/health.json 为基础（保留全部必需字段），指向临时端口，
    可选追加 extra 字段（值为原始 JSON 文本）。"""
    with open(HEALTH_CASE_PATH, encoding="utf-8") as handle:
        case = json.load(handle)
    for required in ("name", "url", "expected_status", "field", "expected_value"):
        assert required in case, f"cases/health.json 缺少必需字段 {required!r}"
    case["url"] = f"http://127.0.0.1:{port}/health"
    text = json.dumps(case, ensure_ascii=False)
    if extra is not None:
        # 在末尾的 } 之前插入 extra 字段，值按原始文本拼接以构造深层嵌套
        assert text.endswith("}")
        text = text[:-1] + ',"extra":' + extra + "}"
    return text


class DeepNestingCaseTests(unittest.TestCase):
    """深层嵌套用例文件：RecursionError 归入 CaseError，公开入口退出码 2。"""

    def setUp(self) -> None:
        self.server = _HealthServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _shutdown_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _write_case(self, name: str, text: str) -> str:
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _run(self, path: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "api_workbench", "run", path],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )

    def _assert_deep_nesting_rejected(self, path: str) -> None:
        """同一文件在 load_case 与公开入口下都按嵌套过深的用例错误拒绝。"""
        # 直接调用 load_case：CaseError，诊断包含路径并指出嵌套过深
        with self.assertRaises(CaseError) as caught:
            load_case(path)
        message = str(caught.exception)
        self.assertIn(path, message)
        self.assertIn("嵌套过深", message)

        # 公开入口：退出码 2，stdout 为空，stderr 为单行诊断且无 Traceback
        completed = self._run(path)
        self.assertEqual(
            completed.returncode, 2, f"应拒绝加载，stderr={completed.stderr!r}"
        )
        self.assertEqual(completed.stdout, b"", "失败时不得输出任何执行报告")
        stderr_text = completed.stderr.decode("utf-8")
        self.assertTrue(
            stderr_text.startswith(DIAGNOSTIC_PREFIX),
            f"诊断必须以 {DIAGNOSTIC_PREFIX} 开头: {stderr_text!r}",
        )
        self.assertIn(path, stderr_text)
        self.assertIn("嵌套过深", stderr_text)
        self.assertNotIn("Traceback", stderr_text)
        # 用例错误不得建立 HTTP 连接、不得发送任何请求
        self.assertEqual(self.server.get_count, 0, "加载失败时不得发送任何 GET")

    def test_deep_nested_array_in_extra_field_is_rejected(self) -> None:
        path = self._write_case(
            "deep_array.json",
            _health_case_text(self.server.port, DEEP_ARRAY),
        )
        self._assert_deep_nesting_rejected(path)

    def test_deep_nested_object_in_extra_field_is_rejected(self) -> None:
        path = self._write_case(
            "deep_object.json",
            _health_case_text(self.server.port, DEEP_OBJECT),
        )
        self._assert_deep_nesting_rejected(path)

    def test_deep_nesting_at_top_level_is_rejected(self) -> None:
        # 整份文件就是深层数组：同样按嵌套过深拒绝，而不是先按非对象拒绝
        path = self._write_case("deep_top_level.json", DEEP_ARRAY)
        self._assert_deep_nesting_rejected(path)

    def test_shallow_extra_is_ignored_and_case_passes(self) -> None:
        # 兼容对照：浅层 extra 被忽略，恰好一次 GET，成功报告，退出码 0
        path = self._write_case(
            "shallow_extra.json",
            _health_case_text(self.server.port, SHALLOW_ARRAY),
        )
        completed = self._run(path)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            completed.stderr, b"", f"stderr 必须为空: {completed.stderr!r}"
        )
        self.assertEqual(self.server.get_count, 1, "整个运行只应收到一次 GET")

        stdout_text = completed.stdout.decode("utf-8")
        decoder = json.JSONDecoder()
        report, end = decoder.raw_decode(stdout_text)
        self.assertEqual(
            stdout_text[end:].strip(),
            "",
            f"stdout 只能包含一个 JSON 报告: {stdout_text!r}",
        )
        expected = {
            "name": "health",
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


if __name__ == "__main__":
    unittest.main()
