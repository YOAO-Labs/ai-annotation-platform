---
title: 对象存储源端迁移清单
audience: [ops, dev]
type: how-to
status: stable
last_reviewed: 2026-10-25
---

# 对象存储源端迁移清单

迁移前可用只读命令盘点当前部署配置的九用途对象存储。清单记录物理桶、生命周期、版本控制、全部对象版本、删除标记及未完成 multipart；每个可读版本完整计算 SHA-256，共用物理桶只读取一次。

它提供选定源端在观测窗口内的事实。封存清单与业务定位目录可用于下文的本地初次复制；目标选择、用途切换与退役仍需各自的迁移方案；`migration_ready` 始终为 `false`。清单不含原件正文，也不能作为备份恢复内容。

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

## 已保存业务定位目录

源对象清单生成后，可另行读取数据库中已保存的对象归属，并关联同一份源报告。新目录使用独立 schema `aap.storage-migration-references/v1`，不会改写源报告，也不访问 S3；它回答“哪条保存记录声明了哪个地址，以及该地址是否被这份源报告观测到”。

```sh
apps/api/.venv/bin/python scripts/inventory_storage_references.py \
  --source-inventory /secure/inventory/source.jsonl \
  --output /secure/inventory/references.jsonl \
  --temp-dir /secure/inventory/tmp

# 只验证目录完整性，不连接数据库或 S3。
apps/api/.venv/bin/python scripts/inventory_storage_references.py \
  --verify /secure/inventory/references.jsonl
```

输出和临时目录的父目录应事先存在并限制访问。输出以 `0600` 独占创建；数据库快照、SQLite 关联索引位于自动清理的私有临时目录中。终端仅输出汇总和安全错误码。目录包含必要的逻辑 owner ID、物理端点、桶、原始键和保存修订，应按敏感运维资料保管；不含认证令牌、签名、加密凭据、授权证明或完整业务正文。头像的 `token` 列是公开头像对象的 32 位十六进制 owner ID，仅作为逻辑主键投影；此例外不适用于认证／API／授权令牌。

数据库账号只需静态白名单列的 SELECT 权限，无需写入、迁移或对象权限。命令先在单个有界的 `REPEATABLE READ READ ONLY` 事务中读取，再关闭连接处理源报告。它实际探测可见列，兼容报告附件版本列缺省的 0244 和新增该列的 0245；不会将缺省值解释为当前版本。权限不足、表形状未知或预算耗尽会保留不完整状态。

目录覆盖 24 类已登记记录的所有状态，包括 pending、ready、失败和 retired，不按当前用途绑定筛选：

| 保存记录                                                                           | 目录中的事实                                                                           |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| 预测输入、金字塔、视频、导出媒体                                                   | 独立 owner 和已保存 `inventory.objects` 的有界展开；预测输入相对键只拼接自身保存前缀。 |
| 训练版本、实验、预览、评估、选样                                                   | 保存对象或前缀、独立包对象；冻结业务文档只报告存在及未展开。                           |
| 头像、报告／评论／指南附件、RLE、导出 ZIP、审计归档                                | 已保存的对象归属、内容回执和版本；RLE 的保存字节 SHA 与规范表示 SHA 分列。             |
| 原件 Creation、Retirement                                                          | 历史声明和独立暂存声明，不据此重写当前地址。                                           |
| DatasetAssetLocation、TaskOriginal、DatasetAuxiliaryOriginal、DatasetSourcePreview | 当前定位与当前位置配置的依据，保留缺失关联。                                           |
| OriginalCacheEntry、TrainingStorageDeletion                                        | 保存身份对应的缓存键或删除前缀声明，不授予清理许可。                                   |

位置和用途绑定单列记录。保存端点优先；只有保存了 `storage_identity` 且与候选端点计算一致时，才可补充地址依据。没有保存身份的当前定位可明确采用当前登记位置，历史声明不能这样推断。缺 owner、未知 schema、地址冲突和未展开文档均有原因及计数，不要求先回填历史记录。

