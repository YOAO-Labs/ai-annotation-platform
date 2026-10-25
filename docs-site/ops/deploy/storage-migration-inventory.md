---
title: 对象存储源端迁移清单
audience: [ops, dev]
type: how-to
status: stable
last_reviewed: 2026-10-25
---

# 对象存储源端迁移清单

迁移前可用只读命令盘点当前部署配置的九用途对象存储。清单记录物理桶、生命周期、版本控制、全部对象版本、删除标记及未完成 multipart；每个可读版本完整计算 SHA-256，共用物理桶只读取一次。

它提供选定源端在观测窗口内的事实。目标选择、对象复制、用途切换与退役仍需各自的迁移方案；`migration_ready` 始终为 `false`。清单不含原件正文，也不能作为备份恢复内容。

## 执行与核对

使用当前 checkout 的 API 虚拟环境，显式加载目标部署的数据库与 S3 配置。命令读取已有 `MINIO_*` 配置，不新增配置变量。数据库账号须能查询用途绑定、位置／连接的非凭据配置及异步任务；对象账号只需读取配置、列举桶版本／未完成上传和读取对象版本的权限。命令不会调用建桶、修改规则、上传或删除操作。

```sh
# 先按部署方式加载已核对的环境，再从仓库根目录执行。
apps/api/.venv/bin/python scripts/inventory_storage_migration.py \
  --output /secure/inventory/source.jsonl \
  --label '当前平台存储'

# 本地验证完整性，不连接数据库或对象存储。
apps/api/.venv/bin/python scripts/inventory_storage_migration.py \
  --verify /secure/inventory/source.jsonl
```

输出目录须事先存在并限制访问。文件以 `0600` 独占创建，已有文件不会覆盖；对象键和必要元数据仅保存在该私有文件中，终端只输出计数和结果。端点剥离用户信息及查询参数，不输出访问凭据或签名地址。清单处理完毕后按内部保留规则删除。

可用 `--handling /secure/inventory/handling.json` 传入九用途的明确处理选择，例如 `{"datasets":"migrate","media-cache":"retain"}`。支持 `migrate`、`rebuild`、`retain`；未提供的用途标为 `unreviewed`。选择重建并不授予删除许可，也不会自动把缓存视为可丢弃内容。

| 退出码 | 含义                                                                         |
| ------ | ---------------------------------------------------------------------------- |
| 0      | 生成时选定源端观测完整；使用 `--verify` 时，文件完整性及观测完整状态均通过。 |
| 1      | 文件已封闭，但存在权限、变化、预算等不完整项，或验证的清单本来就不完整。     |
| 2      | 参数、配置、数据库或文件等错误，不能视为成功清单。                           |

文件包含 header、逐桶／对象记录、issue、受摘要覆盖的 summary 和完整性 tail。中断或缺少有效尾记录的文件不能视为成功；尾部状态与计数也必须和受摘要覆盖的 summary 一致。摘要用于检查截断和意外改写，不提供签名认证。

## 读取与预算边界

未版本化桶使用对象列表；已启用或暂停版本控制的桶读取全部版本及删除标记，保留精确 `VersionId` 和 `IsLatest`。读取使用指定版本与 `IfMatch`，核对完整响应、字节数及前后 HEAD，真实关闭响应流后才发布 SHA。ETag 单列保存，不充当 MD5 或 SHA。`null` 版本是可覆盖的槽位，部分兼容服务器不回显其版本头；命令仍显式请求 `VersionId=null`，并用条件读取和前后 HEAD 核对，不称其为不可变版本。

默认最多 100,000 页、1,000,000 条对象／版本／删除标记／未完成上传、单对象 1 GiB、累计读取 1 TiB、单对象读取 300 秒，每页 1,000 项。可用 `--max-pages`、`--max-entries`、`--max-object-bytes`、`--max-total-bytes`、`--read-timeout-seconds`、`--page-size` 显式调整。网络连接与 SDK 读取也有超时且不自动重试；同步调用的超时取决于底层网络实现，不能抢占所有阻塞操作。

生命周期不存在是已知空规则；未授权或不支持是未知，并使源端观测不完整。分页循环、源配置变化、对象读中变化、长度不符、关闭失败或超限均保留不完整结果。未完成上传只记录事实，不完成、终止或删除。活跃源端并非原子快照，结束时未观测到变化也不代表可以直接切换。

## 业务与写入方覆盖

命令先在有界、只读的 repeatable-read 数据库事务中读取保存的用途绑定、位置数量及 pending／running AsyncJob 分组，再关闭事务进行对象读取；不跨物理 I/O 持有业务锁。

清单明确列出未被当前配置源覆盖的绑定，例如已经切至其他位置的头像用途。数据库观测单独记录时间与截断状态；异步任务只是潜在写入方，外部客户端、旧预签名上传、完整业务引用和历史位置仍标为未知。源端 `complete=true` 不能替代这些覆盖证明或停写屏障。

本地复现使用启动器拥有且业务表与桶均为空的独立 `test` 环境，已有业务或对象会被拒绝：

```sh
pnpm dev:worktree -- exec --mode test -- env DEBUG=false PYTHONDONTWRITEBYTECODE=1 \
  apps/api/.venv/bin/python scripts/verify_storage_migration_inventory.py \
  --report /private/verification.json
```

它创建合成中文键、版本历史、删除标记、未完成上传和临时只读对象账号，验证真实 CLI、写删拒绝及业务行／源对象不变，结束撤销确切账号；调用者随后按启动器的精确 confirmation 销毁所属 test 资源。与正在运行的其他模式及人工环境分开，不能指向日常业务数据库。

恢复数据库及对象内容应使用[一致备份与隔离恢复演练](/ops/deploy/storage-recovery)；应用回退见[升级指南](/ops/upgrade-guide#数据集存储的受控只读应用回退)。
