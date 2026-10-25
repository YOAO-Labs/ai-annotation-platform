# 0157 — 视频执行代次、原子路由绑定与当前写入检查

- Status: Proposed，执行权与绑定通过限定本地验收；完整恢复继续实施
- Date: 2026-10-22
- Deciders: Pending maintainer review
- Supersedes: None；扩展 [ADR-0156](0156-scoped-inference-retirement.md)

## Context

单个 scope 的退役只能证明该范围的旧工作结束。视频 worker 还需要区分当前执行者，
避免重派发创建多个 scope、旧 worker 写入新作业结果，以及 broker 异常覆盖主作业状态。
仅在调用前检查一次 token，或在另一个已提交事务中取得布尔许可，都不能保护最终写入。

现有 runner 保存候选、窗口衔接几何、实例映射和模型 session 于进程内。因此领取执行权
不等于能够恢复模型上下文；租约到期也不证明旧推理已经停止。

## Decision

Jobs 增加固定 `video_execution_step(text,text)` 能力，封闭为 policy、claim、renew、guard
和 release。逻辑 `run_id` 不变，接替时递增 generation 并更换 worker/token；只存 token
的 HMAC。执行权绑定 PostgreSQL cluster system identifier、数据库 OID 与名称。原地时间点
恢复仍需外部 restore epoch，不能由这组身份发现同一 realm 内的历史回退。

所有领取及路由绑定先锁 Labeling 主 Job，再锁 Jobs 私有执行行。guard 与业务写入处于
同一事务，持锁到提交或回滚；独立 asyncpg 适配器拒绝跨事务复用。单函数 LOGIN 为
`video_execution_client`，没有原表、序列、schema CREATE 或数据库 CREATE/TEMP 权限。
API 兼容桥借用现有业务连接，不把 guard 改成 HTTP 请求。

Inference 增加私有 `video_acquire` 命令及固定 `video_execution_scope_step(text,text)`，
路由 LOGIN 的执行面明确为四个函数。core policy、core step 的19项动作与旧 acquire
摘要不变。新命令保存完整加密请求、摘要、稳定 request ID 和由其派生的 scope ID；候选
选择仍由同一 Python 策略完成。预留 scope、一次性事务 permit、Jobs 绑定与 acquire
回执共同提交或回滚。成功与策略拒绝都保存精确回执；重放先验证当前 claim，再读取旧结果。

同一代次只能绑定一个 intent。已绑定旧 scope 在精确退役完成并 reconciled 前不能被
新 scope 替换。过时代次不能新增或重放 attempt 换取新准入，也不能续租；已签发 token
仍由真实后端退役屏障处理，不能宣称数据库代次立即撤销后端已接受的请求。

当前 runner 的启动、进度、候选、Mask 根和失败写入接入同事务检查。已知短暂锁竞争
回滚后重试，保留持有的推理结果与 Mask 数据；失去执行权的 worker 停止写入，不将
替代者标记为失败。取消允许当前 worker 续租、关闭 session 并暂存普通追踪的部分候选，
新推理仍被拒绝；修正取消沿用不保留修正候选的规则。原 actor、来源与采纳检查保留，
不套用首次关联建题的接受时授权规则。

后台汇总使用与主 Job 一致的稳定 UUID，在并发创建和重派发后复用原记录。它只有在
执行权检查后进入 running，仅投影锁定的主 Job 终态。无 scope 的明确拒绝可在核对持久回执后释放执行权，未知或已绑定意图仍要求退役。
成功 acquire 的状态回退同样重验当前代次、scope 和预留有效期；不能借历史成功回执恢复旧执行。

broker failure/revoke 不持有
执行证明，不能直接将新的受管汇总标记失败或取消；旧汇总保持原逻辑。

该执行权阶段先提供冻结计划、结果规范化和 checkpoint 分块的 portable 值合同。源时间线采样
与标注网格独立；缺少后端 incarnation、模型、权重或能力身份时可首次运行，但拒绝复用
checkpoint。窗口持久化、自动退役协调与模型 session 重放由后续
[ADR-0158](0158-video-window-journals-and-replay.md) 扩展。

## Consequences

当前 worker、Inference owner 和 Jobs 能以明确的私有协议组合执行归属，同时保留原业务
事务。执行权、scope 与回执之间不再存在独立提交产生的孤儿准入窗口。

代价是新增持久 claim、intent 与续租，Job 写入和路由操作会发生短暂锁竞争。lease 只
决定执行者，不能用于推导结果成功、后端无工作或 GPU 预算可释放。终态／已删除 Job 的
遗留绑定可取得 cleanup-only claim，不能恢复业务写入或创建新推理。

当前 Labeling ORM 仍是业务 writer。代码入口已检查代次，不代表旧二进制或任意原表
writer 已被数据库统一围栏，也不代表独立 Labeling writer、全 Jobs 或完整模块已完成。
窗口恢复的限定资格见 ADR-0158；原地还原 epoch、受限镜像、另一语言提供方及生产容量
仍有各自门槛。Go 仍只作为实验，日常与生产环境不切换。

## Notes

本地32项选定验收包含 Native 领取与接替、锁竞争、取消时续租但拒绝新 attempt、最小
权限 LOGIN、跨事务 guard 拒绝、稳定汇总，以及 embedded／DSN／HTTP 三种实际视频
链路。每种视频链路都重放同一 acquire、确认只有一个 scope 和两次实际模型调用，取消
第二窗后保留首窗候选。完整迁移覆盖 head0281、回退0189、冻结 SQL 安装回滚和重新授权。
临时数据库、存储身份和运行资源均已清理；早期失败回执单独保留，不计为通过。

该批次使用本机 ARM64、真实 TCP、版本化对象存储和 CPU 模型夹具，没有物理 GPU、
Ubuntu AMD64、远程 CI 或生产资格。独立安装的 Inference 与结果契约包分别通过418／72项检查，包文件指纹与最终源码匹配；
源代码、兼容角色及其余检查在实施记录分别登记，重叠检查不叠加。
