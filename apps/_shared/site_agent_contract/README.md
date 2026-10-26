# 站点 Agent 协议合同

`aap-site-agent-contract` 是只依赖 Python 标准库的协议包，供中心和独立 Linux Agent 共同使用。此包只校验消息，不提供身份认证、当前时钟检查、文件访问、传输、业务任务或 ACK 状态持久化。

## 消息与 digest

```python
from aap_site_agent_contract import (
    canonical_json, digest, parse_json, validate_command, validate_result,
)

command = validate_command(parse_json(command_bytes))
command_digest = digest(command)
wire_bytes = canonical_json(command)

result = validate_result(parse_json(result_bytes))
result_digest = digest(result)
```

中心与 Agent **先调用 `validate_command` 得到规范化映射，再计算 command digest**。校验器会规范化 UUID、补齐下述参数默认值；因此直接对未规范化输入做 digest 可能产生不同结果。`digest` 不排除任何字段。结果 digest 同样针对 `validate_result` 返回的完整映射，包括 `closed`。

规范 JSON 使用排序对象键、`ensure_ascii=True`、紧凑分隔符、UTF-8 编码和 SHA-256。它不是 RFC 8785；不会归一化 Unicode，也不会将 `1` 与 `1.0` 合并。`parse_json` 拒绝重复键（含嵌套对象和转义后同名键）、NaN、Infinity、非法 UTF-8 和孤立代理码。输入必须为 `bytes`，默认上限为 2 MiB；消息校验器同样限制规范化消息为 2 MiB。只接受原生 JSON 类型，不接受 Python 元组、非字符串对象键或自定义对象。

所有校验器返回独立映射及嵌套容器，不保留输入对象的可变别名。失败统一抛出 `ProtocolError`，其 `.code` 和异常字符串只包含稳定错误码，不包含消息内容或路径。

## 命令白名单

固定 `protocol_version=1`。命令顶层必须且只能包含：

| 字段                                   | 合同                              |
| -------------------------------------- | --------------------------------- |
| `command_id`、`agent_id`               | UUID 字符串，规范为小写带连字符   |
| `credential_revision`、`root_revision` | 正整数                            |
| `root_handle`                          | `[A-Za-z0-9_-]{1,64}`             |
| `operation`                            | `probe / browse / scan / read`    |
| `arguments`                            | 对应操作的严格参数对象            |
| `issued_at`、`expires_at`              | 非负整数 Unix 秒；寿命为 1–120 秒 |
| `max_bytes`                            | 整数，1–64 MiB                    |
| `timeout_seconds`                      | 整数，1–60 秒                     |
| `fence`                                | 下述权限与业务边界对象            |

布尔值不能充当整数。过期时间只做结构校验，执行者还须检查当前时间、身份撤销、根资格和业务租约；超时预算不代表允许延长命令有效期。

`fence` 必须包含 `location_id`（UUID）、`location_revision`、`maintenance_revision`（正整数）和 `purpose`（`[a-z][a-z0-9_]{0,63}`）。可选字段为 UUID 类型的 `source_id / run_id / job_id / user_id / asset_id`，正整数 `generation`，以及 `task_id / lease_token`（`[A-Za-z0-9_.:@-]{1,128}`）。缺省的可选 fence 字段保持缺席，显式 null 不合法。

## 操作参数

共用可选参数 `relative_path` 缺省为 `""`，表示已授权来源子目录；`expected_anchor` 缺省为 null。其余参数如下，表中可选参数缺省规范为 null。

| 操作     | 必填                                  | 可选                                                                                                                    |
| -------- | ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `probe`  | 无                                    | `sample_file`：非空相对路径                                                                                             |
| `browse` | `path`：相对路径；`limit`：1–200 整数 | `after`：单个非空文件名；`snapshot`：SHA-256                                                                            |
| `scan`   | `path`：相对路径                      | `expected_identity`：数值对                                                                                             |
| `read`   | `path`：非空相对路径                  | `expected`：observation；`expected_source_identity`：数值对；`expected_sha256`：SHA-256；`expected_size`：0–64 MiB 整数 |

上述可选参数均允许 null，共用 `relative_path` 除外。路径保持原值，不做清洗或展开；拒绝绝对路径、`..`、`.`、重复／尾随斜杠、反斜杠、NUL、超过 1000 字符或 32 层路径。`browse.path`、`scan.path` 可为空以指向当前根。协议没有目标 URL、shell、任意命令或根路径参数；未知字段均拒绝。

非 null anchor 必须恰好包含 `path_digest`（小写 64 位 SHA-256）、`root` 和 `base`（数值对）。数值对严格为 `[positive_int, int]`，首值必须大于零，第二值只要求为整数；布尔值不合法。

非 null observation 是允许部分字段的严格对象，只能包含 `identity / source_identity`（数值对）、`size`（0–64 MiB 整数）、`mode`（非负整数）、`mtime_ns / ctime_ns`（整数，允许负时间戳）。字段值本身不可为 null；需要不指定预期值时使用整个 observation 为 null。

## 结果与 ACK

结果必须恰好包含 `protocol_version / command_id / command_digest / status / result / error_code / closed`。`command_digest` 为小写 64 位 SHA-256，`closed` 为布尔值。暂不解析操作结果内部 schema，`result` 必须是有界原生 JSON 对象。

- `completed`：`error_code=null`、`closed=true`，可携带结果对象。
- `failed`：`result={}`、`error_code` 为安全错误码；`closed=false` 可表示实际 I/O 尚未获关闭证据，不能将其当作已完成清理。

ACK 必须恰好包含 `protocol_version / command_id / command_digest / result_digest / state / code`。两个 digest 均为小写 64 位 SHA-256。`received / applied` 要求 `code=null`；`rejected` 要求安全错误码。错误码与 purpose 使用相同安全标签格式。

`received` 只表示中心持久收到，`applied` 才表示业务应用。校验器不判断 ACK 是否属于当前命令、digest 是否匹配、状态是否前进或租约是否仍有效；调用方必须将这些比较与本地日志／中心事务一起执行。重复 ACK 不能重复推进业务 checkpoint。

## 本地纯测试

在仓库根目录使用现有 API 虚拟环境，无需安装该包、连接数据库或启动服务：

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=apps/_shared/site_agent_contract/src \
apps/api/.venv/bin/python -m pytest \
  -c apps/_shared/site_agent_contract/pyproject.toml \
  -p no:cacheprovider apps/_shared/site_agent_contract/tests
```

这些测试覆盖共享协议，不代表中心接入、隔离网络拓扑或完整 Epic 验收已完成。
