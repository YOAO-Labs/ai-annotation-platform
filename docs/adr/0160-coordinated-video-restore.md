# 0160 — 视频执行与推理路由的受控恢复

- **Status:** Proposed，协调恢复切片的限定本地独立运行、消费者与故障恢复验收通过
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0158](0158-video-window-journals-and-replay.md) 与 [ADR-0159](0159-independent-labeling-video-writer.md)

## Context

视频窗口和最终写入已有持久账本，但恢复数据库副本不能直接恢复执行权。Jobs 的证明密钥绑定数据库 OID、名称和集群身份，Inference 的路由权威也有数据库身份围栏。不同数据库会拒绝复制来的证明；同身份的时间点恢复仍需防止旧进程持有的证明重新生效。手工改写 realm 会丢掉这层保护，也无法证明后台请求已经结束。

备份之后可能出现新的 route scope、请求或 epoch。因此，备份中的 scope 列表与 epoch 不能单独证明物理执行已退休。恢复还必须保留原输入版本、冻结模型身份、完整计划、窗口原始字节和最终发布收据。后台进程重启产生的新 incarnation 不能冒充原冻结执行环境。

## Decision

在既有 Inference 工件内提供离线 operator 协议 `aap-video-restore/v1`，不接入普通 API 或默认服务启动。专用 `video_restore_operator_client` LOGIN 只执行 `aap_inference_routing_v1.video_restore_step(text,text)`。Inference 持有恢复编排与退休证明；Jobs 通过仅授予 Inference owner 的固定内部 capability 重新绑定自有执行账本。operator 不读取或修改原表。

命令固定 restore ID、备份摘要、外部围栏记录摘要、源／目标 Jobs 与 Inference realm，以及源权威密钥指纹。密钥指纹不是原始密钥。`inspect` 只返回构造命令需要的非敏感身份与指纹；`status` 返回持久恢复记录。相同 ID 与相同命令重放既有操作，改变内容则拒绝。

### 外部前提

操作人员先建立源 API、beat、各 worker、独立 owner、数据库及对象存储凭据的围栏，并确认旧连接与在途写入已结束。命令中的 `fence_sha256` 只绑定该外部记录，不能自动证明围栏已建立。健康恢复 marker 不是全局写入围栏。目标保持业务写入关闭，operator 只在明确的隔离目标上运行。

### 持久恢复状态

`prepare` 核对复制来的源身份／指纹及当前目标身份，持久化控制 ID、新 epoch、challenge、签名密钥和原 transport 配置。已有未完成恢复、不能解释的管理／退休过渡或不支持的后台状态均拒绝。Native 写入和恢复操作共用事务屏障，准备前先排空已取得权威的事务，准备后关闭普通 Jobs／Inference 权威。相关 Native 入口要求 READ COMMITTED，较旧的重复读快照不能在等待后继续沿用旧围栏。此屏障不替代普通 API ORM、源端进程或存储凭据的外部围栏。

operator 通过认证且有响应大小限制的 HTTP status 读取实际后台。只接受冻结 incarnation 的稳定当前 epoch，或与本次控制 ID、epoch、challenge 完全一致的重放。实际 epoch 可以比备份中的 epoch 更新，记录它之后按原 predecessor CAS 执行全局 drain 和新 epoch bind。仍有活跃请求时保持准备态；重复同一命令继续，不能新建一个控制操作替代未知结果。物理请求结束并不依赖备份中是否存在其 scope。

每个控制器均已 bound 且活跃请求为零、transport 和配置仍匹配后，才允许原子完成。该事务轮换两类权威密钥并绑定目标 realm，使整个变更权威范围内的旧执行证明失效。Jobs 解密并验算原路由命令，再用新密钥包裹相同内容，保留 Job/run/generation、冻结计划、窗口、业务状态和不可变发布收据。随后由普通 claim 产生新代次和新证明；operator 不直接派发作业或授予业务写入资格。

旧 Inference scope 的原 realm、epoch 和 attempt 事实继续保留。恢复以追加的目标 realm 退休证明记录旧 scope、实际 retired/new epoch、incarnation 和恢复／控制 ID，不能为外来 scope 伪造普通 capability。当前 Jobs 证明通过验证，且旧 intent 有精确的恢复退休证明时，scope status 才可返回空 scope，跳过普通 per-scope RPC；`recovery_ready` 仍独立核对所有历史 intent。未封存 attempt 按既有恢复规则退出，封存窗口继续复用。

### 不明结果与回退