关联按端点、桶、原始 S3 key 和版本比较。键是不可解释的字符串，`%2F`、`%2f`、`..`、重复斜线和字面 `%` 不做 URL 解码或文件系统规范化。前缀必须具有保存的 `/` 边界；前缀范围不等于其中所有对象的准确业务成员关系。

| 结果                                      | 含义                                                       |
| ----------------------------------------- | ---------------------------------------------------------- |
| `exact_version`                           | 精确非空版本被源报告观测到；有保存 SHA／大小时还核对其值。 |
| `mutable_null`                            | 显式 `null` 槽被观测到；该槽可被覆盖，不是不可变版本。     |
| `unpinned_key`                            | 同键存在，但归属未保存版本；不挑选最新版本。               |
| `prefix_scope`                            | 保存前缀范围内有观测对象，不声明逐对象归属。               |
| `outside_selected_source`、`not_observed` | 声明属于其他源，或这份报告未观测到相应地址／版本。         |
| `conflict`、`unlocated`                   | 保存回执／身份冲突，或缺少可靠定位。                       |

源报告的逐对象 SHA、ETag、目录／manifest 摘要具有不同含义，不互相代替。源报告从同一文件描述符验证、建立索引并计算完整文件 SHA，同时核对文件身份及读前读后属性；目录记录配对摘要和观测窗口。数据库与源报告仍是两个时间窗口，`atomic_snapshot=false`。未匹配对象只能称“本有界目录未准确匹配”，不能称孤儿或删除对象。

默认每页 500 行、最多 10,000 页、1,000,000 行、1,000,000 条 JSON 展开、256 MiB 输出、1 GiB 源报告、1,000,000 条源对象记录、总时长 300 秒、单条 SQL 15 秒。分别用 `--page-size`、`--max-pages`、`--max-rows`、`--max-expanded`、`--max-output-bytes`、`--max-source-bytes`、`--max-source-entries`、`--max-seconds`、`--statement-seconds` 调整。页大小上限为 1,000；单记录上限为 256 KiB。总时长为协作式检查，SQL 另有服务端超时；进程中断仍可能留下没有有效尾记录的文件。

退出码沿用 0／1／2，但完成语义属于新目录：0 表示家族枚举及精确定位关联完成且源报告完整；1 表示封闭目录含未知、未固定版本、前缀范围、未展开或截断；2 表示参数、配置或完整性等致命错误。`enumeration_complete` 与 `resolution_complete` 分列，不能只看已枚举家族数量。外部写入方、既有预签名上传和停写覆盖始终为未知，`migration_ready=false`；这不是复制、停写或生产迁移证明。

新增目录的本地实际资格只使用启动器拥有的空 test 资源：

```sh
pnpm dev:worktree -- exec --mode test -- env DEBUG=false PYTHONDONTWRITEBYTECODE=1 \
  apps/api/.venv/bin/python scripts/verify_storage_references.py \
  --report /private/reference-verification.json
```

夹具拥有写权限，CLI 使用临时只读数据库角色，实际核对敏感列／写入拒绝、0244／0245、退休／孤立记录和源报告关联，比较 CLI 前后全部业务表与源对象指纹。只有该空旧事实窗口会往返迁移，结束恢复 head 并撤销临时角色；调用者随后销毁所属 test 数据库、Redis 和九桶。它不会启动应用、Worker 或浏览器，也不重复全链消费者／恢复资格。

## 本地初次复制与可恢复核验

已有封存的源清单与配对业务定位目录后，可用独立工具预复制源对象。它保存源版本到目标实际版本的映射，不修改绑定、业务定位或冻结 manifest。生产选型、停写、增量、切换、新写入回滚与退役仍需独立完成，所有结果始终 `migration_ready=false`。

