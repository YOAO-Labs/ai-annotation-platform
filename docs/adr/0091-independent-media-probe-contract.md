# 0091 — 独立素材探测协议与逐模块提取

- **Status**: Accepted
- **Date**: 2026-10-18
- **Deciders**: 项目维护者授权继续实现所有模块独立部署与语言替换
- **Supersedes**: None

## Context

前序 Python 端口迁移解决了多处依赖方向和写入职责，但没有为全部业务模块提供独立
工件、数据权限和语言无关协议。完整目标继续生效，Go 月报实验的可用性不作为完成门槛。

视频元数据、完整帧时间表和分块 packet 样本探测已有生产、CLI 和测试消费者。
这三个操作只运行 ffprobe 和解析数据，不读写业务数据库或对象存储；探测结果完整后，
原 assets 调用方才更新持久事实。它们适合先建立真实独立进程边界。

## Decision

1. `apps/media-probe` 持有唯一的本地探测实现、类型协议和独立 Python HTTP 服务。
   API 的本地调用也复用这个包，不复制 ffprobe 算法。服务自己的锁文件和镜像只包含
   探测所需依赖，不导入主 API、ORM、Celery 或业务配置。
2. 三个版本化操作是 `POST /v1/metadata`、`POST /v1/frames` 和
   `POST /v1/packets?start_frame=N`，均接收原始文件字节流。不得通过该协议要求
   调用方提供本机路径、存储凭据或任意 URL。独立服务使用专用 bearer token。
   ffprobe 的 [protocol whitelist](https://www.ffmpeg.org/ffmpeg-protocols.html#Protocol-Options)
   限制为 file/pipe，禁止上传文件触发额外网络取源。
3. 元数据返回 JSON；帧和 packet 返回 NDJSON。每条数据行以 LF 结束，最终完成行
   包含记录数量和 SHA-256；摘要覆盖所有数据行的原始 UTF-8 字节及各自行尾 LF，
   不包含完成行。调用方严格验证字段类型、顺序/展示排名、数量和摘要，全部通过才交给
   既有帧索引替换命令。断流、格式错误或未知响应都不能成为部分成功。
4. 独立服务的 `/openapi.json` 是 wire 合同真值；`openapi.json` 快照由同一实现
   导出并检查，包含请求体、参数、错误和 NDJSON 记录 schema，不手维护第二份协议。
   采用既有 FastAPI/Pydantic 工具链生成独立合同；相关能力见
   [FastAPI 独立应用说明](https://fastapi.tiangolo.com/advanced/sub-applications/)。
5. `MEDIA_PROBE_URL` 显式选择独立服务；空值调用同一包的本地实现。选择远程后不
   自动回退，不重试或改变原调用方的错误/提交顺序。`probe_timeout` 恢复为原子进程
   超时类别，其余失败继续由原工作流记录或回滚。正常环境不会因加入本服务自动远程化。
6. 新运行单元先在本地隔离验证。Node.js 仅用于真实 ffprobe 的协议替换验收夹具，
   不作为生产服务选型；同一真实媒体、合同和消费者验证两种运行时的结果。

## Consequences

- 探测服务不取得 assets SQL 写入权。原调用方继续授权、读取源对象、管理账本和提交；
  标注/审核、撤权、审计和通知等强事务仍需在后续模块提取中解决，不能由本片证明。
- 每个远程操作上传一次源文件，增加网络流量和临时磁盘开销。请求采用流式临时文件，
  默认文件上限 1 GiB；HTTP I/O 与 ffprobe 子进程各有明确超时。协议不会消除原算法
  对完整 ffprobe 输出和帧/packet 列表的内存需求；容器预算不代表长视频容量已通过。
- 本片提供一个真实业务子能力的独立部署与替换证据，不把 assets 或全后端标为完成。
  全模块范围、剩余事务与运行门槛见[模块部署状态](../../docs-site/dev/concepts/module-deployment.md)。
- 本地/远程两种装配方式使用同一公开能力；切换只读探测实现不迁移业务数据。默认配置
  可保持本地执行，停用独立服务前须刷新已选择它的调用方，不能通过故障回退掩盖停服。
