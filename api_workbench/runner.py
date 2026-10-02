"""Single-case HTTP regression runner."""

import http.client
import json
import sys
from urllib.parse import urlparse

TIMEOUT_SECONDS = 3.0


def run(case_path):
    try:
        with open(case_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        return _usage_error(f"cannot read case file: {exc}")
    try:
        case = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _usage_error(f"case file is not valid JSON: {exc}")
    if not isinstance(case, dict):
        return _usage_error("case file must contain a JSON object")
    problem = _validate(case)
    if problem is not None:
        return _usage_error(problem)

    report = _execute(case)
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    return 0 if report["passed"] else 1


def _usage_error(message):
    print(message, file=sys.stderr)
    return 2


def _validate(case):
    for key in ("name", "url", "field"):
        if not isinstance(case.get(key), str) or not case[key]:
            return f"case field {key!r} must be a non-empty string"
    if not isinstance(case.get("expected_value"), str):
        return "case field 'expected_value' must be a string"
    status = case.get("expected_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return "case field 'expected_status' must be an integer between 100 and 599"
    try:
        parts = urlparse(case["url"])
        port = parts.port  # raises ValueError on a malformed port
    except ValueError as exc:
        return f"case field 'url' is invalid: {exc}"
    if parts.scheme != "http" or parts.hostname != "127.0.0.1":
        return "case field 'url' must be an http URL with host 127.0.0.1"
    if port is not None and not 1 <= port <= 65535:
        return "case field 'url' has an invalid port"
    return None


def _execute(case):
    status_check = {"expected": case["expected_status"], "actual": None, "passed": False}
    field_check = {
        "field": case["field"],
        "expected": case["expected_value"],
        "actual": None,
        "passed": False,
    }
    error = None

    try:
        status, body = _get_once(case["url"])
    except (OSError, http.client.HTTPException):
        error = "request_failed"
    else:
        status_check["actual"] = status
        status_check["passed"] = status == case["expected_status"]
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            error = "invalid_response"
        elif case["field"] in payload:
            value = payload[case["field"]]
            field_check["actual"] = value
            field_check["passed"] = isinstance(value, str) and value == case["expected_value"]

    if error is None and not (status_check["passed"] and field_check["passed"]):
        error = "assertion_failed"

    return {
        "name": case["name"],
        "passed": error is None,
        "error": error,
        "status_check": status_check,
        "field_check": field_check,
    }


def _get_once(url):
    parts = urlparse(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    connection = http.client.HTTPConnection(
        parts.hostname, parts.port or 80, timeout=TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()