HTTP 或数据库响应不明时保留持久围栏和固定控制 ID。命令重放从已记录的状态继续；完成重放直接返回原记录，不联系后台。没有自动放弃、取消恢复或回滚到旧权威的入口：部分后台可能已接受新 epoch，启用旧权威会让事实与执行环境分裂。完成记录及退休证明不可改写或删除；存在恢复状态时拒绝降级移除该能力。

## Consequences

该能力让恢复交接有单一 operator 合同、最小凭据和可审计的控制流程；普通模块不会获得跨 owner 原表权限。它保持精确 backend incarnation 与模型／能力身份检查。不同后台 incarnation、未知控制过渡或输入对象版本缺失仍拒绝同作业续跑，需要单独的业务处理。

对象存储备份与版本身份恢复不由此 operator 承接。输入必须保留原 `VersionId`、ETag、大小与 SHA-256，并由真实消费者验算；不能用重新 PUT 的相同字节替换冻结引用。该恢复切片也不代表完整 Labeling、Inference 或十模块／七门槛目标已完成。

## Verification

五个选定 Native／真实消费者场景均已通过：跨 realm 恢复及最小权限、同数据库身份轮换且等待在途写事务、后台冷重启拒绝、晚期事务故障回滚与命令重放、真实 runner 中断后的恢复。CPU 控制器包含备份之后的真实活跃请求及比备份更新的 epoch；完成后再做正常 epoch reconcile，退休证明与 Jobs 恢复仍有效。READ COMMITTED 以外的隔离级别被拒绝，旧证明失效，新 claim 保留 run 并取得新代次。

实际消费者主验收使用非 editable 安装的 `aap-video-restore` 子进程，凭据仅经当前用户专用文件传入。源 runner 封存第一个窗口后在下一窗口请求期间被终止；真实 `pg_dump/pg_restore` 后，CLI 保持准备态直至旧请求退出，再完成绑定。目标 Assets Media 再次读取同一 S3 VersionId、SHA-256、大小与字节；目标 runner 复用封存前缀、计算余下两窗，经 Labeling owner 发布六条 bbox 结果与不可变回执。重复 CLI 命令及 status 返回相同完成记录。此验收复用同一 verifier-owned 对象存储，不验证对象存储备份恢复。

源码与独立安装包的 Inference／共享合同套件各通过 420 项；非 editable 安装的六个包、82 个源码文件逐字节匹配，不安装 API、ORM、Celery、对象存储 SDK 或 ML 包。原路由、scope 退休、执行代次、写入围栏与最终发布的 51 项受影响回归全部通过。完整迁移验证通过 fresh head、可逆段退至 base、110 步后缀回退／再升级、历史转换与备份恢复；四个派生数据库清理完成，无残留。

本地证据分别保留在 `.worktree/video-restore-native-fourth.*`、`video-restore-live-fifth.*`、`video-restore-consumers-eleventh.*`、`video-restore-worker-installed-cli.*`、`video-restore-existing-regressions.*`、`video-restore-installed-inference.*` 与 `video-restore-migration-validation.json`。旧失败报告继续保留，不合并计数。GPU、生产环境、跨平台容量和全对象存储恢复不从 CPU 本地结果推断。

实现提交为 `968212e4e0a8c812fa5a8d79606d38fa0a58f953`。匹配该提交的 ARM64
Inference 镜像为 `sha256:3a02a2c399cc688c1880439f46e76f9e2f38a28cc155b9c234e2df79a09bb2f3`，API 镜像为 `sha256:0951eca34a3f514434087cf9314464dfa90346b7cefef0403e4397a8fc9f4b9c`。
Inference 在 UID10008、禁网、只读根文件系统、1 CPU／256 MiB 下冷检通过，82 个包文件
与源码一致且安装了离线 CLI entry point；API 在 UID10001、禁网、只读、1 CPU／512 MiB
下验证 484 个安装包文件、1281 个应用／Native 输入、当前迁移 head 与不变的公开 OpenAPI。
两项 proof 容器均已移除。八张恢复表的真实 ORM autogeneration 对照另通过一项，普通未映射
控制表仍可报告删除。最终格式化的六个 Python 文件与通过验收时的 AST 相同，已重新安装并
核对最终包指纹，复用既有行为证据。

汇总证据为 `.worktree/video-restore-milestone-qualification.json`；逐例来源保留在原 JUnit，
只提取各 cohort 中实际通过的对应场景，不把含其他失败的整份报告标记通过。镜像报告为
`video-restore-inference-image-qualified.json` 与 `video-restore-api-image-final-artifact.json`。
源码已集成，独立安装 CLI 已实际运行；日常主环境未切换，未推送或部署生产。
当前完成的是此协调恢复能力，完整模块仍按唯一[剩余工作台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进，下一项为完整 Identity。
