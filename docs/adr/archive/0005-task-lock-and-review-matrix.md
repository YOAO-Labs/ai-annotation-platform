# 0005 — 任务锁（5min TTL）与审核流转角色矩阵

- **Status:** Accepted
- **Date:** 2026-05-06（回填；锁机制 v0.6.5 落地，审核矩阵 v0.7.0 收口）
- **Deciders:** core team
- **Supersedes:** —

## Context

任务级并发控制有两个相邻问题需要一并解决：

### A. 锁

多个标注员同时打开同一 task → 各自标注 → 提交时彼此覆盖，已发生过事故（v0.6.x 早期）。需要：

- 标注员开始标注时获得**互斥锁**；锁活跃时其他人能看到「他人正在标注」状态。
- 离开页面 / 关闭浏览器后**自动释放**——不能要求用户「记得」点退出。
- reviewer 不是 task assignee，但审核期间也要锁住，防止标注员同时改。
- 单点抓死要可被运维人工解锁。

### B. 审核流转

任务状态机原本只有 `pending / in_progress / completed`，无法描述「提交后待审核 / 通过 / 退回 / 重开」。需要 5 个状态 + 角色矩阵。

候选锁机制：

| 选项 | 主要卖点 | 主要劣势 |
|---|---|---|
| **PG 表 + TTL + 心跳** | 持久化、跨副本可见、可审计 | 需后台清理、心跳风暴风险 |
| Redis SETNX + EXPIRE | 原生 TTL、性能好 | Redis 重启锁丢失、跨副本调试难 |
| 数据库行级锁（SELECT FOR UPDATE） | PG 原生 | 锁仅在事务内生效，标注员开几分钟标注页等于长事务，PG 连接耗尽 |
| 内存锁（per-process） | 简单 | 多副本下完全失效 |

候选 TTL：1min / 5min / 15min / 30min。

## Decision

### A. 锁机制

采用 **PG 表 `task_locks` + 5 分钟 TTL + 60s 心跳 + 3 种自动释放**。

实现：`apps/api/app/services/task_lock.py`、`apps/api/app/db/models/task_lock.py`。

```sql
task_locks (
    id uuid PK,
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    expire_at timestamptz NOT NULL,
    UNIQUE (task_id, user_id)
)
```

关键参数：

| 参数 | 值 | 选择理由 |
|---|---|---|
| `DEFAULT_TTL` | **300s（5min）** | 标注一题平均 20-90s；< 1min TTL 心跳频率扛不住网络抖动；> 10min 抓死时占锁太久 |
| 心跳间隔 | 60s | 给两次心跳容错窗 (`expire_at - now ∈ [240, 300]`) |
| 接管阈值 | TTL/2 = 150s | `last_heartbeat > TTL/2 前` 视为悬挂残留可被接管（`task_lock.py:50-57`） |
| 项目级覆盖 | `Project.task_lock_ttl_seconds` | 长任务项目可调高（`schemas/project.py:66`） |

释放路径：
1. **主动**：用户离开页面 / 切到下一题 → `release(task_id, user_id)`（DELETE 行）。
2. **过期**：心跳停止 → `expire_at` 被 `acquire` / `is_locked` 第一次访问时清理（`_cleanup_expired`）。
3. **悬挂接管**：他人锁但全部 `expire_at < now + TTL/2` → 视为残留，自动接管（应对 keepalive DELETE / acquire 乱序到达留下的影子锁，v0.6.7 B-13）。

### B. 审核流转角色矩阵

任务状态机：

```
                    ┌─────────────┐
                    │   pending   │ ← 新建 / reset
                    └──────┬──────┘
                       claim│ (annotator)
                    ┌──────▼──────┐
                    │ in_progress │
                    └──────┬──────┘
                     submit│ (annotator)
                    ┌──────▼──────┐
        ┌───────────│   review    │
        │           └──────┬──────┘
   reject│                 │approve (reviewer / project_admin)
        │                  │
        ▼                  ▼
  ┌──────────┐      ┌──────────┐
  │ rejected │─────▶│completed │
  └──────────┘reopen└──────────┘
   (annotator)    (super_admin override)
```

`TaskStatus` 枚举（`apps/api/app/db/enums.py:19`）：`uploading | pending | in_progress | review | completed`。状态机迁移由具体路由按角色显式驱动，每次迁移写 `audit_logs.action = task.{submit,withdraw,review_claim,approve,reject,reopen}`。

