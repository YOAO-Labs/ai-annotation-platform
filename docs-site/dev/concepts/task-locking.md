---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-10-20
---

# Task Lock

Task lock 是工作台里的并发编辑保护层，不是权限系统，也不是 task 状态机。

代码真值源：

- `apps/api/app/services/task_lock.py`
- `apps/api/app/api/v1/tasks/locks.py`
- [ADR-0005](/dev/adr/archive/0005-task-lock-and-review-matrix)

## 它解决什么问题

如果两个标注员同时打开同一题并都开始编辑，就会出现：

- 标注互相覆盖
- 前端状态错乱
- submit / review 时审计与归属关系混乱

task lock 的目标是让“同一时刻谁在编辑这题”有一个明确答案。

## 生命周期

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/task-dispatch-and-lock.svg"
  alt="在线派题先复用旧锁或过滤、排序最多二十个候选，再逐个尝试占锁；TaskLockService 在单题 advisory lock 下清理过期行、判断接管并用 upsert 创建或续期锁，同时标出旧锁复用和自定义 TTL 尚未端到端的实现边界"
  caption="Scheduler 候选选择、Task Lock 占锁决策与受保护写入边界"
/>

图中的两层不要混用：scheduler 只决定候选顺序，真正的互斥结果由 `TaskLockService.acquire()` 决定。当前 `/tasks/next` 复用旧 lock 行的分支没有先过滤 `expire_at`，也不会 cleanup、续期或重新 acquire；前端进入题目后仍会通过显式 lock 端点取得编辑锁。这是当前实现边界，不应把“接口返回了题目”解释成“后端已确认持有有效锁”。

## 核心语义

### acquire

`TaskLockService.acquire()` 做的不是“简单 insert 一行”，它还处理：

- 同一 task 的多用户残留锁
- 他人锁是否已 stale
- assignee 的 force takeover
- 并发插入的 upsert 兜底
- PostgreSQL deadlock / serialization failure 的最多 3 次整事务重试

数据库唯一键是 `(task_id, user_id)`，不是单独的 `task_id`；所以服务必须在单题 advisory lock 内读取全部行、清理他人残影，再用 `INSERT ... ON CONFLICT` 创建或续期当前用户的行。

### heartbeat

活跃会话会定期 heartbeat，把 `expire_at` 往后推。

### release

用户离开题目、提交送审、切题等场景，会主动释放锁。

### cleanup expired

`TaskLockService` 的 acquire、读锁与写入守卫会按当前 task 顺手清理过期行，不依赖单独后台任务。`/tasks/next` 开头直接复用旧行的查询是例外，当前没有先走这一步。

### 写入边界

任务、独立标注、视频章节、追踪任务、标注反馈和个人 API Key 路由先通过 `require_active_task_actor` 重新读取账号启用状态，并取得用户行的共享锁，再进入 handler 的资源锁。scheduler 的旧锁复用、任务领取服务也执行同一账号检查。这样，请求通过认证后即使遇到并发停用，也只能先完成原事务或观察到停用状态后停止。

管理员停用／删除／恢复、自助注销 request/cancel 共用新鲜用户行锁；任何会减少启用超级管理员的写入都先取全局最终管理员 advisory fence，再按 UUID 顺序锁 User。自助 due worker 用 operation-receipted pages 和 `SKIP LOCKED` 跳过当前在途账号，持续排空直到无进展；页大小不是总体 cap，并在每个超级管理员停用前再验证仍有其他实际启用者。Accepted self request 跨管理员 suspension/reactivation 继续有效。

凭据副作用按所有者语义区分：管理员/offboarding 流程保持原有 generation、key 与职责交接规则；自助注销到期仅做 inactive/offline 软删除，不推进 JWT generation、不撤 API key、不迁移任务/项目。数据库提交失败时业务状态与自助请求／受限 Audit／Notification outbox 一起回滚；旧自助注销不会因人工 suspend/reactivate 自动取消。

人员交接锁定操作者、目标账号及接收人，再核对项目、批次、任务与锁。由于既有批次管理和任务回写使用不同的资源锁顺序，交接使用 `NOWAIT` 行锁与非阻塞 task advisory lock；遇到在途写入时返回 `409` 要求刷新预览，避免等待构成锁循环。

普通标注创建复用 `annotation_operations` 的持久记录。带 `Idempotency-Key` 的请求在 Task 行锁下复核权限和请求摘要，再创建标注或返回首次响应；唯一键为 `(task_id, actor_id, idempotency_key)`。浏览器队列在支持 Web Locks 的环境中跨标签页串行执行请求与本机确认，服务端记录同时覆盖响应丢失或本机确认失败后的重试。已经存在创建记录时，迁移降级会拒绝移除对应类型约束，避免丢弃去重依据。

