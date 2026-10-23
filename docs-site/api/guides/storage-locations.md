---
audience: [dev, project_admin, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# 受控存储位置

存储位置登记 API 或指定 worker 已挂载的只读目录。部署者配置少量允许根，超级管理员登记根内相对目录；新建子目录或登记已有路径无需重启服务。挂载方法见[生产部署](/ops/deploy/docker-compose#受控存储根目录)。

这些接口提供注册、目录浏览和节点能力证据。存储页可配置位置使用范围与人员；获授权的项目管理员可在自己创建的数据集上接入该位置，扫描、预检并确认原地登记。S3/SFTP 关联位置复用[连接器](./storage-connections)的凭据与权限，不重复保存密钥。

## 端点和权限

所有路径前缀为 `/api/v1/storage-locations`。无权访问的位置返回 `404`；员工无目录浏览权限。所有 GET 要求 `datasets:read`，POST/PATCH 要求 `datasets:write`；JWT 保留当前角色权限。

| 方法  | 路径                   | 权限与行为                                                                        |
| ----- | ---------------------- | --------------------------------------------------------------------------------- |
| GET   | `/roots`               | 超管；仅返回配置的根句柄，不返回绝对路径                                          |
| GET   | `/bindings`            | 超管；只读显示九个用途，即使共用桶仍逐项返回；不初始化绑定                        |
| GET   | 空路径                 | 超管看全部，项目管理员看 global 或获授权位置；limit 默认100、最多200，支持 offset |
| POST  | 空路径                 | 超管；登记 filesystem 位置，返回201                                               |
| GET   | `/{id}`                | 可见该位置的管理员；读取脱敏详情                                                  |
| GET   | `/{id}/source-summary` | 当前可见来源及已有后台任务摘要，独立分页                                          |
| PATCH | `/{id}`                | 超管；更新 filesystem 名称、授权、启停，必须提供 expected_revision                |
| GET   | `/{id}/directories`    | 可见该位置的管理员；根内单层子目录分页                                            |
| POST  | `/{id}/probe`          | 可见该位置的管理员；探测 API 或指定 Celery hostname                               |
| GET   | `/{id}/probes`         | 可见该位置的管理员；查看按位置、修订、执行节点记录的证据                          |

## 登记和修改

部署配置示例：`STORAGE_FILESYSTEM_ROOTS={"ceph":"/mnt/cephfs"}`。API 请求只发送句柄和相对路径：

```json
{
  "name": "公司 CephFS",
  "root_handle": "ceph",
  "relative_path": "traffic",
  "scope": "restricted",
  "use_user_ids": []
}
```

`restricted` 默认仅超管可用；`use_user_ids` 可授权活跃项目管理员，`global` 允许所有平台管理员使用。授权只允许项目管理员管理自己创建的数据集，不授予其他数据集管理权；普通员工沿用任务读取权限。浏览及探测在 I/O 前后复查账号、位置授权和最初 API Key，撤权后不交付目录结果或发布探测证据。位置只读；绝对路径、父级跳转、符号链接和跨子挂载被拒绝。根目录和位置基目录在每个节点固定身份，替换后返回 `root_identity_changed`，不能通过重试静默重新绑定。

PATCH 允许 `name / enabled / cache_policy / scope / use_user_ids`，不允许改根或基目录。每次更新递增 `config_revision`；旧 `expected_revision` 返回409。外部 S3/SFTP 位置的 enabled/cache_policy 可由超管经此接口修改；名称、使用范围、定位和密钥需通过原连接器端点更新。平台固定位置不可修改。配置或密钥变化使位置修订递增；已被内容或用途引用的连接配置不能改向其他目标，也不能删除。

## 目录分页

`path` 相对于已登记位置，默认空字符串；`limit` 默认100、最多200。响应包含 `entries: [{name, relative_path}]` 与 `next_cursor`。游标有效15分钟，绑定位置、修订、API节点、路径摘要与目录快照；目录变化、换节点、改路径或旧修订返回409，调用者应从第一页重读。

每页最多检查5000个原始条目，文件和链接也计入预算；遍历循环预算2秒，超限明确失败。目录名需有效 UTF-8，中文和空格可用；不支持编码返回 `unsupported_path_encoding`。不返回文件内容、符号链接或任意宿主机目录。

## 节点探测

```json
{ "node_id": "celery@scan-host", "sample_file": "vehicle_detection/sample.jpg" }
```

省略 `node_id` 或传 `api` 探测当前 API。其他值必须是完整 Celery hostname，结果用 `worker:<hostname>` 标识。任务只投递到该 worker 的专属队列，执行端核对实际 hostname、操作者及修订；不会由普通共享队列随机选择节点。

`list / read / range` 分别返回 `verified / unknown`；只传目录时不会推断 read 已验证。显式样本以只读方式采样；`write / delete / presign` 为 `unsupported`，不写探针文件。离线返回 `node_timeout` 或 `node_unavailable`，未配置根返回 `root_not_configured`。证据5分钟过期，配置变更或停用后显示 `stale`；API可达不能代替 worker 可扫描。

API本地探测等待窗口3秒，远端等待窗口5秒。超时后台结果不会自行更新有效证据；操作系统阻塞的文件系统调用无法由线程取消，等待窗口不表示底层 I/O 已终止。首次探测某节点信任其部署根映射；跨节点 dev/inode 不比较，能力通过也不代表字节一致或原地预览已验收。

目录位置的 `cache_policy` 为 none/preview，默认 preview；它是来源预览缓存的上限，none 优先于来源请求。修改上限更新位置修订，使旧扫描/登记不能继续发布。已有派生不隐式删除，预览读取实时检查上限；外部原图与已独立托管的副本不受缓存选择影响。

## S3 浏览与探测

有连接器关联的 s3 位置支持相同 directories/probe 接口。path 与 sample_file 为精确前缀/对象键相对 base_prefix 的字符串，不使用文件系统路径规范化。目录页只返回 CommonPrefixes，JWT 游标固定位置、修订和前缀，查询 cursor 最多12288字符。读前读后均复查当前授权。

根探测只证明 list；sample_file 的成功探测才返回 read/conditional_read=verified。每次实际内容读取仍重新验证条件生效。range/write/delete/presign 均为 unsupported。目标受连接器主机白名单限制；严格客户端关闭代理，发送目标限定为配置 endpoint，不跟随跨目标重定向。DNS 解析仍存在 SDK 内再次解析的既有边界，不等同于 IP 固定。

## SFTP 浏览与探测

具备部署资格的 sftp 位置也支持 directories/probe。`declared_capabilities.reference` 表示当前 API 节点的引用准入配置：filesystem 和外部 S3 可用，SFTP 须匹配 `STORAGE_SFTP_ROOTS`；这不代表节点已通过实时可达性探测。平台固定 S3 位置不作为外部来源。

SFTP 连接 base_path 须为 `/`，整个账号命名空间为位置授权范围。path/sample_file 只接受其下严格相对路径，拒绝绝对路径、父级跳转及探测到的符号链接。浏览对最多5000项的完整目录清单排序分页；游标固定清单摘要，目录变化须重选。探测仅验证 list 和显式样本 read；range/write/delete/presign/conditional_read 均为 unsupported。

客户端仅信任部署指定的完整主机公钥，使用白名单校验后固定的 socket 地址连接。服务器只读 jail 由部署者实际核验，不能从探测成功推断。API 与指定 worker 必须具有相同资格；错误公钥、未获资格、目录不可读或超时均不能发布成功证据。

## 来源与任务摘要

`GET /{location_id}/source-summary` 返回 `location_id / items / total / jobs:{items,total}`。来源行只有 source_id/source_name、可空的 dataset_id/dataset_name、enabled/unregistered_at；任务行只有 id/kind/status/created_at/completed_at。不返回凭据、执行证明或内部配置，不探测或遍历外部存储。

来源使用 limit/offset，任务使用 jobs_limit/jobs_offset，各自默认20、最多200。任务范围覆盖该位置全部可见来源，独立于当前来源页，并包括恢复前的历史尝试。项目管理员仍需拥有数据集管理权和位置使用权；全局位置不公开其他人的来源数量。停用、注销历史保留；原数据集已删除的来源仅超管可见，dataset 字段为空。任务详情、取消和恢复沿用原任务及来源接口。
