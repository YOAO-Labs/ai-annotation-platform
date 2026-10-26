# 只读文件系统原语

`aap-filesystem-read` 是仅依赖 Python 标准库的独立包，供中心和站点 Agent 共享安全读取语义。它不导入 API Settings、数据库、Redis、Celery、训练执行器或图像解码依赖。本包提供中心与站点 Agent 共用的本地只读文件系统原语；站点配对、网络传输与平台 Range 由 `aap-site-agent-contract`、`apps/storage-agent` 和中心 API 实现。

来源根通过不可变 `RootSpec(root_handle, absolute_root, relative_base="", binding_revision=1)` 显式传入，不从环境变量读取映射。根路径不能是 `/`，必须是规范化绝对路径；根内路径只能是 UTF-8 相对路径。调用方负责授权、绑定修订核验和已确认进度，本包不决定业务权限。

安全打开只支持 Linux x86_64/aarch64 的 `openat2`；不支持时返回 `safe_open_unsupported`，不会回退到普通路径打开。根内拒绝符号链接、路径越界及跨子挂载访问。受控根的配置路径允许到达挂载点。原有 anchor、目录 manifest 和文件 observation 字段保持不变，设备/inode 和 boot identity 仅用于本机观察，内容身份使用完整 SHA-256。

`capture_anchor`、`scan_directory`、`browse_directory`、`probe_directory` 和 `content_file` 均接收 `RootSpec`。单目录扫描最多 5000 项、2 秒；目录结果是完整快照，调用方必须整批原子确认。`content_file` 在读取前后复核文件以及来源目录身份，拒绝已替换或改变的来源。探测中的 `range=verified` 只证明本机 `pread` 能力，不代表平台已开放 HTTP Range。

`stage_content(factory, ..., directory=state_directory)` 对 `factory()` 返回的 `(body, observation)` 完整读取、计算 SHA-256，并在来源 context 退出复核成功后返回 `VerifiedStage`。调用方必须传入现有私有状态目录，不会默认写入系统临时目录；来源根必须以只读方式提供，状态目录应在独立可写卷。stage 是权限为 0600、立即解除目录链接的临时文件，关闭或进程退出后释放，不承担耐久恢复职责。`VerifiedStage.file` 从偏移 0 开始，所有权由调用方持有，使用完毕调用 `close()` 或通过 context manager 关闭。

单次 stage 最多 64 MiB、10 秒，调用方只能降低这两个上限，并负责全局并发、磁盘容量、带宽及租约预算。声明长度、实际长度、期望长度和 SHA 必须一致；空文件也检查 EOF。取消和超时是协作式的，阻塞的文件系统调用返回后才完成清理；调用方不能在物理读取仍存续时释放准入名额。关闭失败在异常上保留 `storage_operation_unsettled` 标记；调用方应保留未决责任，不能将网络断开或超时视作已关闭。

本地 stage 成功不代表中心已确认。恢复时重新验证原内容，不能将不同 SHA 的字节拼入旧读取；Range 应从同一已验证 stage 使用独立偏移读取。临时文件不会被本包登记为原件或持久缓存。

测试仅使用临时目录和标准库文件操作，不需要 API 环境或服务：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=apps/_shared/filesystem_read/src \
  python -m pytest -p no:cacheprovider apps/_shared/filesystem_read/tests
```
