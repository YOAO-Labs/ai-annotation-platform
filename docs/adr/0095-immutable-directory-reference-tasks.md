# 0095 — 目录样本固定内容身份并在任务锁内复核写入

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** 用户授权推进；仓库实现评审
- **Supersedes:** —

## Context

目录观测与图片核验已能证明某次读取的字节，但尚未产生可用于标注的业务样本。外部文件可被覆盖，旧消费者又默认把 file_path 当 S3 key；直接保存相对路径会混淆内容、位置和业务身份。首个可用切片需要让小目录逐条接入，并保留已有上传与项目关联。

## Decision

采用逐条事务登记。DatasetReference 固定 Item→Source/Run/Entry，唯一 `(source_id, path_digest)`；DatasetAsset 固定 SHA-256 和大小，不创建 managed_original 副本。不同来源的相同字节不合并业务身份。同来源/路径重复登记返回已有 Item；内容改变明确冲突，不改写已有身份。

file_path 保存 `__aap_reference__/<item UUID>/<SHA-256>` 不透明标识。Item 路径/数据集、Reference 记录、引用 Task 的路径/Item/项目由数据库触发器禁止改写。通用 S3 层在调用前拒绝引用标识，SQL 前缀判断转义通配符。未适配的媒体派生、跨任务复制、原生掩码、预测、训练和后台导出关闭引用入口；历史托管数据继续走原路径。

登记复用扫描核验，并在 Location→Source→Run/Entry 锁后重读当前字节、复查最初 API Key hash 与账号；Dataset 使用 NOWAIT，繁忙返回可重试409，避免与数据集删除的反向 FK 锁等待形成环。登记与 Link/补建 Task 共用 Dataset 锁；Link 在锁后重新检查已有关系，每批建档再查 Task，避免漏建或重复。

Task 内容读取按当前分派和项目权限授权，不开放目录枚举。登记要求当前 API read 探测；业务读取使用该节点已固定的锚点与本次真实完整读取作为当前可读证明，TTL 过期不自行重绑根。来源改名、停用再启用可重试旧内容；定位改变或摘要不符拒绝。跨 API 节点以 SHA-256 核对内容，本地 dev/inode 只用于对应节点的路径身份。

普通手工标注与单任务生命周期写入在取得现有 account→membership→task-edit advisory→Task 锁后捕获完整核验快照，保持 Task 锁到原 handler 提交。快照 helper 不提交调用者事务。单任务送审复用同一请求的锁内证明，不重新建立 API Key 身份基准；批次送审尚未接入引用。此切片接受有界文件 I/O 占用行锁的代价，以避免先核验、后等待锁期间源文件被覆盖而仍保存。

Workbench 使用鉴权 Blob，并以账号、登录会话、token、任务内容和请求代次决定所有权。切换、重试和卸载时立即退休旧 URL，取消请求并回收；图片解码完成前禁用编辑与提交。管理多步动作每次 await 后复核单调会话代次，账号或凭据变化使旧链路退休并重置界面。前端不补写“最新摘要”给旧离线操作；不可变 Task 身份和服务端重新读取共同决定是否允许落库。

## Consequences

- 管理员可扫描、逐条登记、预览、关联普通图片项目并开始手工标注；外部原图持久复制量为0，删除业务对象不删除源文件。
- 每次读取及写入均产生有界临时快照并计算摘要，带来额外 I/O；慢挂载会延长 Task 锁占用。并发和耗时仍受 ADR0094 预算约束，阻塞内核 I/O 需由部署恢复。
- 原图被外部覆盖无法靠摘要恢复；需要保留历史字节时必须另行冻结。大目录批量登记、同步、缓存及训练适配保留为后续切片。
- 应用回退保留0198及已登记身份；仅空引用表允许 schema downgrade。禁止用旧 S3 resolver 处理新引用。

## Alternatives Considered

- 全目录后台批量登记：需要独立任务所有权、取消恢复和预算，留给后续切片；逐条已提交结果天然可继续。
- 前端保存摘要证明：不能替代当前权限与实际字节复核，且会增加离线队列上下文迁移；使用不可变 Task→内容绑定和服务端捕获。
- file_path 直接保存目录路径：旧 S3、媒体和训练消费者可能误读，故采用明确拒绝的不透明标识。

## Notes

实现：`services/dataset_references.py`、`reference_access.py`、`reference_keys.py`，Alembic0198，`useVerifiedImage.ts`、`useSessionGuard.ts` 和目录管理面板。接口与预算见[严格读取](../../docs-site/api/guides/verified-content.md)，操作见[目录接入](../../docs-site/user-guide/datasets/import-images.md#服务器目录原地引用)。
