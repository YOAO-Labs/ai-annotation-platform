---
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 独立素材探测协议

独立素材探测服务处理视频元数据、完整帧时间表和分块 packet 样本。它直接接收文件
字节，不需要平台数据库、Redis 或对象存储凭据。启动方式见仓库 `DEV.md` 的
“独立素材探测服务”；全模块提取进度见[模块部署状态](/dev/concepts/module-deployment)。

该服务在自己的地址提供 `/openapi.json`，主 API 的 `/api/v1` 路由和公开 SDK 目录不
包含这些内部操作。服务使用专用 `Authorization: Bearer` token，平台 JWT/API key
不能替代它。类型、参数和错误的规范产物是 `apps/media-probe/openapi.json`，由服务
应用导出，不独立维护第二份 schema。

## 操作

所有 POST 请求的 `Content-Type` 都是 `application/octet-stream`，body 是文件原始
字节；不得发送 JSON 路径、URL 或对象存储 key。原生探测限制为 file/pipe 协议，不会
从媒体内嵌的网络引用取得额外输入。

| 操作                             | 返回                 | 规则                                                                             |
| -------------------------------- | -------------------- | -------------------------------------------------------------------------------- |
| `POST /v1/metadata`              | `VideoMetadata` JSON | 时长毫秒、帧率、帧数、宽高与 codec，未知字段为显式 null                          |
| `POST /v1/frames`                | frame NDJSON         | `frame_index` 从零连续递增，保留源时间戳、关键帧和字节偏移                       |
| `POST /v1/packets?start_frame=N` | packet NDJSON        | 按解码顺序返回，`frame_index` 是按展示时间排序的唯一排名，加上非负 `start_frame` |
| `GET /health`                    | 进程健康 JSON        | 无需认证，不表示长视频容量通过                                                   |
| `GET /openapi.json`              | 独立服务合同         | 包括二进制请求、参数、错误和 NDJSON 记录 schema                                  |

例如，在已配置服务 URL 和专用 token 后，上传本地视频取得元数据：

```bash
curl --fail --request POST \
  --header "Authorization: Bearer $MEDIA_PROBE_TOKEN" \
  --header "Content-Type: application/octet-stream" \
  --data-binary @clip.mp4 \
  "$MEDIA_PROBE_URL/v1/metadata"
```

## 流完整性

帧数据行是 `VideoFrameRecord`，packet 数据行是 `PacketSampleRecord`；两者以
`type` 和 `data` 组成记录。每一行都是 UTF-8 JSON，并以 LF 结束。最后必须有
`StreamComplete`，包含 `type=complete`、`count` 和小写十六进制 `sha256`。

摘要覆盖全部数据行的原始字节，包含每个 LF，不包含完成行。空数据流使用空字节串的
SHA-256。消费者需严格验证类型、帧顺序或 packet 排名、数量和摘要；丢失完成行、
摘要不符、多余记录、断流或无效数据都必须拒绝。完成前不能用部分结果替换持久帧索引。
OpenAPI 的 `x-ndjson-record-schemas` 列出允许的记录类型。

## 失败与预算

| 状态 | 错误码            | 含义                           |
| ---- | ----------------- | ------------------------------ |
| 400  | `invalid_request` | 无效参数、类型或空文件         |
| 401  | `invalid_request` | 专用 token 缺失或不匹配        |
| 413  | `file_too_large`  | 输入超过服务配置上限           |
| 422  | `probe_failed`    | 媒体无法探测或结果不能满足合同 |
| 504  | `probe_timeout`   | ffprobe 超过 30 秒子进程上限   |
| 500  | `probe_failed`    | 非预期探测失败                 |

服务默认最多接收 1 GiB 文件，流式写入自有临时文件；数据行上限为 64 KiB。完整
ffprobe 输出及结果列表仍需进程内存，上传上限不构成内存容量保证。每次远程操作上传
一次源文件，会消耗网络和临时磁盘。

平台客户端通过 `MEDIA_PROBE_URL` 选择本地或远程实现，远程 I/O 超时由
`MEDIA_PROBE_TIMEOUT_SECONDS` 控制；选择远程后不自动回退。授权、源对象读取、
最终 SQL 写入和提交继续归原调用方；服务失败时沿用相应工作流的错误及回滚规则。
