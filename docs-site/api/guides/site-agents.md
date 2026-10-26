---
title: 只读站点 Agent 与传输 API
audience: [dev, ops, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# 只读站点 Agent 与传输 API

只读站点 Agent（`aap-storage-agent`）用于让中心无法主动入站访问的数据服务器通过出站 HTTPS 连接平台，仅开放管理员在本地显式配置的只读目录根。接口分为两部分：**超级管理员管理端点**（站点、Agent 身份生命周期与 Agent 存储位置登记）与 **Agent 配对及出站传输端点**（一次性配对、命令长轮询、分块上传与结果回执）。

部署与升级步骤见[只读站点 Agent 运维指南](/ops/deploy/site-agents)，界面操作见[跨网络站点与 Agent](/user-guide/datasets/site-agents)。

## 超级管理员管理端点

管理接口位于 `/api/v1/site-agents` 与 `/api/v1/storage-locations/agent`。仅当前有效 `super_admin` 可调用；使用 API Key 时，读取要求 `datasets:read`，变更要求 `datasets:write`。公开响应从不返回配对码摘要、凭据 token 或数据服务器上的宿主机绝对路径。

| 方法   | 路径                                           | 权限与行为                                                                                                                                                                           |
| ------ | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET`  | `/api/v1/site-agents/sites`                    | 超管（`datasets:read`）；分页列出站点（`limit` 1–200、默认 100，`offset` 默认 0），返回 `{items, total}`。                                                                           |
| `POST` | `/api/v1/site-agents/sites`                    | 超管（`datasets:write`）；提交 `{name, region?}` 创建站点，返回 `201 SiteOut`；同名冲突返回 `409 site_name_conflict`。                                                               |
| `GET`  | `/api/v1/site-agents/agents`                   | 超管（`datasets:read`）；支持 `site_id`、`agent_id` 过滤与 `limit` / `offset` 分页，返回心跳、协议/凭据修订、受控根 `roots` 与预算。                                                 |
| `POST` | `/api/v1/site-agents/sites/{site_id}/agents`   | 超管（`datasets:write`）；提交 `{name, limits?}` 注册 Agent 并签发 10 分钟有效的一次性 `pair_code`，返回 `201 AgentPairingOut`（`Cache-Control: no-store`）。                        |
| `POST` | `/api/v1/site-agents/agents/{agent_id}/drain`  | 超管（`datasets:write`）；提交 `{expected_revision, draining}` 暂停或恢复向该 Agent 派发新命令，允许已领取命令继续收尾。                                                             |
| `POST` | `/api/v1/site-agents/agents/{agent_id}/rotate` | 超管（`datasets:write`）；提交可选 `{expected_revision}` 轮换身份凭据并返回新的一次性 `pair_code`。要求 Agent 已处于 `draining=true` 且无 `leased`、`received` 或 `unsettled` 命令。 |
| `POST` | `/api/v1/site-agents/agents/{agent_id}/revoke` | 超管（`datasets:write`）；提交可选 `{expected_revision}` 立即撤销当前身份。新任务领取与分块上传立即拒绝，旧凭据摘要保留用于核对关闭回执。                                            |
| `POST` | `/api/v1/storage-locations/agent`              | 超管（`datasets:write`）；提交 `{name, agent_id, root_handle, root_revision, relative_path, scope, use_user_ids, cache_policy, ...}` 登记 `kind="agent"` 的只读存储位置。            |

### 预算与状态字段

- **`limits` 与 `reported_limits`**：包含 `max_concurrency`（1–4，默认 1）、`bandwidth_bytes_per_second`（1 KiB/s–1 GiB/s，默认 8 MiB/s）与 `max_bytes`（1 B–64 MiB，默认 64 MiB）。中心派发命令时取平台配置的 `limits` 与 Agent 上报的 `reported_limits` 中的较小值。
- **`state`**：根据配对、撤销与最后心跳（60 秒窗口）投影为 `pending`、`online`、`degraded`、`offline` 或 `revoked`。心跳超时仅标记 `offline`，不会删除已采纳样本或关联位置。
- **`roots`**：Agent 轮询时上报的受控根数组 `[{root_handle, root_revision}]`（最多 32 个，`root_handle` 匹配 `^[A-Za-z0-9_-]{1,64}$`）。调用 `POST /api/v1/storage-locations/agent` 时，`root_handle` 与 `root_revision` 必须匹配 Agent 当前上报值。

## 一次性配对与出站传输端点

以下端点专供 `aap-storage-agent` 客户端通过出站 HTTPS 调用，不接受平台用户 JWT 或普通 API Key。除 `/pair` 使用一次性配对码外，其余传输端点均使用配对换取的专用 `Authorization: Bearer <agent_token>`、协议头 `X-Agent-Protocol: 1` 以及会话标识 `X-Agent-Session`（耐久保存在 Agent 本地日志中的 session UUID）。

| 方法   | 路径                                               | 认证与用途                                                                                                                                                   |
| ------ | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `POST` | `/api/v1/site-agents/pair`                         | 免 Bearer；提交 `{agent_id, pair_code, protocol_version: 1}` 消费一次性配对码，返回 `{agent_id, credential_revision, token, expires_at}`（最长 30 天有效）。 |
| `POST` | `/api/v1/site-agents/commands/poll`                | Agent Bearer；提交 `{protocol_version: 1, agent_id, credential_revision, session_id, roots, limits}` 更新心跳与根列表，长轮询最多 15 秒领取一条命令。        |
| `POST` | `/api/v1/site-agents/commands/{command_id}/chunks` | Agent Bearer + `X-Command-Digest` + `X-Chunk-Offset` + `X-Chunk-Sha256`；以 `application/octet-stream` 上传 `read` 命令的原图分块。                          |
| `POST` | `/api/v1/site-agents/commands/{command_id}/result` | Agent Bearer（允许已轮换/撤销/过期的保留凭据提交关闭证据）；提交命令执行结果 JSON 与物理关闭状态 `closed`，返回中心确认状态。                                |

### 命令生命周期、分块预算与关闭证据

1. **命令租约与优先级（120 秒上限）**：
   中心将 `probe`、`browse`、`scan`、`read` 封装为有界命令写入 `site_agent_commands`，最长有效期 **120 秒**（应用当前默认签发 45–60 秒，且不超过凭据 `expires_at`）。中心全局最多同时激活 **4 个**中继槽位（`GLOBAL_SLOTS = 4`），按 `read`（优先级 0）高于 `probe`/`browse`（优先级 1）高于 `scan`（优先级 2）的顺序派发。若轮询响应在网络中断中丢失，同一耐久 `session_id` 重新轮询会重投同一条未结清命令，保持原 `command_id`、`command_digest` 与原定 `expires_at`，绝不自动延长授权。
2. **有界分块传输（256 KiB 分块 / 64 MiB 上限）**：
   执行 `read` 命令时，Agent 先在本地受控根内通过 `openat2` 安全读取完整文件、计算 SHA-256 并复核源文件未发生读中变化，生成本地耐久 stage（单文件上限 **64 MiB**）。随后向 `/commands/{command_id}/chunks` 按顺序上传分块：单块最大 **256 KiB**，最多 **1024 块**，请求头必须携带匹配的 `X-Command-Digest`、严格递增的 `X-Chunk-Offset` 与单块 `X-Chunk-Sha256`。相同偏移的相同分块重传幂等返回 `AgentChunkAck`，不同内容冲突返回 `409 agent_chunk_replay_conflict`。
3. **三态结果确认（`received` / `applied` / `rejected`）与 `closed` 证据**：
   Agent 向 `/commands/{command_id}/result` 提交包含 `command_id`、`command_digest`、`status`（`completed` 或 `failed`）、`closed`（布尔值，表示本地文件句柄与读取线程是否已真实关闭）、`error_code` 与 `result` 的 JSON（上限 2 MiB）。中心校验命令摘要、分块完整性与载荷格式后返回 `AgentCommandAck`：
   - `state="received"`：中心已持久记录结果与分块收据，等待调用方业务事务（如探测落库、扫描清单采纳或原图快照读取）最终消费；
   - `state="applied"`：业务调用方已完整核验并消费该结果，中继缓存已释放；
   - `state="rejected"`：因授权变化、位置修订变化、命令过期或校验失败被中心拒绝，附带具体 `code`。
     Agent 只有在本地确认 `closed=true` 且收到身份、命令摘要与结果摘要全部匹配的终态确认（或明确的中心拒绝回执）后，才释放本地待确认 stage。若本地物理读取超时且句柄尚未确认关闭，Agent 上报 `closed=false`，中心将命令置为 `unsettled` 并保留 `StorageOperation` 访问责任，绝不按超时猜测物理句柄已关闭。
4. **退役凭据的关闭回执通道**：
   当凭据已过期、被轮换或被撤销，或者丢失领取响应的命令已超过 `expires_at` 时，中心拒绝其领取新命令或上传分块，但仍允许该保留凭据向 `/commands/{command_id}/result` 提交 `closed` 关闭证据，使中心得以安全结清未决的 `StorageOperation` 账本。

## 相关文档

- [受控存储位置与迁移 API](./storage-locations#agent-位置) — `POST /api/v1/storage-locations/agent` 位置登记与 `agent:<UUID>` 探测合同
- [数据集上传与扫描 API](./datasets#引用样本备用来源) — `/api/v1/dataset-items/{item_id}/reference-replicas` 跨站点同内容显式备用来源关联
- [内容核验与严格读取 API](./verified-content#图片任务) — Agent 引用图片的单区间 Range / `If-Range` 核验读取合同
- [只读站点 Agent 运维指南](/ops/deploy/site-agents) — 独立虚拟环境安装、本地 `--check`/`--drain`、迁移 `0265`–`0267` 与兼容升级
