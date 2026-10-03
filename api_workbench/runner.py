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


def _reject_constant(value: str):
    """拒绝未加引号的 NaN / Infinity / -Infinity（非标准 JSON）。

    该回调只在遇到*字面量*常量时触发；引号内的同名字符串
    （如 "NaN"）以及包含这些字样的字段名不会经过此处，仍然合法。
    """
    raise ValueError(f"响应正文包含非标准 JSON 常量: {value}")


def _parse_response_body(body: bytes):
    """按严格 JSON 解析响应正文，返回对象；任何非标准内容均返回 None。"""
    try:
        return json.loads(body, parse_constant=_reject_constant)
    except ValueError:
        return None


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

    # 可选的每次网络阻塞等待上限（秒），未提供时沿用默认三秒。
    # 只接受 0.1 至 30（含两端）的有限 JSON 数字，整数与小数均可；
    # 布尔、字符串、null、数组、对象一律拒绝，不做类型转换或默认值回退。
    timeout_seconds = REQUEST_TIMEOUT
    if "timeout_seconds" in case:
        raw_timeout = case["timeout_seconds"]
        # bool 是 int 的子类，需显式排除
        if isinstance(raw_timeout, bool) or not isinstance(
            raw_timeout, (int, float)
        ):
            raise CaseError(
                "'timeout_seconds' 必须为 0.1 至 30 之间的有限数字"
                "（不能是布尔值）"
            )
        # NaN 与 ±Infinity 无法通过区间比较，一并在此拒绝
        if not 0.1 <= raw_timeout <= 30:
            raise CaseError(
                "'timeout_seconds' 必须为 0.1 至 30 之间的有限数字"
            )
        timeout_seconds = float(raw_timeout)

    try:
        # 括号不成对等结构无效地址会让 urlsplit 本身（或其 hostname 属性）
        # 抛出 ValueError，必须在连接前作为用例错误拒绝，不得修补后继续请求
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        raise CaseError(
            f"'url' 结构无效（无法解析主机与端口），收到 {url!r}"
        ) from None

    if parsed.scheme != ALLOWED_SCHEME or hostname != ALLOWED_HOST:
        raise CaseError(
            f"'url' 仅支持主机为 {ALLOWED_HOST} 的 HTTP 地址，收到 {url!r}"
        )

    try:
        # 含非数字字符（如字母、负号）或超出 0–65535 时，
        # urlsplit 的 port 属性会抛出 ValueError，须在连接前拒绝用例
        parsed.port
    except ValueError:
        raise CaseError(
            f"'url' 端口无效（必须为 0 至 65535 之间的整数），收到 {url!r}"
        ) from None

    # 实际发往服务端的只有路径与查询字符串：其中未转义的非 ASCII 字符
    # 会原样进入请求行，可能导致编码异常越过本文件的请求错误处理，
    # 因此必须在连接前作为用例错误拒绝，不做自动百分号编码。
    # 已百分号编码的字节均为 ASCII（% 加两位十六进制数），此处原样放行，
    # 发送时既不解码也不重复编码；井号后的片段不发送给服务端，不参与校验。
    request_target = parsed.path if parsed.query == "" else (
        f"{parsed.path}?{parsed.query}"
    )
    try:
        request_target.encode("ascii")
    except UnicodeEncodeError:
        raise CaseError(
            "'url' 的路径或查询字符串含有未转义的非 ASCII 字符"
            "（请先进行百分号编码），井号后的片段不受此限制，"
            f"收到 {url!r}"
        ) from None

    return {
        "name": name,
        "url": url,
        "field": field,
        "expected_value": expected_value,
        "expected_status": expected_status,
        "timeout_seconds": timeout_seconds,
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

    connection = HTTPConnection(
        parsed.hostname,
        port,
        timeout=case.get("timeout_seconds", REQUEST_TIMEOUT),
    )
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

    # 响应必须是合法的严格 UTF-8 JSON 对象，否则字段检查失败、actual 为 null。
    # parse_constant 使整份正文（含其他字段、嵌套对象与数组元素）中的
    # 未加引号 NaN/Infinity/-Infinity 一律解析失败；引号内字符串与字段名不受影响。
    payload = _parse_response_body(body)
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
