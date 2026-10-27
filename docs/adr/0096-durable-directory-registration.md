# 0096 — 目录批量登记使用独立的两阶段持久账本

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** 用户授权推进完整 E03；实现与并发复核
- **Supersedes:** —

## Context

逐条登记已能建立不可变的 Item/Asset/Reference，并通过任务链路读取和标注。全目录接入还需要内容预检、明确确认、增量结果、取消与恢复。扫描清单只证明枚举范围，AsyncJob 又会定期清理，二者都不能单独承担长期登记检查点。

## Decision

新增 DatasetRegistrationRun/Entry：每个完整 ScanRun 对应一个登记账本，阶段为 preflight/registering；候选通过 INSERT SELECT 建立，逐项事务保存结果、异常和累计数。预检只保存严格内容回执；显式确认后重新读取固定摘要，将业务对象、关联任务和该项检查点同事务提交。既有引用保持不可变，同来源路径幂等，原图复制量为0。

每次尝试固定原始账号、API Key hash、配置修订及节点。分片拥有独立 task ID、generation、token 和60秒租约，单片最多25项或90秒。Run 同时作为派发 outbox：先提交 pending 再发布，beat 每30秒恢复待派发或过期分片。取消保留已提交部分；旧消息、旧租约及迟到失败信号不能写入新的尝试。终态 AsyncJob 的清理不删除账本。

确认捕获预检尝试身份，读取期间出现新预检时返回冲突。锁内回调只检查请求权限，不在已锁住扫描任务的事务内再锁旧登记任务，避免与任务历史清理形成锁环。登记复用原有 Location→Source 锁序及 Dataset NOWAIT；逐项快照回调在提交核验回执前检查执行 fence，最终业务提交再检查一次。

显式节点使用固定锚点和实际完整安全读取证明可达，不因探测 TTL 到期中止已领取任务。根离线中断阶段；文件自身错误隔离并允许单项重试。没有核验回执的异常项可显式读取修复后内容；已核验内容不能通过重试改变摘要。数据集繁忙或快照容量不足退避，不能消费正常候选为损坏项。

## Consequences

- 可关闭页面后继续查看、取消和恢复，单文件错误不阻断正常图片；预检、待核验、已登记、异常及排除计数分别展示。
- 每项正式登记仍完整读取，增加 I/O；有界临时快照和分片降低资源占用，但不保证阻塞内核调用可被强制终止。
- 部署必须刷新登记 worker 和 beat；应用回退停止新任务并保留0199账本，有历史记录时禁止破坏性降级。
- 当前批量入口采用 reference/none。授权扩展、缓存策略和外部适配器继续按完整 E03 验收，不由本决定替代。

## Alternatives Considered

- 复用 ScanEntry.state：混淆元数据枚举、内容核验与业务登记，且破坏扫描恢复边界。
- 只保存 AsyncJob.result：任务历史清理后无法恢复，计数也不能证明哪项业务事务已提交。
- 全目录单事务：取消代价和锁占用随文件数增长，无法增量使用已提交样本。

## Notes

实现入口为 `services/dataset_registration.py`、`workers/dataset_registration.py` 与迁移0199。接口见[目录来源与批量登记](../../docs-site/api/guides/dataset-sources.md)，管理员流程见[服务器目录原地引用](../../docs-site/user-guide/datasets/import-images.md#服务器目录原地引用)。继承[ADR0095](0095-immutable-directory-reference-tasks.md)的不可变内容与任务写入边界。
