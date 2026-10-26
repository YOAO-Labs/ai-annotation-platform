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

## MinIO 原生源端封闭

`scripts/storage_source_fence.py` 在部署方已登记的 Docker 拓扑上控制真正的源端写入口与进程，适用于本机回环 HTTP 入口。它独立于上述应用窗口；当前应用不接受该命令的 JSON 作为全源停写证明，也不会因此开放最终业务切换。

### 登记受控拓扑

先由运维建立唯一 S3 入口，保留原签名使用的主机与端口。源容器只连接一个 `Internal=true` 私有 Docker 网络，不发布 S3 或控制台端口；网络只含源与入口两个容器。入口另连一个独立的非内部 bridge 网络，该网络仅含入口，用于发布回环端口；设置 `net.ipv4.ip_forward=0`，不提供跨网络路由。应用、浏览器和外部写入方都经过入口。宿主机与 Docker 管理员属于可信运维边界，命令不防止管理员另建入口或改写底层卷。

源使用 `minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`，仅将具名数据卷挂到 `/data`。入口使用 `nginx@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c`，入口程序为 `nginx`、参数为 `-g 'daemon off;'`，将一个配置目录只读挂到 `/etc/nginx`，发布 `127.0.0.1:<原端口>:80`。配置目录权限为 `0700`，其中无密钥的 `nginx.conf` 为 `0644`。配置内容由 `storage_source_fence_docker.gateway_config(source_ip, readonly=False)` 生成，保留 Host 和原始 URI，关闭请求缓冲；不转发控制台或管理路径。

每项资源都必须带 `aap.source-fence.owner`（同一随机32位小写十六进制标识）、`aap.source-fence.checkout`（当前仓库真实绝对路径）和 `aap.source-fence.role` 标签。源、入口、私有网络、入口网络、数据卷的角色依次为 `source / gateway / network / frontend / data`。命令持续核对精确容器和网络 ID、卷、镜像、挂载、成员及入口端口，不按名称自动接管当前9000服务。HTTPS、外部反代、多节点 MinIO及有其他直达通道的拓扑不由此适配器证明。

在权限为 `0700` 的私有目录保存 `0600` JSON 描述文件，不放凭据。状态文件固定为入口配置目录父级的 `fence-state.json`，同一物理入口始终共用同一日志和跨进程锁，不能改路径再初始化一套控制状态：

| 字段                                                | 内容                                                             |
| --------------------------------------------------- | ---------------------------------------------------------------- |
| `schema`                                            | `aap.storage-source-fence-config/v1`                             |
| `owner / checkout`                                  | 上述共同标签值                                                   |
| `source_container / gateway_container / network_id` | 实际64位 Docker ID                                               |
| `gateway_frontend_network_id`                       | 独立入口 bridge 网络的准确64位 ID                                |
| `source_volume_name`                                | 源具名数据卷的准确名称                                           |
| `gateway_config_directory`                          | 上述配置目录的真实绝对路径                                       |
| `public_endpoint`                                   | 保持原主机／端口的 `http://127.0.0.1:<端口>`                     |
| `purpose_buckets`                                   | 九用途到实际桶名的完整映射，允许共用桶；不得漏掉源上的其他业务桶 |

根凭据只由当前命令进程的 `AAP_SOURCE_FENCE_ACCESS_KEY / AAP_SOURCE_FENCE_SECRET_KEY` 环境变量取得，不存入描述文件、状态或报告。需要 Docker 管理权限、现有 `mc` 和当前 API 虚拟环境；不构建后端镜像、不改应用 `.env`，不使用全局 `~/.mc`。

### 封闭、核对与恢复

