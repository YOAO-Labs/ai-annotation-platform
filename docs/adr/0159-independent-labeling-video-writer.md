# 0159 — Labeling 视频 worker 最终写入 owner

- **Status:** Proposed，最终写入与真实进程恢复通过限定本地验收；完整模块继续实施
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0158](0158-video-window-journals-and-replay.md)，不替代其窗口账本决策

## Context

窗口账本使视频跟踪可以固定原输入、保存完整执行计划并恢复已封存窗口。此前 worker 的最终候选与 Job 状态仍由 API runner 直接处理；这让 Labeling 的最终业务写入边界继续依赖 API 的 ORM 装配。一次成功、取消或失败终结必须同时保留正确候选、Mask 引用、Job 状态和可重放结果，不能拆成跨事务的若干更新。

这项切片只移动视频 worker 的最终结算，不代表整个 Labeling 拥有独立凭据、部署或可替换语言。Go 月报服务仍是架构实验，与本协议没有运行依赖。

## Decision

增加 `apps/labeling` 私有协议 `aap-labeling-video-writer/v1` 与 Native envelope `aap-labeling-video-writer-data/v1`。命令固定 operation UUID、job/run/worker/generation/lease、outcome、可选已接受输入断言、候选断言和失败说明。operation UUID 由 run 稳定派生；每次重试保持相同命令和摘要。业务事务提交结果不明时，调用方查询相同 operation、执行证明与 request digest 的 status，不重新运行模型。

Labeling `video_writer_step(text,text)` 是写入入口。`labeling_video_writer_client` LOGIN 仅可执行这一项 Labeling 函数。它不读 Jobs 或 Labeling 表。Labeling owner 通过 `aap_jobs_v1.video_writer_authority(text,jsonb)` 这个固定内部 wrapper 获取当前执行证明、Job、窗口与终结能力；read_publication 只开放 job/run 回执读取，不读取窗口 payload；wrapper 只授权 Labeling owner，不能转调任意 Jobs RPC，也不开放 Jobs 原表。Job 状态、staged candidate、Mask 根交接和不可变 receipt 在一个事务中提交。

HTTP status 有两个不同的封闭请求。常规 operation status 仍要求 schema、operation ID、当前执行证明与 request digest，用于判断同一事务的结果。Native read_publication action 对应的 HTTP publication lookup 只接收 schema 与当前 execution proof，按当前 Job/run 查找已存在的不可变终态回执。新代次取消清理先执行 publication lookup；找到回执即返回原结果，不重写旧候选。没有回执时才按取消规则恢复封存前缀。

## Canonical results and authority

成功候选只从冻结计划对应的 Jobs canonical window 账本生成。owner 以 ordinal 读取每个 manifest 和原始 UTF-8 payload 文本，核验字节摘要、计划、前驱、窗口与 attempt 后组合 track 阶段结果；discovery 窗口不生成候选。调用方提供的非空完成列表仅作为断言，必须逐项等于重建值；成功需要全部计划窗口及必要的 session-close 事实。

普通追踪取消必须先有数据库的实际 cancellation marker。它保留封存的 canonical track 前缀。可选尾部只允许来自当前执行代次中已开始／捕获的下一计划 track 窗口，并逐帧满足窗口边界；新代次恢复没有 worker 本地帧行，只重建封存前缀。纠错取消绕过 candidate stage 授权并清空纠错候选。失败结算不重新读取或重新授权已损坏的计划，并保留已取消作业原有的取消候选。已接受作业的 tracker actor、来源版本、Task、segment 和当前作业状态检查继续生效；建题流程的“接受时保留权利”规则不会转给视频 tracker。

引用的 Mask 对象键在写入事务内排序并锁定，再由 Assets Mask Content owner 验证字节。成功候选的 Mask 根与待审／标注根原子交接；bbox-only 结果无需 Mask Content 凭据。请求最多 65 MiB，最终 staged JSON 保持已有 64 MiB 限制。原窗口文本保留数值字节；最终 JSONB 沿用数据库对带符号零的规范化，收据摘要绑定写入前的原始候选字节。既有通知事件仍为尽力交付，持久状态与候选以数据库为准。

## Bindings and runtime

API 使用同一便携管理器与 SQL 合同，提供三种绑定：

