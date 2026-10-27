# 0093 — 目录观测按完整目录提交并以执行代次恢复

- Status: Accepted
- Date: 2026-10-22
- Deciders: Maintainers (authorized E04-A implementation)
- Supersedes: None

## Context

受控位置已支持动态登记与节点探测，原地数据集仍需要可恢复的扫描基座。活跃文件系统不提供跨重启稳定的分页游标或全树原子快照；一次枚举失败不能代表历史文件缺失。通用 AsyncJob 方法没有执行租约，终态任务还会按保留策略清理。

## Decision

DatasetSource 先以 inventory 合同绑定数据集、位置、固定节点和不可重定向规则。DatasetScanRun/Entry 独立持久化，每个目录完整列举并通过身份、mtime/ctime检查后，在同一事务写入观测、子目录队列和目录完成标记。条目路径以 SHA256 索引，原始相对路径为 TEXT；不保存 UI 的15分钟游标或文件系统FD偏移。

固定单目录5000原始条目/2秒循环预算，单运行50000观测/5000目录；过滤项同样记账。只读 stat，不读原图字节。恢复和收尾重算已捕获目录的摘要；变化、权限、替换、超限、取消和超时均不完整。即使枚举成功，也不判 missing，不将 stat 当内容验证，不创建 Item/Asset/Task。

来源行锁和部分唯一索引保证只有一个活动扫描。短事务统一 Location→Source→Job→Run 锁序，与任务清理一致；I/O 在锁外。task ID、来源代次、generation、随机令牌和60秒租约共同验证批提交。一个 Job 只 claim 一次；显式恢复创建新 Job/task ID，旧消息和迟到信号失效。Celery 使用指定节点专属队列并核对实际 hostname；信号提前分派到同一领域 owner。

Job FK SET NULL，数据集删除清空来源关联，账本独立保留。入口先限当前超管，包括通用任务读取和取消；每个批边界复核权限及配置。未知总量只显示计数和阶段。没有新周期调度器；失租或发布窗口中断由显式恢复接管，运行尝试受240/300秒软/硬限约束，内核阻塞限制按平台处理。

## Consequences

### 正向

- 重复投递、事务回滚与进程中断不会丢失已提交目录，也不会重复登记业务样本。
- 旧执行者和信号无法覆盖新尝试，短期 Job 清理不会抹掉观测历史。
- 可在内容读取和数据集向导之前独立验证扫描恢复能力。

### 负向

- 单目录超出预算时需缩小来源范围；不承诺无限目录规模。
- 目录持续写入可使完整性复核失败；完成也不是全树原子快照。
- 无周期失租回收，运维需显式恢复；内容核验、missing裁决和原地读取仍由后续独立交付。

## Alternatives Considered

- 内存递归后一次提交：无法保留恢复检查点，失败代价随全树规模增长。
- 持久 scandir offset 或 UI JWT：跨重启、目录变化和过期时不可靠。
- 只依赖 Celery acks_late：消息重复与进程丢失仍需数据库 fence。

## Notes

实现位于 `services/dataset_scans.py`、`services/filesystem_storage.py`、`workers/dataset_scan.py`；接口见 `docs-site/api/guides/dataset-sources.md`。

- [PostgreSQL row locks](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS)
- [Celery task acknowledgement and idempotency](https://docs.celeryq.dev/en/stable/userguide/tasks.html#acks-late)