```sh
apps/api/.venv/bin/python scripts/storage_source_fence.py init \
  --config /private/source.json --state /private/fence-state.json
apps/api/.venv/bin/python scripts/storage_source_fence.py preview \
  --config /private/source.json --state /private/fence-state.json
# 使用 init/status 返回的最新 revision 和本次操作的 UUID。
apps/api/.venv/bin/python scripts/storage_source_fence.py seal \
  --config /private/source.json --state /private/fence-state.json \
  --expected-revision 1 --operation-id 540546ac-c81b-4f9d-81a2-8a8a4cc9c434
apps/api/.venv/bin/python scripts/storage_source_fence.py status \
  --config /private/source.json --state /private/fence-state.json
```

封闭先持久记录意图，再将入口切为只允许 GET／HEAD 并真正重启入口，终止旧入口进程。保存原生命周期规则及已观察的准确 multipart 上传ID后，禁用并回读全部规则；停止整个源容器、确认进程已退出再启动，按保存ID和当前列表逐个终止 multipart，并通过 ListParts 返回 NoSuchUpload 确认，然后再执行一次完整进程屏障。已签发的 PUT／Complete URL、root、普通账号及匿名写入均受同一入口约束；已经接受的请求由进程退出收尾。命令不会删除完整对象、历史版本或删除标记，未完成上传的终止属于本操作的明确变更范围。

最后重启后重新核验全局 tier 为空、站点复制关闭、桶复制无配置、全部实际对象版本没有 Restore 状态、当前可恢复批处理作业为空，以及规则仍被禁用、已保存上传均不可继续、源卷当前 multipart 元数据残留为零和源进程代次／入口配置未变。已有当前与非当前过期规则的非空源可以使用；不支持的后台写入者或观测失败明确拒绝，不解释成“没有”。这些检查只针对本次源端对象稳定性，不要求清零历史业务定位或旧 tier／MRF 记录。

