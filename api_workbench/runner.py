"""单用例执行：用例加载/校验、发送 GET、生成 JSON 报告。"""

from __future__ import annotations

import json
import math
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


def _parse_float_finite(value: str):
    """解析 JSON 小数；指数溢出（如 1e400 / -1E+400）产生 ±inf 时拒绝。

    该回调接收的仍是原始数字文本，有限值（含 1e308 这类大指数写法）
    照常解析为 float；只有按现有规则会得到正/负无穷时才抛错，
    使整份正文判为 invalid_response，避免报告输出非标准的 Infinity。
    """
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"响应正文包含溢出的数值: {value}")
    return result


def _decode_body(body: bytes):
    """将完整响应正文严格解码为 Unicode 文本，失败时返回 None。

    只接受严格 UTF-8：编码检查覆盖整份正文，任何非法 UTF-8 字节
    （如 UTF-16/UTF-32 的 BOM 与 NUL 排布、孤立代理项的原始字节
    ED A0 80、截断或超长序列）都令解码失败。绝不依据响应头的
    charset 改用其他编码，也不忽略坏字节或以替代字符顶替。

    唯一放宽：正文开头允许恰好一个 UTF-8 BOM（EF BB BF），解码后
    剥去这一个字符；其后再出现的 BOM 作为普通 U+FEFF 保留
    （落在 JSON 语法位置会令解析失败，落在字符串内则是合法字符）。
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _parse_response_body(body: bytes):
    """按严格编码与严格 JSON 解析响应正文，返回对象；任何无效内容均返回 None。

    先在字节层面完成严格 UTF-8 解码（见 _decode_body），再把文本交给
    json：直接向 json.loads 传 bytes 会让其按 BOM 自动识别 UTF-16/UTF-32
    正文，因此必须先解码为 str 以封死该旁路。ASCII 正文中以 JSON 转义
    书写的 \\ud800 / \\udc00 解码后是合法文本，照常解析为孤立代理项。
    """
    text = _decode_body(body)
    if text is None:
        return None
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_parse_float_finite,
        )
    except ValueError:
        return None


class CaseError(ValueError):
    """用例文件不可读、无法解析或字段校验失败。"""


def _is_finite_number(value) -> bool:
    """是否为有限 JSON 数字（int/float，布尔除外）。

    未加引号的 NaN/Infinity/-Infinity 以及 1e400 这类指数溢出，
    按 json 默认解析会得到 nan/inf，在此一并判为非法。

    JSON 整数字面量一律解析为 Python 任意精度 int，本身不存在
    无穷概念——包括超出浮点范围的大整数（如 1 后接 400 个 0），
    全部合法。math.isfinite 只能用于 float：对超出浮点范围的
    大整数调用会抛 OverflowError，因此整数分支必须先行返回。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


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
    except ValueError as exc:
        # JSONDecodeError 是 ValueError 子类；此外超过 Python 数字文本长度
        # 限制（见 sys.get_int_max_str_digits，默认 4300 位）时，解析器抛
        # 裸 ValueError 而非 JSONDecodeError。两者都属用例错误：转为
        # CaseError，保证不逃逸成 Traceback（不放宽、不突破该长度限制）。
        raise CaseError(f"用例文件 {path!r} 不是合法的 JSON: {exc}") from exc

    if not isinstance(case, dict):
        raise CaseError("用例必须是一个 JSON 对象")

    name = case.get("name")
    url = case.get("url")
    field = case.get("field")
    expected_status = case.get("expected_status")

    if not isinstance(name, str) or not name:
        raise CaseError("'name' 必须为非空字符串")
    if not isinstance(url, str) or not url:
        raise CaseError("'url' 必须为非空字符串")
    if not isinstance(field, str) or not field:
        raise CaseError("'field' 必须为非空字符串")
    # expected_value 接受字符串、布尔值（true/false）、JSON 数字、
    # 显式 null（None），或以上标量组成的数组（可为空数组，元素可混合）。
    # 数字中整数按任意精度接受（含超出浮点范围的大整数，如 1 后接 400 个 0），
    # 小数/指数写法则必须解析为有限 float（1e400、NaN、Infinity 拒绝）。
    # 键必须存在：省略该键与写 null 含义不同，省略仍属用例错误，
    # 因此不能用 get 的默认值区分，必须显式检查键是否存在。
    if "expected_value" not in case:
        raise CaseError(
            "'expected_value' 缺失：必须为字符串、布尔值、有限数字、null "
            "或由这些标量组成的数组"
        )
    expected_value = case["expected_value"]
    # bool 是 int 的子类，需先识别布尔；对象一律拒绝；None（JSON null）
    # 显式允许。数组单独校验：元素只能是字符串、布尔值、有限数字或 null，
    # 不接受对象、嵌套数组或非有限数字。
    if isinstance(expected_value, list):
        for element in expected_value:
            if (
                element is not None
                and not isinstance(element, (str, bool))
                and not _is_finite_number(element)
            ):
                raise CaseError(
                    "'expected_value' 数组元素必须为字符串、布尔值"
                    "（true/false）、JSON 数字或 null（不接受对象、"
                    "嵌套数组、NaN、Infinity 或 1e400 这类溢出的"
                    "小数/指数数值）"
                )
    elif (
        expected_value is not None
        and not isinstance(expected_value, (str, bool))
        and not _is_finite_number(expected_value)
    ):
        raise CaseError(
            "'expected_value' 必须为字符串、布尔值（true/false）、"
            "JSON 数字、null 或由这些标量组成的数组（整数按任意精度接受；"
            "不接受对象、NaN、Infinity 或 1e400 这类溢出的小数/指数数值）"
        )
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

    # 实际发往服务端的只有路径与查询（片段不发送，不参与校验）。
    # 其中的原始空格（U+0020）必须已做百分号编码（%20）：未编码的空格
    # 会让请求行出现非法的空白分隔，越过现有错误处理，因此必须在连接前
    # 作为用例错误拒绝。已编码的 %20 与查询中的加号都是普通字符，不受影响。
    if " " in parsed.path or " " in parsed.query:
        raise CaseError(
            f"'url' 路径或查询包含未编码的空格，收到 {url!r}"
        )

    # 同理，路径与查询中的非 ASCII 字符（中文、带重音字母、表情等）必须
    # 已做百分号编码；未转义的原始字符会让请求行的编码行为依赖底层实现，
    # 越过现有错误处理，因此必须在连接前作为用例错误拒绝，
    # 不做自动编码、不解码已编码内容。
    request_target = parsed.path + parsed.query
    if any(ord(char) > 127 for char in request_target):
        raise CaseError(
            f"'url' 路径或查询包含未转义的非 ASCII 字符，收到 {url!r}"
        )

    return {
        "name": name,
        "url": url,
        "field": field,
        "expected_value": expected_value,
        "expected_status": expected_status,
        "timeout_seconds": timeout_seconds,
        "_parsed": parsed,
    }


