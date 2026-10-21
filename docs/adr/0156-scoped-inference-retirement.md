# 0156 — 推理执行范围的定向退役与作业交接边界

- Status: Proposed，定向退役已通过限定本地验收；完整视频交接仍未完成
- Date: 2026-10-22
- Deciders: Pending maintainer review
- Supersedes: None；扩展 [ADR-0155](0155-durable-inference-routing-authority.md)

## Context

视频跟踪作业在整个运行期间固定一个后端实例。worker 崩溃后，持久账本中的活动或未决
attempt 不能因租约到期而清除。现有后端排空／绑定针对整个 authority epoch，既会等待
其他作业，也会改变其他作业的准入，无法独立接续一个作业。

本决策提出时，视频 runner 只接收 queued 作业，结果、窗口衔接几何、实例映射和模型 session 状态
保存在进程内。后续执行代次与绑定见 [ADR-0157](0157-video-execution-claims-and-route-binding.md)，
窗口检查点与自动恢复仍待实现。定向退役是交接的前置能力，不能把
退役接口实现视为视频作业已经可以崩溃续跑。

## Decision

在既有 Inference owner 和后端路由控制器上增加按 scope 的退役协议，继续复用原来的
独立运行单元、控制认证和显式数据来源。原 epoch 协议及普通准入不更换语义。

1. Native 短事务验证 scope capability 与数据库 realm，持久化同一个退役身份，关闭旧
   scope 的后续准入，并固定后端配置、进程身份、authority epoch 和 token 有效期上界。
2. 提供方在事务结束后向实际后端发送认证退役命令。后端先在控制锁内屏蔽该 scope 的
   延迟 token，再等待该 scope 的请求和已登记后台工作真实结束；其他 scope 继续运行。
3. 回包必须匹配退役 ID、scope、registry、epoch、后端 incarnation 与 challenge。
   只有 retired 且精确活动数为零，Native 才将该 scope 的活动／未决 attempt 标为
   reconciled；既有响应历史和其他 scope 保持原事实。
4. 同一个退役 ID 可查询或继续其已持久化阶段。提交结果不明时只查询一次状态；控制
   响应丢失不会触发模型请求重发，也不能宣告原推理成功。

Native 签发 token 时在同一事务记录实际 exp 上界。迁移前的 token 没有完整有效期记录，
因此迁移使用保守的剩余有效期上界；不能从 scope TTL 或 attempt 的事务时间推算。
后端屏蔽记录覆盖 token 上界及保留余量，活动工作结束前不清除。新 incarnation 的
零计数不证明旧进程已经停止，不能替代原身份的退役证据。

后台工作必须在 handler 完成前登记，这是后端提供方的合同前置条件。越过退役或 epoch
屏障才登记工作属于违规，会封锁新的准入及后续退役／全局排空证明；它不能追溯撤销
已经发出的证明。因此，任意未登记线程或任务不在本协议可证明的范围内，不能把该类
后端列为合格提供方。仓库当前登记点位于请求执行及超时／取消处理路径；其他实现
仍须以自身实际模型的后台工作验证这一条件。

新增一项固定 `scope_retirement_step(text,text)` 能力，支持 policy、prepare、status、
finish 四个封闭动作。既有 routing policy 与十九项 core 动作保持；受限路由 LOGIN 的
执行面明确增加这一项能力，不增加原表读取、写入或通用 SQL 权限。公开业务 API、SDK
响应与作业模型不在此阶段改变，scope capability 和控制凭据不进入事件或公开结果。

## Consequences

正向：实例内可以单独关闭一个执行范围，并依据真实后台工作确认安全边界，为独立 Jobs
owner、worker 交接和语言替换提供可调用的协议；不用停止无关作业来模拟交接。

负向：旧后端不可达或进程身份改变时仍保留未决状态；定向退役不能证明未知推理结果，
也不序列化模型隐藏状态。它不改变 GPU allocation 或预算，不能替代 GPU 驻留终态证明。
它增加一个持久控制阶段和一个受限 Native 能力，需要纳入
安装包、角色、迁移／恢复与实际消费者资格验证。

完整视频交接仍需 Jobs 持久执行代次、调用前保存的 acquire 请求身份、私有路由绑定、
逐窗口检查点，以及 Labeling 对全部结果／Mask root／失败终态写入的同事务代次围栏。
模型 session 不可序列化时还需定义上下文重放，不能静默替换已提交窗口结果。

## Notes

限定验证包含受限 Native 角色、真实 TCP、请求／已登记 future、并发的无关 scope、延迟
token、丢失控制回包，以及三种 API 连接来源。独立非 editable 工件通过413项检查，
六个包的77个文件与源码一致，未安装 API／ORM／Celery。实际安装入口完成停止、重启
和状态恢复，状态查询没有重复控制；非空克隆保留退役记录、函数与权限，同时拒绝复制
来的 capability，源库和克隆记录均保持不变。所有验证仅使用本地 CPU 夹具和临时资源。

本 ADR 的实现与验证证据在本地实施记录中单独登记；完整 Inference、十模块／七门槛、
受限镜像、替换语言、真实 GPU 与生产容量仍按各自门槛推进。主环境没有切换，Go 仍是
独立实验。
