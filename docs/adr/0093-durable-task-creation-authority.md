# 0093 — 已接受建题操作的执行权与分块围栏

- **Status**: Accepted
- **Date**: 2026-10-18
- **Deciders**: 项目维护者明确选择保留已接受操作的执行权，解绑或显式撤销后停止
- **Supersedes**: None

## Context

首次关联的后台建题原本只保存 dataset/project ID。执行器重启后无法判断原发起者、
关联是否已被重新创建，或哪一块已经提交。若每块重新校验发起者的当前管理权，
账号停用或负责人转移又会改变既有后台操作的行为。用户选择保留已接受操作的执行权，
同时要求解绑和显式取消能阻止后续块写入。

## Decision

1. 在接受时通过 Identity 当前活跃账号与 Workspace 当前项目管理权合同授权，
   将 actor、dataset、project、association、job、mode 和 chunk size 固定在
   Labeling 持有的操作中。授权的项目行用 `SHARE NOWAIT` 锁，防止普通负责人或
   模式更新穿过接受事务；引用锁仍使用独立的 `KEY SHARE` 合同。
2. 可信执行器以 operation ID 恢复已接受操作，不在每块重新授权发起者。账号停用、
   负责人转移后，已接受操作继续；新接受操作必须重新通过当前管理权。
   每次 prepare/read/advance 都检查原 association UUID 和绑定 Jobs 状态。
   解绑后即使重建同一 dataset/project 关联，旧操作也不能继续写入。
   同步关联使用关联 UUID 作为 operation ID，前置标定提交后的失败可在重复关联时
   恢复；后台作业继续使用其独立操作 ID，终态不会由同步恢复路径复活。
3. Labeling 冻结待建来源计划和 total，按 next_chunk 推进。Task、点云链接、实际计数、
   操作游标、Jobs 进度与分块回执共同提交。重放相同 chunk 返回原回执；回执中的历史
   总数不能覆盖当前项目投影。源计划不交付给客户端，也不能由客户端提供任意文件路径。
4. 当前项目管理者可显式取消建题。取消与分块通过同一父行/作业锁串行，已提交的块
   保留，取消事务提交后拒绝后续块。共同锁序为 dataset → 当前授权或项目引用 →
   当前关联 → Jobs → operation → 来源引用 → 项目计数；函数不内部提交。
5. Jobs 持有版本化创建、查找、领取、进度、完成、失败和取消操作。重复关联可找回
   同一活跃作业并在提交后重新派发，以恢复提交成功但 broker 发布失败的窗口。
   Celery 建题任务延迟确认，worker 丢失时重新入队；暂时性连接、锁或序列化错误
   回滚后持续重试，不把未保存的成功工作记作帧/素材失败。
   配置依据 [Celery task acknowledgment](https://docs.celeryq.dev/en/stable/userguide/tasks.html#acks-late)
   与 [worker lost requeue](https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-reject-on-worker-lost)。
6. 沿用 [ADR-0092](0092-transactional-module-data-capabilities.md) 的标准 PostgreSQL
   协议、同连接外层事务、冻结 SQL/JSON 合同和独立函数 owner。Identity/Workspace/
   Assets/Jobs/Labeling 各自只写其数据；调用角色不获得私有表或内部 helper 的权限。
   普通点云标定仍保留原前置提交，nuScenes 导入继续关闭内部提交，由外层原子提交。

## Consequences

- 接受操作等于授予持久执行许可，停用发起者不会隐式撤销它。管理员需要停止时应
  取消作业或解绑关联；取消不删除已提交任务，恢复已有任务无需破坏性解绑。
- 已完成、失败或取消作业不会因重复领取被重新激活；未版本化的旧 payload 不能
  从当前负责人或当前关联推导身份。生产切换前必须排空或由当前管理者重新接受旧作业。
- 回执和来源计划会增长，当前没有自动保留期清理。调用方可读写自身语言的协议绑定，
  但仍依赖同一 PostgreSQL，不能直接搬到不同数据库。
- 这只是初始建题、授权和作业生命周期的数据边界；完整 assets/labeling/jobs 模块的
  运行工件、全部操作、对象权限和其余跨域事务仍须逐项完成。