固定 MinIO 构建的全桶 multipart 列表来自内存缓存，重启后为空不能证明磁盘上传已消失。命令保留重启前实际观察的准确ID并逐个核验；最终对 `/data/.minio.sys/multipart` 做有界只读元数据检查，补住列表未覆盖或停止前才落盘的上传。发现未映射残留或无法完整检查时拒绝封存，不猜对象键、不删除完整对象。该限制见[固定源码的列表实现](https://github.com/minio/minio/blob/07c3a429bfed433e49018cb0f78a52145d4bedeb/cmd/erasure-server-pool.go#L1718)。

`held=true` 是成功实际观测的结果，附观测时刻；状态文件本身没有永久证明效力。状态、快照及锁位于私有目录，采用修订 CAS、操作 UUID 和跨进程锁。发生中断或原生应答未知时保持封闭责任，按最新修订和同一操作 UUID 续跑；不会自动解封。若源已停止但尚未重启，续跑先确认原进程已退出并重新建立只读入口，再启动原源容器；只读的 `status / preview` 不启动容器。`status` 重新检查真实状态，配置漂移会使证明失效。任何未知结果均先核对现有进程和日志，不另建同名源或删除状态文件。

需要恢复写入时，显式执行 `release`，传入最新 `--expected-revision` 和另一个 `--operation-id` UUID。命令先使证明失效，再按原规则和已禁用快照核对恢复生命周期，成功后才恢复入口写入。规则被其他运维修改时拒绝覆盖，保持责任供核实。该步骤只恢复原生入口，不关闭应用维护窗口；最终清单、增量复制和业务切换仍须各自完成。

### 本地原生验收

纯检查使用 `PYTHONDONTWRITEBYTECODE=1 apps/api/.venv/bin/python -m unittest discover -s scripts -p 'test_storage_source_fence_*.py'`。真实验收入口为 `apps/api/.venv/bin/python scripts/verify_storage_source_fence.py --manifest /private/owner.json --summary /private/result.json`，两个文件均须不存在且父目录为 `0700`。它只创建随机所属的容器、卷、网络与回环端口，不读取应用配置或连接现有数据库／MinIO。它使用带历史版本、删除标记、既有规则、multipart 和已接受慢 PUT 的非空源，验证硬退出续跑、拒写、最终源清单与对象复制、准确读取及显式恢复。

私有 owner 清单保留真实资源 ID，正常结束按精确归属清理；验收进程被硬杀后，使用该脚本的 `--cleanup --manifest <原清单> --summary <新的私有汇总文件>` 恢复清理，不能按名称前缀批量删除。保留脱敏有界汇总，清理原始凭据、日志、报告和临时配置。此演练不代表应用路由已切换、真实 TLS 或生产迁移已经通过。

## 绑定窗口并封存最终增量

`scripts/finalize_storage_migration.py` 把实际原生封闭与应用窗口、新清单及 B5 后继复制绑定。命令由可信部署运维在当前应用数据库环境中执行，原生凭据仍只用上述两个操作进程变量；API 与 Worker 不需要 Docker 权限。`--actor-id` 为当前有效超级管理员 UUID。命令不接受上传的证明文件；bind／seal不发布业务读取路由，原件映射须另外显式发布。

1. 先完成初次复制及九用途处理评审，开启以该批次为父的迁移窗口，并停止登记访问。待旧任务与实际许可全部收尾后，实际执行原生 `seal`。
2. 选定一个新的最终核验 UUID，在同一私有配置与状态上执行 `bind`。它先持久保存应用绑定责任，再核对数据库、源／目标和窗口，递增窗口修订；之后生成的新任务会固定此修订。
3. 在该窗口重新生成完整源清单，以原父批次创建 B5 后继，执行增量复制及完整的 `verify`。完整核验会实际重读包括复用回执在内的准确目标版本。单项重试成功、准备完成或复制子集成功都不能代替它。
4. 用后继批次执行最终 `seal`。命令先冻结批次，再在无数据库事务的阶段重新物化全部检查点和核对实际原生状态，最后短事务封存结果。控制台窗口详情显示核验状态、观测时间、分项数量及最终复制批次入口。

```sh
# 两种 revision 分别取原生 status 与应用窗口详情，不能混用。
apps/api/.venv/bin/python scripts/finalize_storage_migration.py bind \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <new-uuid> \
  --window-id <window-uuid> --expected-window-revision <revision> \
  --expected-native-revision <native-revision>

# 清单、后继复制及完整 verify 收尾后：
apps/api/.venv/bin/python scripts/finalize_storage_migration.py seal \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <same-uuid> \
  --copy-id <successor-uuid> --expected-revision <finalization-revision>
```

最终源清单须完整、覆盖全部唯一源桶，当前对象均已核验 SHA，且无当前问题或未完成上传。九用途和共桶处理选择须明确；`migrate` 对象必须有准确且完整核验的目标版本，`retain / rebuild` 和删除标记分别保留事实，不能用普通 `skipped` 掩盖未核验、冲突或超预算。历史目录未知、未展开和旧批次事实继续单列，不要求全历史定位归零，也不据此声明保留对象已迁走或重建已经完成。

`sealed` 表示这一次窗口下的清单与复制证据已封存，附实际原生观测时刻；数据库记录不是当前源状态的永久证明。原报告中的 `source_quiescence_proven=false` 不改写，应用 `migration_ready=false` 保持，业务位置切换仍须后续执行。封存记录独立于单次任务保留，不因 AsyncJob 过期失去绑定与摘要。批次封存后不能新增尝试或后继；晚到物理回执仍保存，证据改变会使最终核验进入待解除状态。

### 中断、解除与恢复写入

使用 `status`（相同 `--config / --state / --actor-id / --finalization-id`）同时重新观测实际原生状态并查询绑定。中断后保留原 UUID 和 nonce 责任，先读取最新修订，再按原动作续做；命令不按 TTL 解除。配置漂移或源代次变化时拒绝继续封存，须先处理原责任并重新封闭、生成对应新窗口证据。

绑定存在时，原生命令的独立解封／重封闭及窗口直接关闭均拒绝绕过。需结束本次核验时显式执行：

```sh
apps/api/.venv/bin/python scripts/finalize_storage_migration.py invalidate \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <same-uuid> \
  --expected-revision <finalization-revision>
```

解除先持久进入 `invalidating`，禁止新任务读取；仍有旧任务或实际操作时保持等待，待真实收尾后以最新修订重试，成功保存 `invalidated` 后才清除原生绑定。这个命令本身不恢复写入。随后可另行执行原生 `release`，并按维护流程关闭应用窗口。

数据库登记应答未知时不清除本地 claim。若尚未确认登记、同一数据库中确实没有该记录，并在锁定原窗口及其当前 Gate 后核实无旧任务或操作，可用 `--expected-revision 0` 显式废弃该登记意图并保存解除历史，原窗口已关闭也可核实后废弃；已确认登记却丢失记录时保持未知，不采用此路径。不要删除状态、换最终核验 UUID 或改数据库来绕过责任。数据库失联时保留物理只读，恢复连接后按原身份核实。

## 发布托管原件读取映射

应用0263并刷新所属Worker后，可从同一个sealed最终核验发布托管原件映射。发布重新物化并核对全量Copy证据，在私有Journal锁内前后复核真实原生封闭，只纳入当前清单中已完整verify的复制对象。它不重新复制，也不要求先补认全库未知历史。

```sh
apps/api/.venv/bin/python scripts/finalize_storage_migration.py publish-reads \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <same-uuid> \
  --expected-revision <sealed-finalization-revision>

apps/api/.venv/bin/python scripts/finalize_storage_migration.py status-reads \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <same-uuid>
```

发布ID就是Finalization ID。`status-reads`只查询同一责任的耐久数据库记录，不表示当前原生栅栏有效；源离线时也可查询修订以续做撤回。原Task／样本／Asset、保存定位与创建回执不改写；托管原件读取根据完整原定位和保存SHA／大小，使用目标准确VersionId和ETag，取得目标位置的实际许可。源仍可保持停止，B的同名新版本或删除标记不会替换保存副本。业务权限、当前目标连接、物理身份及访问维护仍逐次核对。retain、rebuild和marker不生成可读副本；其他冻结包／派生物、新分配用途绑定及全量切换／回滚另行实施。

同一发布也用于训练保留包、冻结媒体／mask及已有产物的纯读取和导出ZIP的缓存核验／新签名。完整保存地址和SHA限定对象；有独立大小或保存源版本时同时核对。目标实际读取同时登记发布责任和原产物读取purpose，回收与撤回都等待真实句柄关闭。头像、报告附件、审计归档、RLE掩码、评论附件、指南图片及ready独立导出媒体也使用这一读取接缝。RLE按保存的压缩字节匹配，再核canonical内容；独立媒体的完整冻结inventory须与同一封存源prefix的当前可见键及内容全等。目标迁移prefix不作为原目录列举，部分retain／rebuild映射不提供完整目录证明。

有独立预期SHA的Mask导入暂存可读取准确副本，保留上传owner、原计划与逐项结果，预检最终复核同一发布；这不授权导入输出向停止的A写入。已结束训练的日志完整核验后脱敏，以认证API交付有界内容，不再为下载向旧Run位置写派生物。API的日志下载描述明确要求认证，不能当作公开S3签名分享。

最终停写前，逐项处置旧位置的实际在途任务：Creation直传、预标输入租约、训练包／产物创建和export building须排空，或保留原责任后取消／显式重建。已分配owner不随新Binding重定向；ready文件可读不证明未完成创建已完成。旧签名及已接受PUT／multipart的关闭继续由原生栅栏证明，登记许可排空不替代它。清理沿保存的准确源地址执行，A停止则延期；Creation和Mask暂存当前没有自动远端回收，源字节及记录保留至明确观察／退役决定。无需强制所有历史任务成功或全部未知归零。

缩略图、图像金字塔、视频派生物和ready原图缓存也可读取封存副本。金字塔／视频只要求本次实际请求成员有合格映射，既有库存及当前owner／代际核验保持；未映射成员继续要求原A可读。schema 2的同源能力地址保留原逻辑身份，服务端以准确B版本处理HEAD、完整GET和Range，核对区间、大小、ETag与复制内容属性；部分GET不等同于重算整文件SHA。schema 1保持直接S3签名及原期限。原图缓存继续按规范原件位置授权和计费，保存的缓存位置不改写；实际读取操作按Entry身份跨位置阻止清理，不因临时租约过期忽略尚未关闭的句柄。发布撤回停止新准入，流式响应逐块复核、发现撤回即停止后续交付，已经发送的字节不能追回；Body或client关闭不明时保留实际B上的发布和owner责任。

新签名固定B的准确VersionId并使用B的实际可达端点。之前发出的A签名，以及冻结ZIP中已经嵌入的媒体URL，继续遵循原有效期；ZIP最长七天的期限须计入已有观察／退役安排，旧入口保留只读直至这些链接自然失效。不能改写冻结包或把发布撤回宣称为撤销已发出的S3签名。最终用途切换与新写入回滚仍须另行执行。

发布活动或撤回中时，普通`invalidate`拒绝解除最终核验。需要停止此映射时先执行：

```sh
apps/api/.venv/bin/python scripts/finalize_storage_migration.py withdraw-reads \
  --config /private/source.json --state /private/fence-state.json \
  --actor-id <super-admin-uuid> --finalization-id <same-uuid> \
  --expected-revision <publication-revision>
```

撤回先提交`withdrawing`，阻止新映射I/O，再等待实际属于此发布的许可全部结束；未结束时用返回的当前修订续做，不以TTL清理责任。`withdrawn`后保留映射历史、Copy和原生claim，不自动恢复A写入、不删除任一端对象；随后才可按原顺序显式`invalidate`与原生`release`。同一发布不能重新激活，新发布需要新窗口与新最终核验。提交结果未知时保留相同身份核实，禁止换ID绕过。即使原生状态未知，仍可撤回读取；这不授予源端解封资格。

## 九用途原子切换与新写入回滚

回滚用于切换后无法及时修复的目标端故障，例如持续写入失败、关键权限不兼容或不可接受的读写性能，由运维明确发起。正常切换成功后进入观察和旧实例退役，不需要执行回滚。切换前失败只需暂停迁移并保留A服务；若B已有新对象，恢复A写入仍须保留这些对象及业务引用。B对象受损或不可读时，还需要按备份／副本恢复，回滚本身不能补回丢失内容。验收中的一次回滚演练用于证明上述恢复能力，不要求正常迁移反复回退。

应用0264并刷新实际API／Worker后，由可信部署运维运行 `scripts/cutover_storage_migration.py`。切换范围是本次已封存A的完整九用途，目标为该Publication固定的B；A保持原生封闭及应用停止，读取发布处于active。单用途已提前漂移时整组拒绝，不将当前部分向量当成新的基线。历史未知继续单列，不增加全库补认门槛。当前双端原生回滚适配受控本机MinIO拓扑；其他供应商的原生封闭与部署资格仍须按实际目标实现和核实。

```sh
apps/api/.venv/bin/python scripts/cutover_storage_migration.py preview \
  --config /private/a/source.json --state /private/a/fence-state.json \
  --actor-id <super-admin-uuid> --cutover-id <publication-uuid> \
  --expected-finalization-revision <sealed-revision> \
  --expected-publication-revision <active-publication-revision> \
  --review-file /private/review.json

apps/api/.venv/bin/python scripts/cutover_storage_migration.py activate \
  --config /private/a/source.json --state /private/a/fence-state.json \
  --actor-id <super-admin-uuid> --cutover-id <same-publication-uuid> \
  --review-file /private/review.json
```

review所在目录须私有，文件必须不存在；命令独占创建0600文件并同步落盘。stdout提供审阅内容但不输出确认token。预览在事务外分别执行九用途已有能力探测，完整复核后签发五分钟确认；过期或任一代际、配置、Gate、发布身份变化均需重新预览。activate将九项绑定和组记录放在同一事务，代际各加一；已保存地址不改写。提交结果未知时先查询同一组，不能重复生成另一个ID。

```sh
apps/api/.venv/bin/python scripts/cutover_storage_migration.py status \
  --actor-id <super-admin-uuid> --cutover-id <same-publication-uuid>

apps/api/.venv/bin/python scripts/cutover_storage_migration.py begin-rollback \
  --actor-id <super-admin-uuid> --cutover-id <same-publication-uuid> \
  --expected-revision <active-cutover-revision>
```

`status`无需原生Journal或其中仍有A claim，只查询耐久组责任。`begin-rollback`进入`rollback_draining`，停止B新准入并等待实际操作。随后在B自己的原生描述／Journal中运行既有`storage_source_fence.py seal`，验证B唯一入口拒写、已接受PUT与multipart收尾。B描述、gateway和state使用独立私有父目录，不与A共用锁。此单端原生命令的操作进程使用B自己的`AAP_SOURCE_FENCE_ACCESS_KEY / AAP_SOURCE_FENCE_SECRET_KEY`。

组合回滚命令中，A凭据使用操作进程的`AAP_SOURCE_FENCE_ACCESS_KEY / AAP_SOURCE_FENCE_SECRET_KEY`，B凭据使用`AAP_TARGET_FENCE_ACCESS_KEY / AAP_TARGET_FENCE_SECRET_KEY`；两套均不写描述、状态或报告，不传给API／Worker，不跨端回退。B应用访问地址由完整拓扑核验推出gateway在frontend网络中的唯一IPv4:80，不使用裸MinIO地址，也不放开连接器回环地址限制。

```sh
apps/api/.venv/bin/python scripts/cutover_storage_migration.py bind-rollback \
  --target-config /private/b/source.json --target-state /private/b/fence-state.json \
  --actor-id <super-admin-uuid> --cutover-id <same-publication-uuid> \
  --expected-revision <rollback-draining-revision>

apps/api/.venv/bin/python scripts/cutover_storage_migration.py restore-source \
  --config /private/a/source.json --state /private/a/fence-state.json \
  --target-config /private/b/source.json --target-state /private/b/fence-state.json \
  --actor-id <super-admin-uuid> --cutover-id <same-publication-uuid> \
  --expected-revision <rollback-bound-or-current-restoring-revision>
```

bind先在B Journal持久保存独立rollback claim，再同事务停止B Gate、恢复九项A地址并进入`rollback_bound`；代际继续加一，不恢复旧数值。restore先提交唯一A release操作ID并进入`rollback_restoring`，再依次撤回A读取发布并等待实际许可、解除最终封存、解绑A claim、按保存ID实际恢复A写入及关闭窗口。最后复核九项回滚向量、A开放和B仍held，将B应用Gate恢复active并进入`rolled_back`。这只恢复保存B对象的读取准入，B原生仍拒绝PUT，rollback claim继续保留，普通原生release禁止解封。

中断或提交未知时先status取得当前组修订，再以同一组执行相应命令；restore从已有A release UUID续做，A claim已经解绑也能恢复，已经释放的A仍实际verify_open。B claim未确认不等于可删，不能因数据库暂不可达而解除任一物理封闭。普通维护恢复B和过早解除A最终封存均受组责任约束。

回滚后A旧对象与B新对象按各自保存定位读取并核验内容，业务引用及冻结事实保持；未来分配回到A。B上未完成写入须显式重建，不能把旧owner重定向。不要恢复旧数据库快照覆盖切换后的业务行，也不要删除B新对象。B保留、旧链接观察及两端最终退役继续独立安排。

### 一次性完整链路验收

`scripts/verify_storage_migration_finalization.py` 仅在已核对、业务表为空的当前工作树 `test` 模式运行。它创建独立的非空源与目标、API 和 Worker，实际完成初次复制、窗口绑定、新清单、复用准确回执的增量与完整核验、封存提交后硬退出续做，以及等待真实任务收尾的解除顺序。源和目标容器使用已有镜像，API／Worker 不取得 Docker 权限；人工验收环境保持独立。

```sh
pnpm dev:worktree -- exec --mode test -- env PYTHONDONTWRITEBYTECODE=1 \
  apps/api/.venv/bin/python scripts/verify_storage_migration_finalization.py \
  --manifest /private/finalization-owner.json --summary /private/finalization-result.json
```

需要浏览器核验时加 `--pause-for-browser`；私有 owner 目录中的 `ready.json` 保存独立页面、临时认证状态及继续标记，不能公开。脚本按精确进程与资源归属清理；异常退出后的 `--cleanup` 使用原 manifest 和新的 summary。最后销毁本次一次性 `test` 模式，保留脱敏有界结果。此演练不表示生产后端已选定、业务位置已切换或旧存储可退役。

原件读取的扩展验收使用相同的空test约束，将上面脚本换成 `scripts/verify_storage_migration_original_reads.py`。它在自有源中建立真实原件，复制封存并发布后停止源容器，在目标制造同键新版本及删除标记，实际核对认证HTTP图片／下载与独立Worker原件读取、逻辑事实不变、撤回拒读和原生claim保留。它不代替所有业务消费者或九用途最终切换资格。

训练及ZIP读取的扩展验收使用 `scripts/verify_storage_migration_artifact_reads.py`，同样要求所属空test模式。它核对源离线和目标同键新版本／marker下的认证训练包下载、独立Worker冻结媒体／mask和ZIP完整核验、新签名GET及真实打开GET的撤回排空；已发出的目标S3签名继续按原期限可用。其余保留文件的实际验收入口为 `scripts/verify_storage_migration_retained_reads.py`，参数相同；涵盖头像HTTP和七类读取的独立Worker，使用持久头像／RLE owner及其余物理读取接缝，核对源离线、目标foreign latest／marker、准确签名和真实GET撤回排空。各业务权限与owner最终复核仍由相应SQL回归验证，不将物理接缝测试称为全部业务流程验收。所属资源及launcher测试模式分别按owner清理，不使用人工环境作为测试库。

媒体缓存扩展验收入口为 `scripts/verify_storage_migration_cache_reads.py`，参数和空test约束相同。它建立持久缩略图／金字塔／视频输出及通过正常store创建的原图缓存，源离线且目标有foreign latest／marker时，核对同源HEAD／完整GET／Range、独立Worker缓存读取与金字塔／视频物理读取计划、旧schema直接准确签名，以及真实打开Range GET的撤回排空。视频payload为合成传输字节，不作为编解码或播放资格；物理计划不代替业务权限／当前代际的SQL回归。

九用途切换的实际入口为 `scripts/verify_storage_migration_cutover.py`，同样仅接受核验归属且业务为空的test模式和新的私有manifest／summary。它建立独立双端原生拓扑，以正常Task直传和独立Celery缩略图形成B新对象，切换并回滚后核对A旧／B新对象的认证读取、完整内容和保存回执；旧B签名仍有效时验证物理拒写，并核对未来分配回到A。缩略图使用已有、固定内容摘要且含libvips的Worker镜像，只读挂载当前API源码及共享Python包，仅消费本次media队列；主机Worker负责default迁移队列，不重建镜像。其资源由原owner清理，launcher测试数据库／Redis／九桶另行销毁；结果不授予生产切换或退役资格。

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