先准备仅所属用户可访问的目录（`0700`）和目标配置文件（`0600`）。目标桶须事先存在且启用版本控制；目标地址不得与源清单任一物理桶相同。目标配置 JSON 仅包含以下字段，凭据值在私有文件中填写，不放在命令行或 checkpoint：

```json
{
  "endpoint_url": "http://target.example:9000",
  "bucket": "migration-target",
  "key_prefix": "initial-copy/",
  "access_key_id": "REPLACE_IN_PRIVATE_FILE",
  "secret_access_key": "REPLACE_IN_PRIVATE_FILE"
}
```

源凭据每次从既有部署配置读取，端点须与计划一致。目标账号需要目标桶的版本控制读取、精确版本列举及对象 PUT／GET／HEAD 权限；不需要建桶、删除或规则写入权限。工具不会创建桶、修改生命周期或删除任何源／目标对象。

```sh
apps/api/.venv/bin/python scripts/copy_storage_migration.py plan \
  --source-inventory /secure/inventory/source.jsonl \
  --references /secure/inventory/references.jsonl \
  --checkpoint /secure/inventory/copy.sqlite \
  --target-config /secure/inventory/target.json

apps/api/.venv/bin/python scripts/copy_storage_migration.py run \
  --checkpoint /secure/inventory/copy.sqlite \
  --target-config /secure/inventory/target.json

apps/api/.venv/bin/python scripts/copy_storage_migration.py verify \
  --checkpoint /secure/inventory/copy.sqlite \
  --target-config /secure/inventory/target.json

apps/api/.venv/bin/python scripts/copy_storage_migration.py status \
  --checkpoint /secure/inventory/copy.sqlite
```

`plan` 在各输入的同一文件描述符中验证封存、计算完整文件摘要并建立索引，核对目录与源报告的配对摘要。未封存或损坏输入拒绝；已封存但不完整的输入可生成部分计划。`--handling` 可读取同格式 `0600` 私有 JSON，明确覆盖九用途的 `migrate/rebuild/retain` 选择；选择固定进计划。共用物理桶仅计一次，所有关联用途须一致且已评审；歧义、retain、rebuild、未核验、单对象超限和删除标记均跳过并单列。未完成 multipart 保留为源事实，不复制为对象，也不自动清理。业务未知声明不阻断已知源对象预复制，目录未匹配不等于孤儿。

checkpoint 是 `0600` SQLite，含计划 UUID、两个输入的完整摘要、非凭据源／目标身份、处理选择和逐源版本结果。独占进程锁、完整同步事务和记录摘要保护耐久状态；摘要检查意外损坏和改写，不提供签名认证。终端仅输出有界计数与固定错误码，原始键、元数据、nonce 和版本回执留在私有 checkpoint。不要编辑数据库或并发使用同一文件；计划创建失败留下的无效私有文件应由所属操作者调查后处理。

目标键严格使用 `<key_prefix><plan_uuid>/<源定位SHA>/<源内容SHA>`；源定位摘要覆盖原端点、桶、原始 key 和 VersionId。历史版本各有独立地址和实际目标 VersionId，不假定两端版本相同；S3 键不解码或做路径归一化。源读取指定保存版本与条件 ETag，完整核对 SHA、大小、前后 HEAD 与真实内容元数据后，才由私有磁盘暂存上传。`null` 是可覆盖槽位，未固定版本另行保留；任何变化或确认不足均留下失败／未知，不能证明活跃源已停止写入。

每次 PUT 之前，工具耐久保存意图与随机创建 nonce，使用 `IfNoneMatch="*"`。目标须完整读回并核对 SHA、大小、内容元数据、创建标识和实际不可变 VersionId，才能发布 verified。PUT 丢失应答或进程硬退出留下 writing／uncertain；重启先按保存标识与完整内容证据认领同一版本。已有其他对象拒绝覆盖；有历史版本或删除标记时也不会靠再次 PUT 解决不明确结果。

