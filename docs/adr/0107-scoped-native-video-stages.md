# 0107 — 原生视频通过路径 Cookie 读取有界完整核验暂存

- **Status:** Accepted
- **Date:** 2026-10-24
- **Deciders:** 持续 Epic 实施中的原件读取与浏览器交付设计

## Context

托管原件已有固定保存位置、完整SHA和当前权限复核。完整下载响应独占临时文件并在结束时关闭，不能直接承担原生video的反复seek；video也不能为每次请求自行添加Bearer。转码、poster和精确帧chunk的字节不同，不能借原件身份接管其历史对象。

| 方案                    | 优点                                  | 代价                                   |
| ----------------------- | ------------------------------------- | -------------------------------------- |
| 每次Range完整捕获远端   | 无跨请求暂存                          | seek反复复制完整大文件                 |
| URL query能力凭据       | 原生媒体可直接使用                    | 凭据进入现有代理及Uvicorn请求行日志    |
| 路径Cookie与进程内stage | 无秘密URL，同一内容可复用完整核验结果 | 须显式管理短租约、真实关闭及单实例边界 |

## Decision

Bearer POST以当前原Authority、Task/Item关系和完整OriginalSource冻结准备请求，沿保存位置严格捕获全量字节。只接托管video，未知历史、引用媒体和其它格式按已有合同拒绝。同Authority/session/资源/完整来源的ready stage可复用；同键在途准备明确429。客户端session只划分资源命名空间，不授予服务端权限。

prepare生成独立lease UUID、平台相对media_path与期限，随机256位能力只通过准确路径的HttpOnly、SameSite=Strict、host-only Cookie发送，HTTPS为Secure；不保存原JWT/API Key。GET/HEAD先pin，再复核原凭证自然期限/blacklist/代次、当前User/Key/scope、Task可见性或Dataset管理权限、源身份和Gate；所有await之后同步检查同一租约仍有效。DELETE只允许当前本人账号及准确资源/session释放租约。

每进程两个stage、单个5GiB、声明暂存最多10GiB，最多8个响应，每stage32租约。准备900秒、transport30秒、响应120秒是合作式期限。租约300秒、stage最迟3600秒退休；30秒reaper及lifespan负责无人访问时的收尾。退休禁止新pin，实际复制、范围线程与响应pin结束后才真实关闭并释放额度；未知收尾继续占额。

范围适配器用os.pread独立偏移，支持完整GET、HEAD、单范围和强SHA ETag If-Range。复用[RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html#name-range-requests)的状态和范围语义，参考Starlette的范围/关闭生命周期与Django的明确资源closer；普通FileResponse不能直接拥有共享匿名文件和全局保留额度。取消及发送失败必须drain物理线程后unpin，单响应不关闭共享stage。

当前只资格化单Uvicorn实例。匿名stage不进入数据库、备份或恢复；reload、重启、过期或落到无lease实例时返回410，客户端重新Bearer准备。GET不得隐式重建远端完整副本。跨实例共享暂存、路由及集群总预算需要独立合同。

## Consequences

反复seek和并发范围共享一份完整内容证明，能力不会进入请求行，当前授权仍贯穿每个请求。准备延迟包含一次完整复制，临时磁盘与并发额度有明确上限；合作式超时不保证阻塞I/O按硬墙钟结束。

本决定只交付原视频协议基础；现有工作台生命周期、视频DTO/manifest、转码和精确帧派生仍需按各自事实接入。没有新增持久归属表、配置、依赖、服务或镜像要求，回退新增API不改变原件责任。实际接口及限制见[内容读取合同](../../docs-site/api/guides/verified-content.md#视频原件准备与范围读取)。