def _report(
    case: dict,
    status,
    field_actual,
    status_passed,
    field_passed,
    error,
    field_present,
):
    """组装唯一一种报告结构；所有结果分支（成功、断言失败、
    invalid_response、request_failed）都经由这里输出相同字段。"""
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
            # present 只表达顶层键是否存在，不参与 passed 判定：
            # 完整响应为 JSON 对象时为 true/false，无法检查时为 null
            "present": field_present,
        },
    }


def _field_matches(expected, actual) -> bool:
    """按类型严格匹配字段期望值与实际值。

    - 布尔期望只匹配布尔实际值（true 不匹配 1 或 "true"）；
    - 数字期望只匹配数字实际值（布尔除外），按 JSON 解析结果精确比较，
      不设误差容限：1、1.0 与 1e0 互相匹配，0 与 -0.0 相等；
      1 不匹配 true，0 不匹配 false；超出浮点范围的大整数（如 10**400）
      仍按任意精度整数精确比较，只匹配逐位相等的整数实际值；
    - 字符串期望只匹配完全相等的字符串；
    - null 期望只匹配 JSON null（不匹配 "null"、""、false、0、[]、{}）；
    - 数组期望只匹配数组实际值（不匹配对象或其他标量）：按长度、顺序
      及各位置的值逐项比较，不排序、不去重；空数组只匹配空数组。
      元素沿用上述严格类型规则（true 不等于 1，"1" 不等于 1，
      数字 1 与 1.0 相等，null 只匹配 null；大整数仍按任意精度比较）。
    """
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _field_matches(expected_item, actual_item)
                for expected_item, actual_item in zip(expected, actual)
            )
        )
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual == expected
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        try:
            return actual == expected
        except OverflowError:
            # 超出浮点范围的整数与有限 float 不可能相等（某些 Python 版本
            # 在跨类型比较时会因整数无法转 float 而抛 OverflowError）。
            return False
    if expected is None:
        return actual is None
    return isinstance(actual, str) and actual == expected