单题送审会修改任务、审核轮次和批次计数，不能以共享行锁代替写锁。同批次原件读取只持共享锁，因此送审先以批次级事务预约排除第二个升级者，再用 `FOR SHARE NOWAIT` 区分已存在的编辑者，最后在同一事务内短暂等待升级为 `FOR UPDATE`。等待不超过一秒，也不延长调用方设置的更短期限。获得写锁后仍重验任务所属批次、权限及轮次；拒绝竞争的提交不能留下计数、审计或质检任务。这些事务锁不替代工作台的编辑租约。

角色权限矩阵（节选自 `apps/api/app/api/v1/tasks.py`）：

| 操作 | super_admin | project_admin | reviewer | annotator | viewer |
|---|:-:|:-:|:-:|:-:|:-:|
| claim（pending → in_progress）| ✅ | ✅ | ✅ | ✅（仅自己被分配的 batch）| ❌ |
| submit（in_progress → review）| ✅ | ✅ | ✅ | ✅（自己持锁）| ❌ |
| withdraw（in_progress → pending）| ✅ | ✅ | ✅ | ✅（自己持锁）| ❌ |
| review_claim（review → 锁给 reviewer）| ✅ | ✅ | ✅ | ❌ | ❌ |
| approve（review → completed）| ✅ | ✅ | ✅ | ❌ | ❌ |
| reject（review → pending，附 reason）| ✅ | ✅ | ✅ | ❌ | ❌ |
| reopen（completed → pending）| ✅ | 仅 owner | ❌ | ❌ | ❌ |

`_REVIEWERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN, UserRole.REVIEWER)`（`apps/api/app/api/v1/tasks.py:49`）。

## Consequences

正向：

- 5min TTL + 60s 心跳是「平衡点」：锁丢失风险足够低（要连续两次心跳失败才释放），抓死回收时间也足够短（运维不需要拍键盘解锁）。
- 锁记录持久化让 audit / 调试可见——SettingsPage 「会话」页可显示当前持锁者（v0.7.x 有页面查询 `task_locks`）。
- 接管阈值（TTL/2）解决了 v0.6.6/0.6.7 多次出现的「DELETE-then-INSERT 乱序」造成的死锁横幅误显示。
- 状态机 5 状态 + 显式迁移路由让 audit 完整：每次状态变化都有 actor / reason / 前后值。
- reviewer 的锁与 annotator 的锁在同一表存（区分 user_id 即可），不需要双锁机制。

负向：

- 长 batch 情况下心跳风暴：100 个标注员同时在线 → 100 次 / min 的 UPDATE。当前规模下可承受；触发条件：> 1000 同时在线时考虑切到 Redis SETNX。
- 锁状态依赖 `task_locks` 表健康：误 DELETE 这张表 = 全员重新 acquire；alembic 操作此表必须谨慎。已加 alembic 检查脚本（v0.7.x）。
- 状态机迁移路径硬编码在路由里，不是配置驱动。这是有意——审核流程的角色权限属于「业务规则」而非「配置项」，配置化反而增加误用面。

## Alternatives Considered（详）

**Redis SETNX**：v0.5 试过。优势：原生 TTL 不需后台清理。劣势：

- Redis 重启 / 主从切换时锁丢失，全员能重新 claim 同一 task → 写冲突回归。
- 锁状态不持久，运维查锁要专门写 redis-cli 命令。
- 跨 PG / Redis 双数据源调试难，复现 bug 时需对齐两边时钟。

**SELECT FOR UPDATE 长事务**：当用户保持标注页 5 分钟，PG 连接持续占用——FastAPI 使用 asyncpg pool 通常 ≤ 50 连接，10 个标注员就可能耗尽。彻底放弃。

**1min / 15min / 30min TTL**：

- 1min：心跳每 30s 一次，移动端用户切后台 30s 直接被踢，体验崩。
- 15min / 30min：抓死回收太慢；多见用户开个页面就去吃饭、回来发现标注员锁死了任务大半小时。
- 5min 是 v0.6.5 ~ v0.7.x 持续观察后保留下来的折中。

## Notes

- 「`annotating → active` 暂停」未实现的根因正是这个状态机：scheduler.check_auto_transitions 看到 `in_progress` task 会立刻把 batch 推回 annotating，需要先释放 in_progress 锁 + 引入 batch admin-locked 标志阻断 scheduler。详见 ROADMAP §A 二阶段批次状态机。
- 后续可能演进：把 task_lock TTL 升到 10min 但缩短接管阈值，让长任务用户更舒适、悬挂回收更快。需要 A/B 实验数据支撑后再调。
- 如果 ROADMAP 「2FA / 设备绑定」实现后，是否需要绑定锁到 device 而非 user 是开放问题；当前不需要。
