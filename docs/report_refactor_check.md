# 报告组装重构 · 行为核对记录

范围：仅重构 `api_workbench/runner.py` 的报告组装流程，消除请求失败分支与其他结果分支
重复维护的报告结构。用例格式、CLI 入口（`run` / `serve`）、报告字段与退出码语义不变，
未新增断言或报告字段。

## 重构内容

- 新增唯一报告组装入口 `_build_report`（`api_workbench/runner.py:213`）：
  `name`/`passed`/`error`/`status_check`/`field_check` 的字段结构只在此定义一次，
  所有结果分支共用；各 `passed` 在组装处统一归一为 `bool`。
- 删除原先独立拼装同一结构的 `_request_failed_report` 和位置参数版 `_report`。
- 网络收发抽出为 `_send_get`（`api_workbench/runner.py:276`）：成功收满返回
  `(status, body)`，任何网络/协议错误返回 `None`。
- `execute`（`api_workbench/runner.py:307`）四个出口全部走 `_build_report`：
  request_failed（:316）、invalid_response（:337）、成功/断言失败（:362）。

## 核对方式

1. 现有测试：`python3 -m unittest discover -s tests` → 169 个全部通过
   （重构前后一致；环境无 pytest，按仓库既有的 unittest 方式运行）。
2. 受控本地响应：原始 socket 服务返回固定状态码/正文，经真实入口
   `python3 -m api_workbench run case.json` 对照四种结果；stdout 均为单一、
   可一次 `json.loads` 解析的 UTF-8 JSON 对象，无报告文件落盘。

## 四种结果对照（代表用例 field="status", expected_value="ok"）

| 场景（受控响应） | error | 退出码 | status_check | field_check | 源码位置 |
| --- | --- | --- | --- | --- | --- |
| 成功：200 `{"status":"ok"}` | `null` | 0 | expected 200 / actual 200 / true | actual `"ok"` / present true / true | runner.py:345-371 |
| 断言失败：200 `{"status":"wrong"}` | `assertion_failed` | 1 | 200 / 200 / **true** | actual `"wrong"` / present true / **false** | runner.py:345-371 |
| 无效正文：200 `not json` | `invalid_response` | 1 | 200 / 200 / **true**（状态码保留） | actual null / present null / false | runner.py:331-343 |
| 请求失败：已发响应头、Content-Length: 64 但仅发 4 字节即断开 | `request_failed` | 1 | 200 / actual **null** / false（不保留已收到的 200） | actual null / present null / false | runner.py:311-322 |

另核对：

- 状态码 500 但字段匹配：status_check=false、field_check=true 分别保留，
  error 仍为 `assertion_failed`，退出码 1（状态码不符不覆盖字段结果）。
- 无服务连接拒绝：`request_failed`，两项 actual 与 present 均为 null，
  三个 passed 全 false，退出码 1。
- 用例非法（`{ not json`）：退出码 2，stdout 为空，stderr 为
  `api_workbench: …`，无 Traceback（`run_case`，runner.py:374）。
- 大整数（1 后接 400 个 0 期望/实际）：退出码 0，报告 actual 完整保留 401 位十进制数。
- Unicode 文本（`你好, 世界 🎉`）：退出码 0，actual 原样保留 UTF-8 文本。
- 默认超时 3 秒、只发一次 GET、不跟随重定向、`serve` 入口
  （`GET /health`→200、其他路径→404）行为不变，由现有测试集与冒烟核对覆盖。
