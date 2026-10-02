"""单用例执行：用例加载/校验、发送 GET、生成 JSON 报告。"""

from __future__ import annotations

import json
import socket
import sys
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

REQUEST_TIMEOUT = 3.0
ALLOWED_SCHEME = "http"
ALLOWED_HOST = "127.0.0.1"

ASSERTION_FAILED = "assertion_failed"
INVALID_RESPONSE = "invalid_response"
REQUEST_FAILED = "request_failed"


class CaseError(ValueError):
    """用例文件不可读、无法解析或字段校验失败。"""


def load_case(path: str) -> dict:
    """读取并校验用例文件，失败时抛出 CaseError（不发送任何请求）。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise CaseError(f"无法读取用例文件 {path!r}: {exc}") from exc

    try:
        case = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise CaseError(f"用例文件 {path!r} 不是合法的 UTF-8 文本: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CaseError(f"用例文件 {path!r} 不是合法的 JSON: {exc}") from exc

    if not isinstance(case, dict):
        raise CaseError("用例必须是一个 JSON 对象")

    name = case.get("name")
    url = case.get("url")
    field = case.get("field")
    expected_value = case.get("expected_value")
    expected_status = case.get("expected_status")

    if not isinstance(name, str) or not name:
        raise CaseError("'name' 必须为非空字符串")
    if not isinstance(url, str) or not url:
        raise CaseError("'url' 必须为非空字符串")
    if not isinstance(field, str) or not field:
        raise CaseError("'field' 必须为非空字符串")
    if not isinstance(expected_value, str):
        raise CaseError("'expected_value' 必须为字符串")
    # bool 是 int 的子类，需显式排除
    if isinstance(expected_status, bool) or not isinstance(expected_status, int):
        raise CaseError("'expected_status' 必须为整数（不能是布尔值）")
    if not 100 <= expected_status <= 599:
        raise CaseError("'expected_status' 必须是 100 至 599 之间的整数")

    parsed = urlsplit(url)
    if parsed.scheme != ALLOWED_SCHEME or parsed.hostname != ALLOWED_HOST:
        raise CaseError(
            f"'url' 仅支持主机为 {ALLOWED_HOST} 的 HTTP 地址，收到 {url!r}"
        )

    return {
        "name": name,
        "url": url,
        "field": field,
        "expected_value": expected_value,
        "expected_status": expected_status,
        "_parsed": parsed,
    }


def _request_failed_report(case: dict) -> dict:
    return {
        "name": case["name"],
        "passed": False,
        "error": REQUEST_FAILED,
        "status_check": {
            "expected": case["expected_status"],
            "actual": None,
            "passed": False,
        },
        "field_check": {
            "field": case["field"],
            "expected": case["expected_value"],
            "actual": None,
            "passed": False,
        },
    }


def _report(case: dict, status, field_actual, status_passed, field_passed, error):
    return {
        "name": case["name"],
        "passed": bool(status_passed and field_passed),
        "error": error,
        "status_check": {
            "expected": case["expected_status"],
            "actual": status,
            "passed": bool(status_passed),
        },
        "field_check": {
            "field": case["field"],
            "expected": case["expected_value"],
            "actual": field_actual,
            "passed": bool(field_passed),
        },
    }


def execute(case: dict) -> tuple[dict, int]:
    """发送唯一一次 GET（不跟随重定向），返回 (报告, 退出码)。"""
    parsed = case["_parsed"]
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection = HTTPConnection(parsed.hostname, port, timeout=REQUEST_TIMEOUT)
    try:
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read()
            status = response.status
        except (socket.timeout, TimeoutError, OSError, HTTPException) as exc:
            return _request_failed_report(case), 1
    finally:
        connection.close()

    status_passed = status == case["expected_status"]

    # 响应必须是合法的 UTF-8 JSON 对象，否则字段检查失败、actual 为 null
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        report = _report(
            case,
            status=status,
            field_actual=None,
            status_passed=status_passed,
            field_passed=False,
            error=INVALID_RESPONSE,
        )
        return report, 1

    field_name = case["field"]
    if field_name not in payload:
        # 字段缺失：actual 为 null
        field_actual = None
        field_passed = False
    else:
        value = payload[field_name]
        # 非字符串值原样保留；仅字符串且相等才算通过
        field_actual = value
        field_passed = isinstance(value, str) and value == case["expected_value"]

    error = None if status_passed and field_passed else ASSERTION_FAILED
    exit_code = 0 if error is None else 1
    report = _report(
        case,
        status=status,
        field_actual=field_actual,
        status_passed=status_passed,
        field_passed=field_passed,
        error=error,
    )
    return report, exit_code


def run_case(path: str) -> int:
    try:
        case = load_case(path)
    except CaseError as exc:
        print(f"api_workbench: {exc}", file=sys.stderr)
        return 2

    report, exit_code = execute(case)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    sys.stdout.buffer.write((output + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return exit_code
