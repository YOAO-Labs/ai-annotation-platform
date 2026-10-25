---
title: 对象存储清单与初次复制
audience: [ops, dev]
type: how-to
status: stable
last_reviewed: 2026-10-26
---

# 对象存储清单与初次复制

迁移前可用只读命令盘点当前部署配置的九用途对象存储。清单记录物理桶、生命周期、版本控制、全部对象版本、删除标记及未完成 multipart；每个可读版本完整计算 SHA-256，共用物理桶只读取一次。

它提供选定源端在观测窗口内的事实。封存清单与业务定位目录可用于控制台或CLI的初次复制；用途切换与退役仍需各自的执行结果，`migration_ready` 始终为 `false`。清单不含原件正文，也不能作为备份恢复内容。

## 从控制台建立清单

超级管理员可在 **存储管理 → 迁移准备** 提交同一配置源清单引擎的后台任务，查看分页历史、共享任务详情和真实完整性结果，并认证下载封存JSONL。来源仍固定部署端点及九用途桶；无需目标存储，已切往外部位置的用途如实保留未覆盖。高级预算限制读取与执行，报告在生成期间受32MiB硬上限约束。

应用任务的私密报告存于数据库，沿现有终态Job规则保留30天。下载后若要用于下文的业务定位目录或本地预复制，将文件放入权限为`0700`的私有目录，并将文件权限设置为`0600`；浏览器下载权限由所在系统决定。可先用下文的`--verify`检查封存格式。CLI仍可独立生成清单，其输出路径和保留由操作者管理。