| 绑定                | 事务与用途                                       |
| ------------------- | ------------------------------------------------ |
| 借用调用方 Session  | 默认模式，由 API 保留提交权                      |
| 专用 PostgreSQL DSN | 专用函数 LOGIN 在独立 READ COMMITTED 事务内调用  |
| HTTP owner          | 显式私有 control bearer 调用独立进程，不静默回退 |

URL 与 DSN 互斥。独立进程复用 `apps/labeling` 工件，入口 `aap-labeling-video-writer`，默认监听本机 8180。HTTP 只开放 `/v1/labeling/video-writer/execute`、`/v1/labeling/video-writer/status` 与 health/readiness；它不是公开 REST API。Mask 校验仅通过 Assets Mask Content owner，不在 Labeling 进程中直接访问对象存储。

## Consequences

Labeling 获得 worker 最终结果的唯一原子发布路径，且普通重试只重放稳定写入命令，不会重复推理。SQL 函数保持数据所有权在数据库事务中，不依赖跨进程共享 Session。

此次边界有意保持窄。Human accept/decide/discard 以及中间 start/progress 写仍由 API ORM 持有。本地验收已覆盖六个 bbox／Mask × 借用事务／DSN／HTTP 真实消费者、独立 HTTP 进程重启、当前证明下的旧收据查询，以及进程中断后的取消前缀恢复与再次投递。原有三个进程中断恢复场景也继续通过。非 editable 安装运行同一套 60 个包用例，148 个便携包与运行时文件逐字节匹配源码，且不安装 API、ORM、Celery、对象存储 SDK 或 ML 包。受限 owner 镜像在 UID10006、只读、禁网、1 CPU／768 MiB 下验证了148个包文件；匹配 API 冷镜像验证480个包文件与1278个 API／native 输入，OpenAPI 不变。实际容器消费者覆盖 bbox／Mask 与重启；每种形态各16个收据读取、并发4，锁争用可重试。这是有限读取采样，不代表写入或生产容量。

有限 bbox 写入采样另对 direct-Postgres 与 HTTP 各发布 16 个独立作业，每作业 3 个窗口、6 条帧结果，并发均为 4；32 个收据与 192 条规范帧行及来源指纹均核对通过。直接绑定 96 次 Native 调用，HTTP 16 次请求，两侧均无忙重试。单作业中位数为 169.48／178.93 ms，最近秩 p95 为 186.75／184.55 ms，总耗时为 703.73／735.12 ms。采样使用同一受限 owner 容器，数据库在容器外；直接绑定增加一个容器内 Python 进程，HTTP caller 在宿主机。结束时 cgroup 当前内存 87.08／60.30 MiB、HTTP owner RSS 61.84／62.25 MiB，不是峰值或等预算对照。样本没有模型调用、队列、最大缓冲区、大 Mask 或旧实现延迟比较，不构成生产容量门槛。

同实例非空 `pg_dump/pg_restore` 验证保留完整计划、窗口分块（含带符号零及科学计数法字节）、不可变收据和 helper／ACL；存在收据时拒绝降级。副本保留源权威身份并拒绝旧证明，未执行手工 realm／secret 改写。受控重新绑定已由 [ADR-0160](0160-coordinated-video-restore.md) 提供，并通过独立安装 CLI、真实 CPU 后台及 runner 的限定本地中断续跑验收。验收复用同一 S3 原始版本；Mask／原片对象备份恢复和完整容量门槛仍未完成。完整 Labeling 模块、全部模块的独立部署和语言替换均未完成。不能以该切片作为生产切换或跨平台容量证明。

## Notes

- Portable policy、wire schema 与运行时位于 `apps/labeling/src/aap_labeling/video_writer*.py`。
- API 集成位于 `apps/api/app/services/video_tracking/writer.py` 与 `runner.py`。
- Native capability 与 receipt 位于迁移 `0283`；部署角色来源见 `infra/docker/labeling-video-writer-grants.sql`。
- 开发启动、配置及权限顺序见仓库 `DEV.md`；协议调用见 `docs-site/api/guides/labeling-runtime.md` 与 `docs-site/api/guides/module-data.md`。

## Terminal publication and cleanup lease

Native renewal validates realm, generation, token and expiry before returning
`job_not_active` for a terminal Job. The worker stops renewal on this definite
reply while retaining the current proof for physical retirement within its
remaining lease. It does not grant further business writes; Native Job and lease
guards still reject them. Unknown renewal, replacement, realm mismatch and expiry
continue to stop writes. Container tests shorten the renewal interval to exercise
this path during restart and concurrent receipt reads.
