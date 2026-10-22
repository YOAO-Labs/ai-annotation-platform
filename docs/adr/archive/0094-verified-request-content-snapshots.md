# 0094 — 可变原图通过有界请求快照核验后交付

- Status: Accepted
- Date: 2026-10-22
- Deciders: Maintainers (authorized E05-A implementation)
- Supersedes: None

## Context

目录观测只有 stat 信息。即使安全打开同一 FD，读取期间仍可能被原地改写。托管对象的上传签名也可能在有效期内重复 PUT；UUID key 不构成不可变版本。现有 Asset 已保存 verified SHA-256，但消费者仍有全局桶/预签名假设。

## Decision

共用请求内 TemporaryFile 快照 owner。filesystem 复用 openat2 根边界和本 API 节点固定 anchor；S3 只接受已核验 Asset 的固定托管副本。完整读取、核对摘要和长度并解码单帧 PNG/JPEG/WebP 后，才从同一快照发送。不能重新打开源路径或重签可覆盖 key。原件与派生物分别授权和使用。

扫描条目独立保存 verified_content，0197 迁移有内容时拒绝降级抹除。首次核验建立当前捕获字节的身份，不证明扫描时的内容；设备号/inode 不跨主机比较；首次核验要求与实际扫描同内核主机，并检查固定来源目录及文件身份，未知/跨主机须重新扫描。已核验 SHA 可跨节点使用。回执禁止数据库原位改写。原地业务样本及外部任务写入仍由 E03 激活，不能为核验伪造 Item/Asset。

超管核验来源配置，任务读取复用实际项目/任务权限。I/O 不跨数据库锁，响应前重读权限、API Key 状态/凭据身份/scope、修订和绑定。旧 file-url 增加任务可见性并保持响应合同；新严格 API 以期望摘要读取，不自动切换旧消费者。工作台 Bearer/Blob 生命周期和标注内容凭据需与 E03 一起验收。

固定64MiB、32M像素、10秒读取循环、15秒等待、30秒响应和每进程4名额，含响应期的临时字节上限256MiB。取消/超时协作停止，线程真正退出前不归还名额；内核阻塞不能硬中断。响应 __call__ finally 关闭快照，不只依赖 generator/background 清理。Range、持久缓存和预览派生后置。

## Consequences

- 同一快照的字节与已核验摘要一致；来源后续覆盖不会把旧标注配到新字节。
- 本阶段可独立用于管理员图片核验及现有托管任务的严格 API 读取；不创建业务样本。
- 首次读取增加完整 I/O、临时磁盘和解码成本，受明确规模与并发预算约束。
- API 必须实际可达目录并有自身 read 证据；仅 worker 可达不能宣称 API 可读。远程读取代理单独实现。
- 历史签名消费者仍有旧保证窗口，不能将新 API 的保证扩展到尚未迁移的 UI/训练/ML/导出。

## Alternatives Considered

- stat 后直接发送源文件：核验与实际响应之间仍可变化。
- FileResponse 指向源路径：响应重新打开可变原件。
- Redis/Celery 结果传原图：放大消息与结果存储，新增过期和大对象治理。
- 持久原图缓存：额外引入保留、配额与淘汰合同，不作为请求内验证的前提。

## Notes

- [Python TemporaryFile](https://docs.python.org/3/library/tempfile.html#tempfile.TemporaryFile)
- [Starlette responses](https://www.starlette.dev/responses/)
- 实现：`services/content_snapshot.py`、`services/dataset_content.py`；接口：`docs-site/api/guides/verified-content.md`。
