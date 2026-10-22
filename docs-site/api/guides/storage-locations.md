---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# 受控存储位置

存储位置登记 API 或指定 worker 已挂载的只读目录。部署者配置少量允许根，超级管理员登记根内相对目录；新建子目录或登记已有路径无需重启服务。挂载方法见[生产部署](/ops/deploy/docker-compose#受控存储根目录)。

这些接口提供注册、目录浏览和节点能力证据。当前数据集 UI 仍使用上传与连接器复制导入；注册位置尚不会创建原地引用数据集或改变原图读取路径。S3/SFTP 关联位置复用[连接器](./storage-connections)的凭据与权限，不重复保存密钥。

## 端点和权限

所有路径前缀为 `/api/v1/storage-locations`。无权访问的位置返回 `404`；员工无目录浏览权限。

| 方法  | 路径                | 权限与行为                                                                        |
| ----- | ------------------- | --------------------------------------------------------------------------------- |
| GET   | `/roots`            | 超管；仅返回配置的根句柄，不返回绝对路径                                          |
| GET   | `/bindings`         | 超管；只读显示九个用途，即使共用桶仍逐项返回；不初始化绑定                        |
| GET   | 空路径              | 超管看全部，项目管理员看 global 或获授权位置；limit 默认100、最多200，支持 offset |
| POST  | 空路径              | 超管；登记 filesystem 位置，返回201                                               |
| GET   | `/{id}`             | 可见该位置的管理员；读取脱敏详情                                                  |
| PATCH | `/{id}`             | 超管；更新 filesystem 名称、授权、启停，必须提供 expected_revision                |
| GET   | `/{id}/directories` | 可见该位置的管理员；根内单层子目录分页                                            |
| POST  | `/{id}/probe`       | 超管；探测 API 或指定 Celery hostname                                             |
| GET   | `/{id}/probes`      | 超管；查看按位置、修订、执行节点记录的证据                                        |

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

`restricted` 默认仅超管可用；`use_user_ids` 可授权活跃项目管理员，`global` 允许所有平台管理员使用。授权不等于普通员工的任务读取权限。位置只读；绝对路径、父级跳转、符号链接和跨子挂载被拒绝。根目录和位置基目录在每个节点固定身份，替换后返回 `root_identity_changed`，不能通过重试静默重新绑定。

PATCH 允许 `name / enabled / scope / use_user_ids`，不允许改根或基目录。每次更新递增 `config_revision`；旧 `expected_revision` 返回409。S3/SFTP 位置需通过原连接器端点更新，配置或密钥变化使位置修订递增；已被内容或用途引用的连接配置不能改向其他目标，也不能删除。

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
