# 0092 — 存储位置在受控部署根内动态注册并按节点验证

- Status: Accepted
- Date: 2026-10-22
- Deciders: Maintainers (authorized E02-A implementation)
- Supersedes: None

## Context

已有 S3/SFTP 连接用于复制导入，平台原图身份和物理位置已分离。CephFS/NFS 等目录需要动态登记，逐数据集修改 Compose 会造成不必要重建。数据库登记不能令未挂载宿主机路径对容器可见，API 与 worker 的路径可达性也可能不同。

## Decision

部署只配置少量 `STORAGE_FILESYSTEM_ROOTS` 句柄映射，默认拒绝所有根；超管以根句柄和相对路径登记只读 StorageLocation。根内新增目录和登记无需重启，修改实际挂载仍由运维重建受影响容器，不要求镜像重建。位置授权与元数据更新使用 expected_revision；S3/SFTP 关联位置继续由旧连接器持有权限和加密凭据。

Linux openat2 使用 BENEATH / NO_SYMLINKS / NO_XDEV，保持打开的目录 FD 边界，不使用 realpath 字符串检查或普通 open 回退。根及位置基目录按节点保存首次身份，替换拒绝静默重绑。目录浏览仅单层、有限条目与循环预算，游标绑定位置、修订、节点、路径摘要与目录快照；不支持编码明确失败。

节点探测通过 Celery worker_direct 投递并校验执行 hostname，重新读取操作者及修订；API落库前再次校验。list/read/range 独立验证，文件采样只读，不写探针；证据5分钟过期，按观察时间阻止晚到覆盖。等待窗口不等于能取消内核阻塞调用。首次节点探测信任部署根映射，不比较跨节点 dev/inode，也不将能力探测当作内容一致性核验。

九用途绑定只读展示，共桶仍保留用途；本切片不切换写入目标，不接入 DatasetSource、原地扫描、原图 HTTP resolver 或 UI 向导。

## Consequences

### 正向

- 根内目录动态接入，与容器挂载和对象存储凭据分离。
- 有明确的使用权限、节点能力与修订失效边界。
- 原有复制导入与固定业务桶保持兼容。

### 负向

- 依赖 Linux5.6+ 及受支持架构，跨子挂载需分别声明根。
- 节点路径替换需人工核查并重新登记，不能自动重绑。
- 线程不能硬中断阻塞文件系统调用，未来扫描需独立任务预算与恢复机制。
- 内容一致性、原地读取和数据集向导仍须后续独立交付。

## Alternatives Considered

- 任意宿主机绝对路径：容器无法凭数据库获得路径访问，且无法控制边界。
- 每个数据集追加 volume/env：增加运维变更，不能支持根内动态注册。
- realpath 前缀检查：检查到打开之间可能替换，拒绝采用。

## Notes

实现：`apps/api/app/services/filesystem_storage.py`、`apps/api/app/services/storage_location.py`、`apps/api/app/workers/storage_probe.py`。部署与接口契约见 `docs-site/ops/deploy/docker-compose.md`、`docs-site/api/guides/storage-locations.md`。

- [Linux openat2](https://man7.org/linux/man-pages/man2/openat2.2.html)
- [Celery worker_direct](https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-direct)
