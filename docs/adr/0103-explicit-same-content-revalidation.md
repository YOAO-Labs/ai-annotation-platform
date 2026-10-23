# 0103 — 同内容重新核验追加读取凭据并保留业务身份

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** 用户授权完整推进 E04
- **Supersedes:** —

## Context

同字节重写可能改变 inode 或时间，旧任务的严格引用会拒绝读取。自动按 SHA 更新原 Reference 会抹去历史；建立新样本则无必要地割裂已有标注。

## Decision

只允许完整预检中的 observation_changed 显式重新核验，固定 generation、前驱和候选 SHA。复用 SourceRevision 追加同 Item/Asset/SHA 的接纳记录，保留原所有权、Reference、业务对象、metadata、任务和标注。无新 schema。

复用严格快照、Location→Source→Dataset 顺序与授权、原 Key proof、策略、前驱屏障。重复及并发确认返回原记录，不倒退路径头。资格变化继续阻断。

原图读取先验证原始不可变绑定，再选择同来源/路径、同业务 Item/Asset 的最新接纳观测，核对原资格与 SHA。选中修订 ID 纳入 I/O 前后上下文比较；失败不回退旧凭据，不搜索同摘要的其他样本。新内容的不同 Item 不改变旧 Item 身份。

## Consequences

严格引用可以在同内容被明确重新核验后继续原任务，预检差异和周期门禁仍保留。托管复制仅推进外部来源观测，不新增对象。整体应用回退可能失去新凭据的可读性，需保留新版严格读取；不能删除账本来恢复旧行为。该方案不备份外部原件，不裁决移动、资格更换或删除。