`run` 默认续跑 pending／uncertain，已 verified 项不重复上传，failed 项仅加 `--retry-failed` 时重试。`verify` 只读已保存的目标精确版本，不上传；失败会撤销成功状态，保留原回执与最近错误。`status` 离线读取 checkpoint，计数从唯一结果行导出；`copy_subset_complete` 仅指可复制子集，源清单、业务目录、跳过项和未知覆盖分别保留。

| 退出码 | 含义                                                                                |
| ------ | ----------------------------------------------------------------------------------- |
| 0      | 可复制子集均已 verified；不表示全量迁移或切换就绪。                                 |
| 1      | 存在 pending、writing、uncertain、failed 或本次暂停；新计划尚待复制时通常返回此码。 |
| 2      | 参数、输入完整性、私有文件、身份或连接等致命错误。                                  |

默认单对象 256 MiB；每次调用实际源读取、目标上传、目标核验分别最多 2 GiB、最多 10,000 个待处理对象及 30 分钟。可用 `--max-object-bytes`、`--max-source-bytes`、`--max-upload-bytes`、`--max-verify-bytes`、`--max-objects`、`--max-seconds` 显式调整；只接受有限正值。计划记录默认值，run／verify 的显式覆盖仅作用于本次调用。连接／读取超时分别默认 10／60 秒，通过 `--connect-timeout-seconds`、`--read-timeout-seconds` 调整；SDK 不自动重试。正文流式处理，不使用 multipart 或后台传输；协作时限与信号不能抢占所有底层网络阻塞。

SIGINT／SIGTERM 暂停后可重新执行相同 run。正常完成、暂停和重启只清理该计划精确私有暂存路径；硬杀残留由同 checkpoint 的下次 run／verify 收尾，不扫描或删除其他文件。磁盘满、读写／关闭失败和预算耗尽保留可恢复状态。回退仅停用工具并保留目标和 checkpoint，不反向删除源或其他目标。

新增工具的实际复现仅在官方启动器已核验的空 test 环境运行：

```sh
pnpm dev:worktree -- exec --mode test -- env DEBUG=false PYTHONDONTWRITEBYTECODE=1 \
  apps/api/.venv/bin/python scripts/verify_storage_migration_copy.py \
  --report /private/copy-verification.json
```

夹具拥有随机源对象与独立目标桶、临时只读源账号和限定目标账号；实际 CLI 经过复制前后、PUT 确认窗口的强制退出和进程重启，核对显式失败项重试、空对象、原始中文／百分号键、历史版本、共享用途、mutable null、删除标记和未完成上传。它独立读回目标字节／版本／元数据并比较源规则、对象和业务表指纹。结束撤销账号、删除确切目标桶并独立确认不存在；调用者随后按官方 confirmation 销毁所属 test 数据库、Redis、源桶及其夹具。报告仅保留有界脱敏结果，不替代生产目标或应用消费者资格。

## 源对象清单的本地复现

源对象清单的独立复现使用启动器拥有且业务表与桶均为空的 `test` 环境，已有业务或对象会被拒绝：

```sh
pnpm dev:worktree -- exec --mode test -- env DEBUG=false PYTHONDONTWRITEBYTECODE=1 \
  apps/api/.venv/bin/python scripts/verify_storage_migration_inventory.py \
  --report /private/verification.json
```

它创建合成中文键、版本历史、删除标记、未完成上传和临时只读对象账号，验证真实 CLI、写删拒绝及业务行／源对象不变，结束撤销确切账号；调用者随后按启动器的精确 confirmation 销毁所属 test 资源。与正在运行的其他模式及人工环境分开，不能指向日常业务数据库。

恢复数据库及对象内容应使用[一致备份与隔离恢复演练](/ops/deploy/storage-recovery)；应用回退见[升级指南](/ops/upgrade-guide#数据集存储的受控只读应用回退)。
