# HTTP 接口回归工作台

建设面向接口开发的本地回归测试产品，逐步覆盖请求集合、参数与环境变量、JSON 响应断言、用例执行、失败报告、历史结果比较和 OpenAPI JSON 的有限导入。

采用：Python 3 标准库（urllib / http.server / http.client / json / argparse），无第三方依赖。

## 当前功能

单用例本地 HTTP 回归，提供两个入口：

- `python -m api_workbench serve`：在 `127.0.0.1:8765` 启动示例服务。
  - `GET /health` → 200 `{"status":"ok"}`
  - 其他路径 → 404 `{}`
  - 端口绑定失败时向 stderr 说明原因并以退出码 2 退出。
- `python -m api_workbench run case.json`：对单个用例只发送一次 `GET`（不跟随重定向，超时固定 3 秒）。

### 用例格式

UTF-8 编码的 JSON 对象：

```json
{
  "name": "health",
  "url": "http://127.0.0.1:8765/health",
  "expected_status": 200,
  "field": "status",
  "expected_value": "ok"
}
```

校验规则：`name`、`url`、`field` 为非空字符串；`expected_value` 为字符串、布尔值
（JSON 的 `true`/`false`）、JSON 数字或显式 `null`——整数按任意精度接受
（含超出浮点范围的大整数，如 `1` 后接 400 个 `0`），小数/指数写法则必须解析为
有限值（`1e400`、`NaN`、`Infinity` 拒绝）；数字文本长度仍受 Python 现有限制
（默认 4300 位）约束，超过后按用例错误拒绝；
`expected_status` 为 100–599 的整数（布尔值排除）；`url` 仅为主机是 `127.0.0.1`
的 HTTP 地址，端口必须为 0–65535 的整数（未填写端口、端口段为空或显式 `0`
时沿用默认 80 端口）。文件不可读、JSON 无法解析或字段无效（含字母、负数等无法
解析的端口及超出范围的端口）时不发送请求：stdout 为空、stderr 说明原因、退出码为 2。
`url` 结构无效（如主机部分括号不成对：`http://[127.0.0.1:8765/health`、
`http://127.0.0.1]:8765/health`）时同样不修补、不连接，按用例错误以退出码 2 拒绝，
诊断以 `api_workbench: ` 开头且包含原始地址，不输出 Traceback。
路径或查询中的非 ASCII 字符（中文、带重音字母、表情等，含经 `\uXXXX`
转义还原的字符）必须已做百分号编码；未转义的原始字符同样在连接前按用例错误
以退出码 2 拒绝。已百分号编码的地址按原有文本发送，不解码、不重复编码；
`#` 之后的片段不发送给服务端，片段单独含非 ASCII 字符不影响校验结果。

### 报告

stdout 仅输出一个 JSON 报告（不落盘），包含 `name`、`passed`、`error`，
以及状态码检查 `status_check` 与字段检查 `field_check`（各含 `expected`、
`actual`、`passed`，字段检查另含被检查的字段名 `field` 与存在性标记
`present`）。

| 场景 | error | 退出码 |
| --- | --- | --- |
| 两项检查均通过 | `null` | 0 |
| 状态码或字段断言失败 | `assertion_failed` | 1 |
| 响应不是合法 JSON 对象（字段检查失败、actual 为 null，状态码检查结果仍保留） | `invalid_response` | 1 |
| 超时或连接失败（两项 actual 均为 null、所有 passed 为 false） | `request_failed` | 1 |

说明：

- 状态码不符时仍会执行字段检查。
- 字段检查的 `present` 标记字段存在性（JSON 布尔值或 `null`，不替代
  `passed`）：请求完成且正文通过严格 JSON 校验、顶层为对象时，顶层键
  存在为 `true`、缺失为 `false`；值为 `null`、`false`、`0`、空字符串、
  数组或对象都不影响存在判断。`field` 按完整键名匹配，点号不表示嵌套
  路径。正文无效或顶层不是对象（`invalid_response`）以及请求失败
  （`request_failed`）时 `present` 为 `null`。
- 字段缺失时 `actual` 为 `null`；其他类型的实际值原样保留（保留各自的 JSON
  类型，布尔不转成文字），这两种情况字段检查均失败。
- 断言按类型严格匹配：字符串期望只匹配完全相等的字符串；布尔期望只匹配布尔
  实际值——`true` 不匹配数字 `1` 或字符串 `"true"`，`false` 不匹配数字 `0`、
  空字符串 `""` 或 `null`；数字期望只匹配数字实际值（布尔除外），按 JSON
  解析结果精确比较、不使用误差容限——`1`、`1.0` 与 `1e0` 互相匹配，
  `0` 与 `-0.0` 相等，`1` 不匹配 `true`、`0` 不匹配 `false`，也不匹配
  字符串、null、数组或对象；超出浮点范围的整数（如 `1` 后接 400 个 `0`）
  仍按任意精度整数逐位比较，报告中完整保留十进制数值、不截断或舍入；
  `null` 期望只在键存在且值为 `null` 时通过——
  键缺失时 `actual` 虽为 `null` 仍判失败，字符串 `"null"`、空字符串、
  `false`、`0`、数组和对象均不匹配 `null`。
- 用例文件中的 `expected_value` 缺失，或为字符串、布尔值、JSON 数字与 `null`
  之外的类型（数组、对象，含未加引号的 `NaN`、`Infinity`、`-Infinity`，
  以及 `1e400` 这类解析后非有限的小数/指数数值）时，
  `load_case` 抛出用例错误：不发送请求，stdout 为空，stderr 以
  `api_workbench: ` 开头并指出 `expected_value`，退出码为 2，不出现 Traceback。
  引号内的 `"Infinity"` 仍是普通字符串期望，按既有字符串规则处理。
- 响应正文按严格 JSON 校验：未加引号的 `NaN`、`Infinity`、`-Infinity`
  不是标准 JSON，无论位于目标字段、其他字段、嵌套对象还是数组元素，
  整份正文一律判为 `invalid_response`；状态码是否符合期望不改变该分类。
  引号内的同名字符串（如 `"NaN"`）、包含这些字样的字段名或较长字符串
  （如 `"the limit is Infinity"`）仍是合法文本，不受影响。
- 数值溢出同样判为 `invalid_response`：`1e400`、`-1E+400` 等按解析规则
  产生正/负无穷的数值，无论位于正文何处，整份正文无效（避免报告输出
  非标准的 `Infinity`）。有限数字不受影响：`1e308` 等大指数写法照常解析，
  作为实际值保留数值类型参与原有断言；字符串 `"1e400"`、`"Infinity"`
  及含这些字样的字段名仍是普通文本。

### 示例用例

- `cases/health.json`：通过用例。
- `cases/health_wrong_value.json`：仅将 `expected_value` 改为 `"wrong"` 的失败用例。
