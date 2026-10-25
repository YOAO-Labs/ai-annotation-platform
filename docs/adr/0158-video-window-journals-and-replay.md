# 0158 — 视频窗口账本与会话重放

- Status: Proposed，窗口账本与本地进程接管通过限定验收；完整模块继续实施
- Date: 2026-10-22
- Deciders: Pending maintainer review
- Supersedes: None；扩展 [ADR-0157](0157-video-execution-claims-and-route-binding.md)

## Context

执行权与路由绑定能拒绝旧 worker 写入，但不能恢复进程内的候选、实例关联和模型上下文。
直接从当前配置重新分窗会改变已经接受的作业；把同一窗口的重放输出覆盖原结果，则会丢失原始证据。

Assets 的源时间线采样和 Labeling 的输出网格是不同事实。只保存 grid step 无法重建原始输入，
过期 URL 也不能作为可恢复的来源身份。

## Decision

Jobs 增加固定 `video_execution_checkpoint_step(text,text)`。执行 LOGIN 的精确权限面扩为
两项函数；原执行权 RPC 的 policy 仍描述其自身函数。新 RPC 保存不可变完整计划、逐窗口
结果、代次内 capture/parts/attempt 和独立的 session 关闭事实，不取得跨 owner 原表权限。

Labeling 和 Assets 分别通过窄 owner capability 返回原始请求与已就绪输入事实；Jobs 校验
job/input/task/project、请求摘要及精确版本后冻结计划。采样步长、选中帧数、源 SHA 和测量
事实来自 Assets 的不可变计划，不由标注网格或当前 Task 元数据推导。Inference 仅向 Jobs
暴露最小 scope 事实与精确退役判断，Jobs 不获得整个路由写 RPC 的调用权限。

窗口使用实际选定实例及模型能力，包含平台限制、模型窗口上限、session context 上限和
创建修正时接受的限制。明确的逐模型权重 release/SHA 成对保留；旧 version label 不代替
权重身份。内置 `mock_bbox` 使用实际适配器和几何辅助函数源码摘要，没有虚构 registry 或
权重。缺少完整兼容身份允许首次执行，但不允许复用已保存窗口。

兼容摘要还绑定模型默认阈值／变体、实际 registry dispatch 参数及平台结果处理策略。
本次执行固定低置信度阈值；新进程配置不一致时拒绝复用，避免同一作业混用不同的 outside
判定。运行身份读取暂不可用时暂缓执行，不据此把已保留结果标记失败。

每次计算前先持久化 begin。完整响应的规范化候选与后续状态通过稳定 checkpoint ID 和
前驱 manifest 摘要关联。单次 RPC 最多 2 MiB，窗口 payload 最多 64 MiB，分块最多
512 KiB、128 块，元数据分页最多 100 窗；没有逐帧 SQL 调用。只有字节、分块及身份核验
完整后才能发布 canonical window。持久化竞争重试持有的结果，不重新调用模型。

恢复先通过配置的 Inference owner 读取旧 scope，并取得实际后端退役证明。未知结果仍
暂缓；claim 到期不代表工作完成。Native 确认本次逻辑 run 的所有旧范围均已退役后，清除
旧 route pointer 并允许新准入。私有 HTTP status 使用现有鉴权，严格校验完整执行证明，
不通过 API 数据库绕过已配置的独立 HTTP owner。

已闭合 context span 复用原窗口；未闭合 session 从其原种子重建新上下文，重放已完成
前缀并严格比对候选、continuation 和 discovery seed。观察到的响应摘要可以不同，原始
canonical payload 和 manifest 不被覆盖。session token、签名 URL 和凭据不进入检查点。
普通追踪取消保留已持有的部分候选，修正取消不保留修正候选；当前 actor、来源和接受规则
继续生效，不套用首次关联建题的接受时授权规则。

Mask 根在 writer finish 前保留，并与最终 staged/annotation 根在业务事务中交接；输入
和原素材保留到精确 execution retire。取消状态本身不能证明 worker 已完成部分结果暂存。
成功要求全部计划窗口和 session 关闭事实齐全；后续接受取消候选不重写原 writer outcome。
正常成功或失败若仍需退役，Celery 保持重投，汇总仍以已提交的主作业终态为准。
任务使用延迟确认和 worker 丢失重投；续租响应未知时旧证明停止写入，主作业仍活跃则保留
重派发机会。业务提交确认丢失时重读已提交的终态并完成精确退役，不把清理跳过为正常空返回。

## Consequences

计划、结果传输和恢复判断成为可独立实现的协议；当前 Python runner 使用同一套便携值
校验，Labeling ORM 继续持有最终业务事务。这不是完整 Jobs 或 Labeling 独立部署的证明。

未完整封存的窗口仍可能在崩溃后重新计算；已经封存的结果保持不变。session 提供方必须
能够重建并产生相同的规范化输出，否则恢复暂缓。缺少兼容身份或身份变化时不能自动换
实例续跑，需要取消后重新发起。模型后台工作登记仍是退役证明的前置合同。

原地 PITR 的外部 restore epoch、恢复后安全重绑定、独立 Labeling writer、完整模块凭据、
提供方一致性和容量门槛仍需分别完成。scope 退役不释放模型驻留或 GPU 预算。Go 保持实验，
当前改动不切换常驻开发或生产服务，不构成 GPU、AMD64、远程 CI 或生产容量资格。

## Notes

当前本地选定台账包含 126 个去重用例，保留各轮原始失败／中断回执，并为每项登记最近
的通过证据。真实 SIGKILL 覆盖 stateless 复用和 session 重放：旧请求仍活跃时不创建替代
范围；退役后保持原首窗字节／摘要、完整输入版本与候选唯一性。初始 worker 和接管 writer
使用兼容运行账号，Inference HTTP owner 使用函数专用连接。嵌入／DSN／HTTP 三条普通
视频链路也验证了取消、原始版本固定和精确退役。

单独的真实 Celery prefork 验收使用实际注册任务与确认配置，只替换异步业务入口：杀掉子
进程、保留父进程后，同一消息由新子进程重投执行；该用例不替代完整视频业务验收。另一
真实窗口链路在最终业务提交成功后模拟确认丢失，保持原窗口、候选和精确退役／保留释放。
素材与 Mask 回归保留原辅助引用规则；自动生成迁移以 schema 和表名区分 Native 私表，
仍报告无关表删除。

API／流水线／架构选定源码用例去重后 432 项通过，另有 148 项为不适用的参数规则跳过；
脚本检查 20 项通过。全新非 editable 安装分别通过 Inference 430 项、结果包 118 项，
78／23 个包文件与源码逐一一致，未安装 API、ORM 或 Celery。这些计数属于不同验证面，
不相加作为完整模块资格。当前本地回执入口为 `.worktree/video-window-checkpoint-qualification.json`。

该批次的真实中断使用本机 ARM64 与确定性 bbox 工件。真实 Mask／纠错崩溃、恢复期间
撤权／取消、非空窗口账本还原、受限镜像和容量仍需各自的限定验收；不由这里的通过数推导。