def execute(case: dict) -> tuple[dict, int]:
    """发送唯一一次 GET（不跟随重定向），返回 (报告, 退出码)。

    各结果分支只负责算出状态码/字段的实际值与通过标记，
    报告结构与退出码在函数末尾统一生成，避免重复定义字段。
    """
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
        except (socket.timeout, TimeoutError, OSError, HTTPException):
            # 连接失败/超时/正文提前断开：即使已收到响应头也不保留状态码
            status = None
            body = None
    finally:
        connection.close()

    if status is None:
        # 请求失败：响应不可用，状态码与字段检查均无实际值，
        # 既不能确认键存在也不能确认缺失，present 为 null
        error = REQUEST_FAILED
        status_passed = False
        field_present = None
        field_actual = None
        field_passed = False
    else:
        status_passed = status == case["expected_status"]

        # 响应必须是严格 UTF-8（开头至多一个 BOM）编码的合法严格 JSON 对象，
        # 否则字段检查失败、actual 为 null。_decode_body 对整份正文做严格
        # UTF-8 检查：非法字节（含 UTF-16/UTF-32 正文、孤立代理项的原始字节）
        # 无论位于目标字段、其他字段、对象键还是嵌套内容都令整份正文无效，
        # 不参考响应头 charset、不忽略或替换坏字节。parse_constant 使整份正文
        # 中未加引号的 NaN/Infinity/-Infinity 一律解析失败；parse_float 使
        # 1e400 等指数溢出为 ±inf 的数值同样解析失败；引号内字符串与字段名
        # 不受影响（ASCII 转义的 \ud800 等仍是合法文本）。
        payload = _parse_response_body(body)
        if not isinstance(payload, dict):
            # 正文无效或顶层不是对象：无法判断键是否存在，present 为 null
            error = INVALID_RESPONSE
            field_present = None
            field_actual = None
            field_passed = False
        else:
            field_name = case["field"]
            # present 只按完整键名判断顶层键是否存在（点号不表示嵌套路径）：
            # 值为 null、false、0、空字符串、数组或对象都算存在。
            field_present = field_name in payload
            if field_present:
                # 实际值原样保留（保留 JSON 类型，布尔不转文字）；
                # 仅当实际值与期望值类型相同且相等时才通过（规则见 _field_matches）。
                field_actual = payload[field_name]
                field_passed = _field_matches(case["expected_value"], field_actual)
            else:
                # 字段缺失：present 为 false、actual 为 null，即使期望也是 null
                # 仍判失败——null 期望只在键存在且值为 null 时通过
                field_actual = None
                field_passed = False
            error = None if status_passed and field_passed else ASSERTION_FAILED

    report = _report(
        case,
        status=status,
        field_actual=field_actual,
        status_passed=status_passed,
        field_passed=field_passed,
        error=error,
        field_present=field_present,
    )
    return report, 0 if error is None else 1


def run_case(path: str) -> int:
    try:
        case = load_case(path)
    except CaseError as exc:
        print(f"api_workbench: {exc}", file=sys.stderr)
        return 2

    report, exit_code = execute(case)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    # 用例 JSON 转义（如 \ud800 / \udc00）可能引入孤立代理项，
    # ensure_ascii=False 会保留它们，直接 UTF-8 编码会抛 UnicodeEncodeError。
    # backslashreplace 只对无法编码的代理项生效，将其原样转成 ASCII 字面量
    # \ud800（本就出现在 json 已生成的字符串字面量内部，是合法 JSON 转义），
    # 解析后与输入值完全一致；普通中文、表情与合法代理对照常输出 UTF-8。
    encoded = (output + "\n").encode("utf-8", errors="backslashreplace")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return exit_code