需要把“检查当前锁持有人”和后续 annotation 写入视为一个原子决定的路径，会调用 `assert_write_allowed()`。服务使用按 task 派生的 PostgreSQL transaction advisory lock，把 acquire、release、heartbeat 与这类写入串行化；随后再锁定当前 task lock 行复核持有人。原生 AI Mask 接受和多对象 Mask 原子 mutation 都使用这条边界，避免检查通过后另一会话恰好拿锁并同时提交。

Mask 原子 mutation 还需要同时锁定多个 annotation、可选的视频 segment 和内容引用。固定顺序是：`Task` row → task edit advisory / task-lock rows → video segment → 按 object key 排序的 RLE advisory → `RasterMaskUpload` row → 按 UUID 排序的范围 annotation。标注转换 execute 也遵循这一顺序：先用未加行锁的冻结快照重算 manifest，取得 RLE / upload 锁后再按 UUID 锁 annotation 并复查快照，对象内容只在复查通过后写入。普通 Mask create / PATCH、视频 Mask 单帧保存，以及关键帧删除、outside 标记与恢复 held 也先锁 Task，再锁对应 segment 与 annotation；内容上传从 reservation、对象写入到事务提交全程持有同一 RLE advisory。DELETE 和纯字段更新先锁 Task 再修改 annotation。GC 按 RLE advisory → upload row 工作：先提交可恢复的数据库删除，再开启第二事务重新取得同一 RLE 锁，复查引用和新 reservation 后才在持锁期间删除对象。因此失败最多留下可再清理的存储孤儿，也不会误删同键并发上传或留下指向缺失对象的数据库行。新的多对象写路径不得调换这个顺序。

## TTL 与 takeover

当前默认 `DEFAULT_TTL = 300s`，前端每 60s heartbeat。需要区分三条路径：

- scheduler 首次 acquire 会传入 `project.task_lock_ttl_seconds`。
- 显式 `POST /tasks/{task_id}/lock` 和 heartbeat 端点没有传项目 TTL，当前都会回到 300s 默认值。
- 自动接管阈值固定使用 `DEFAULT_TTL / 2 = 150s`，不随项目配置变化。

因此项目 TTL 目前只影响 scheduler 首次占锁，并不是端到端租约配置。修改该字段语义时必须同时检查显式 acquire、heartbeat 与 takeover，不能只改 scheduler。

300s 默认租约配合 60s heartbeat 的目的：

- 避免用户刚断网几十秒就被踢锁
- 又不至于一把死锁占一整天

## 与 scheduler 的关系

task lock 和 scheduler 是串联关系：

1. `scheduler.get_next_task()` 先按 `expire_at` 取当前项目下最近的本人 lock 行；当前没有过滤是否过期。
2. 没有可复用行时才构造候选，并在同 scene 优先或经典采样后逐个尝试 acquire。
3. 最多检查 20 个候选，竞态中被别人先拿走的题会跳过；首个占锁成功者才会返回。

所以经典候选路径会把“下一题”和“拿到锁”串在同一个服务调用里，但候选查询与 acquire 之间仍有 TOCTOU 窗口；正确性来自“占锁失败就不返回该候选”，而不是假设查询结果不会变化。

## 与 task 状态机的关系

lock 不等于状态：

- `review` / `completed` 这些状态可能因为业务规则不可编辑
- `in_progress` 只是工作流状态，不保证一定有锁
- 一题是否可编辑，通常要同时看：
  - task 状态
  - 当前用户角色
  - lock 是否属于自己

## 典型场景

### 场景 1：用户刷新页面

- 如果锁还有效，scheduler 会把原题返还给他
- 不会悄悄换成别的题

### 场景 2：旧会话残留锁

- assignee 重进时可 `force_takeover`
- stale 锁也可能被自动接管

### 场景 3：审核员进入 review

- reviewer 虽不是 annotator，也会参与同一套 lock 机制
- lock 表里只是换一个 `user_id`

## 常见误解

### 误解 1：有锁就一定不可见

不是。lock 防并发编辑，不负责页面可见性。

### 误解 2：锁是纯前端行为

不是。真值在数据库和后端服务层，前端只是消费 acquire / heartbeat / release 端点。

### 误解 3：一题只能有一行 lock

理想上是，但历史并发残留会让同 task 出现多行，服务层显式做了去重兜底。

## 常见修改落点

| 你想改什么          | 先看哪里                                         |
| ------------------- | ------------------------------------------------ |
| 调整 TTL            | `services/task_lock.py` + `db/models/project.py` |
| 调整 takeover 逻辑  | `services/task_lock.py`                          |
| 改前端续期节奏      | `apps/web/src/hooks/useTaskLock.ts`              |
| 改提交/切题时释放锁 | `api/v1/tasks/lifecycle.py`                      |

## 相关文档

- [Scheduler 与派题](./scheduler-and-task-dispatch)
- [任务模块](./task-module)
- [状态机总览](./state-machines)