此入口须应用0255并刷新所属Worker，无需重建挂载源码的后端镜像。任务只观察源端，不执行目标复制、绑定切换、停写、回滚或物理退役；清单完整也不能替代这些工作。接口见[配置源迁移清单任务](/api/guides/storage-locations#配置源迁移清单任务)。

## 从控制台执行初次复制

在封存清单行点击 **准备初次复制**，选择已登记的全局S3目标并明确九用途的复制／重建／保留声明。后台使用同一只读定位collector生成与源报告摘要配对的目录，再用下文的复制核心建立私密计划；准备步骤不向目标写入。审阅可复制数、跳过、歧义与未知覆盖后，显式开始初次复制。部分封存输入不需要先补成全历史完整清单。

应用复制owner保存冻结源报告、目录、SQLite初始计划及摘要，执行时只将逐对象delta独立提交数据库。writing／nonce先于PUT提交，返回VersionId先于完整目标读回保存；普通续跑、显式失败重试与重新核验沿同一目标key和回执，不重新生成计划或累加成功数。目标版本控制、准确历史检查、条件创建与完整SHA核验沿原核心执行，不增加DELETE探测权限。

失败对象可在计划详情内分页选择，仅重试已选项时其余状态保持；使用安全对象身份摘要定位，完整物理地址可从认证下载的SQLite检查点核对。下载文件包含恢复材料和元数据，应移入权限`0700`的私有目录并设为`0600`。检查点快照包含初始基线与已保存delta，原AsyncJob的30天清理不删除复制owner、映射、回执或目标责任。

正常已收尾的attempt可由其他Worker沿同一计划续跑。硬中断的恢复只在确切host／boot／PID namespace／PID start证据证明原进程已退出或被替换时解除该attempt的两个准确Permit；源／目标的不明PUT仍按nonce、条件创建和精确版本恢复。无法确认旧执行结束时保留unknown，不按TTL清空或声称跨节点硬恢复成功。取消和晚期撤权不抹去已发生的物理事实。

目标使用当前登记凭据，单纯密钥轮换不会改变冻结的物理身份；运行中配置变化暂停进一步I/O，修正后可沿原计划执行新attempt。复制owner进入位置引用和维护影响，即使任务取消、成功或已过期，目标也不能被无声改址或删除。此入口须应用0256并刷新所属Worker，无需重建挂载源码镜像；接口见[初次复制计划与续跑](/api/guides/storage-locations#初次复制计划与续跑)。

结果始终区分本次选中对象、全复制子集与业务覆盖；零可复制对象不等于复制成功。应用及下文CLI都不关闭原写入渠道、不更新业务引用／用途绑定，不执行最终切换、回滚、源数据删除或物理退役。

## 后续清单与增量批次

在已有复制计划详情选择 **准备后续批次**，提交同一来源的新封存清单。服务端继承冻结目标、九用途声明与执行预算，在原执行及原生读取／上传确已收尾后封存父批次证据并建立唯一后继。父批次仍可查看和下载，但不能再执行、核验或恢复；同父批次和同后续清单重复提交返回已有后继，不产生分支。

新批次使用自包含的 SQLite 检查点，保留祖先的创建身份、目标映射、准确回执与去重历史。旧检查点仍可读取和续跑，新检查点也可通过现有 CLI 的 `status`、`run`、`verify` 使用。应用端建立新批次，CLI 不另提供生成后继的命令。

同端点／桶／键／显式非 `null` 版本的 SHA、大小和 ETag 相同，且已有物理创建证据时，继承原目标键、nonce 和准确版本回执。继承行首先为待核验，执行时完整读取保存的准确目标版本；即使同键已有其他最新版本，也不改读最新版本。有准确回执时不再读源或 PUT；未知上传应答沿原 nonce 恢复，不另建目标。可变或未固定版本重新复制；显式版本身份冲突不覆盖或采纳其他对象。

差异中的 `previous_only_objects` 只比较直接父批次当前对象与新报告；祖先旧对象继续留在历史，不逐代重复算作本轮未见。`history_records` 包括去重旧对象结果和桶／multipart／问题事实，不是对象成功数。新清单里的最新状态和删除标记保持原观测，缺失不推断删除。不完整清单仍保持不完整，`migration_ready=false`。

部署此功能须应用0257并刷新所属Worker，无需重建挂载源码镜像。增量批次不提供最终停写屏障；最终核验、业务切换、新写入回滚与物理退役仍分别执行。

## 迁移维护窗口中的只读准备

控制台可从已准备且实际执行已收尾的父复制批次开启迁移窗口。须应用0258、刷新所属API／Worker并已启用既有访问维护协议；无需重建挂载源码的后端镜像。窗口固定默认来源的端点、九桶映射和配置修订以及父批次／目标身份，普通位置维护接口对默认来源的限制保持。

开启后默认来源Gate进入排空，拒绝新的普通登记准入。只有保存的实际操作为零时才可显式停止；随后选择此窗口建立源清单，再由固定父批次建立后继。清单和复制任务保存窗口ID及修订，专用来源准入联查实际任务、权限、停止Gate和冻结配置；目标仍走原普通许可。窗口内来源SDK能力仅有冻结桶的必要只读操作，客户端、读流和原生线程仍须真实关闭。

该窗口只管理登记访问，`source_quiescence_proven=false`和`migration_ready=false`始终保留。外部预签名、未登记调用、其他逻辑位置指向同源的写入，以及提供方生命周期等自动写入仍需真实存储端／入口栅栏和已接受请求排空证据；普通许可清零、签名到期或重复清单一致不能代替这些事实。此处不接受操作员勾选来推导全源已静止。最终历史路由、业务切换和旧源退役仍按迁移后续步骤执行。

关闭窗口先停止新专用准入；存在未收尾操作时保留关闭中与来源阻断，实际归零后再次关闭才恢复普通准入。任务取消不会自动重开来源，硬中断不按TTL清除许可。窗口独立于AsyncJob保留，已有窗口事实时不能破坏性降级schema；应用回退应保留0258和全部保存责任。

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

数据库账号只需静态白名单列的 SELECT 权限，无需写入、迁移或对象权限。命令先在单个有界的 `REPEATABLE READ READ ONLY` 事务中读取，再关闭连接处理源报告。它实际探测可见列，兼容报告附件版本列缺省的旧schema；新责任表在其引入前不适用，当前schema缺表或缺少必要字段则报告不完整。不会将缺省版本解释为当前版本。权限不足、表形状未知或预算耗尽会保留不完整状态。

目录覆盖27类已登记记录的所有状态，包括pending、ready、失败和retired，不按当前用途绑定筛选。逐family识别当前保存结构；数据集原件的新定位仍为schema1，不将全部新地址统一当作schema2：

| 保存记录                                                                           | 目录中的事实                                                                                                |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| 预测输入、金字塔、视频、导出媒体                                                   | 独立 owner 和已保存 `inventory.objects` 的有界展开；预测输入相对键只拼接自身保存前缀。                      |
| 训练版本、实验、预览、评估、选样                                                   | 保存对象或前缀、独立包对象；冻结业务文档只报告存在及未展开。                                                |
| 头像、报告／评论／指南附件、RLE、导出 ZIP、审计归档                                | 已保存的对象归属、内容回执和版本；RLE 的保存字节 SHA 与规范表示 SHA 分列。                                  |
| 原件 Creation、Retirement                                                          | 历史声明与独立暂存声明；Creation跨端点staging_ref沿自身保存地址，不沿最终原件端点。                         |
| DatasetAssetLocation、TaskOriginal、DatasetAuxiliaryOriginal、DatasetSourcePreview | 优先使用已保存端点／桶／完整键；旧无保存引用的当前定位明确标记其登记位置依据。                              |
| OriginalCacheEntry、ThumbnailOutput、TrainingStorageDeletion                       | 新原图缓存为保存prefix下的payload，缩略图为thumbnail.webp；旧NULL缓存仍用原无根键，删除前缀不授予清理许可。 |
| MaskFormatUpload、MaskFormatImport                                                 | 上传及批次各自保存的import地址；完整键为保存前缀加逻辑文件名，批次字节摘要与计划摘要不混用。                |

位置和用途绑定单列记录。保存端点优先；仅在没有坏保存引用、并且保存的`storage_identity`与候选端点计算一致时，才可补充地址依据。没有保存身份的旧当前定位可明确采用当前登记位置，历史声明不能这样推断；未知schema或地址冲突不能靠今天的绑定／连接补全。对象引用中的key_prefix只是受控根，不额外生成前缀范围；真正prefix owner的prefix已含完整物理根，inventory完整键不重复拼根。缺owner、未知schema、地址冲突和未展开文档均有原因及计数，不要求先回填历史记录。

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
  apps/api/.venv/bin/python scripts/verify_storage_references_current.py \
  --report /private/reference-verification.json
```

当前夹具在0254准备小型合成保存记录和源对象，CLI使用临时只读数据库角色实际核对27家族SQL投影、不同保存地址形式、跨端点暂存、当前配置不同于保存端点、版本和未知历史，并交给现有copy plan读取。比较CLI前后全部业务表、源对象版本／内容和输入清单指纹，验证写入及秘密列读取均被拒绝；结束撤销临时角色，调用者随后销毁所属test数据库、Redis和九桶。它不往返数据库迁移，不启动应用、Worker或浏览器，不代表完整业务消费者或生产迁移验证。

旧`verify_storage_references.py`保留0244／0245历史复现夹具；当前工具的纯检查覆盖其旧表／列形状兼容，不把历史实测记录冒称当前head验证。

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

需要只重试部分失败对象时，在`--retry-failed`之外重复传入`--retry-object-id <对象身份SHA>`。存在明确选择时，仅这些failed对象执行，其他状态保持；对象身份从私密checkpoint核对，不传入原始S3键。未传入选择时，全失败项重试及普通续跑行为保持。全计划仍有未完成项时，不能从选中项成功推出迁移就绪。

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
