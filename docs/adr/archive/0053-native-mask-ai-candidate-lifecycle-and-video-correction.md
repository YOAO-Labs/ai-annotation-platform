# 0053 — 原生 Mask AI 候选生命周期与视频局部纠错

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** core team
- **Supersedes:** —（扩展 [ADR-0048](./0048-video-raster-mask-content-addressed-rle.md) 与 [ADR-0052](./0052-shared-raster-mask-and-image-geometry.md) 的持久 Mask 合同，不改变其内容寻址格式）

## Context

平台已能把图片 `raster_mask` 与视频 `video_track_mask` 保存为内容寻址 COCO RLE，但交互式 SAM
仍存在两类生命周期没有冻结：单帧候选及 low-resolution logits 应否持久化，以及视频 Tracker
漂移后如何只纠正一个目标的有限窗口。若继续沿用 polygon 中转或整 job 接受，会丢失 hole / 多连通
区域，或覆盖窗口外与人工关键帧；若把每个未接受候选都写对象存储，又会产生大量无人认领的内容对象。

候选方案：

| 选项                                                           | 主要卖点                                                                 | 主要劣势                                                      |
| -------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------- |
| **A. 瞬态单帧候选 + 原子接受 + staged 视频局部决定（本 ADR）** | 未接受数据不污染持久真值；接受和纠错都在服务端复核版本与锁；像素语义完整 | 浏览器刷新会释放单帧候选；需要 TTL、GC、局部状态机和更多观测  |
| B. 每个候选立即持久化为 Prediction / RLE 对象                  | 刷新后容易恢复                                                           | 候选风暴放大数据库与对象存储；取消语义和清理责任不清          |
| C. 客户端把 Mask 转 polygon 后沿用旧接受路径                   | 改动最少                                                                 | hole、多组件和像素边界有损；视频纠错仍只能整窗覆盖            |
| D. 后端维护长生命周期推理 session                              | logits 与模型状态可跨请求复用                                            | 引入有状态 GPU 服务、故障恢复和跨实例粘性，超出当前可靠性预算 |

## Decision

### D1. 单帧候选只在当前交互会话存在

- 图片与视频单帧原生候选通过受限 HTTP 响应返回完整 COCO RLE；未接受候选不写 Annotation、
  Prediction 或不可变 RLE 对象。
- 浏览器按 task、frame、backend / model 与 prompt revision 持有候选。切题、切帧、切模型、取消、
  接受或会话过期即释放；网络失败保留当前候选与幂等 key，允许原地重试。
- `candidate_id` 由规范 RLE 摘要、prompt revision 和候选序号稳定派生，只用于选择与诊断，不是
  对象 key 或访问凭证。
- 请求与响应继续遵守尺寸、像素、runs、单对象 4 MiB 和整响应 16 MiB 上限；空前景返回空结果，
  不用零 bbox、空 polygon 或全图占位冒充 Mask。

### D2. logits 使用短期加密鉴权令牌

- backend 的 `mask_input_next` 由 API 放入 Fernet 加密且鉴权的五分钟令牌，密钥从部署 secret
  通过域分离 SHA-256 派生。密文绑定 task、frame、实际 backend、model、prompt revision 和候选。
- 令牌不写数据库、对象存储、审计详情或普通日志；浏览器只把它回传给同一交互链路。
- API 在转发前验证密文、版本、签发 / 到期时间、绑定 claims 与内部 logits 编码；篡改返回
  `invalid_mask_session`，到期返回 `mask_session_expired`。
- 不建立服务端 session cache。浏览器丢失令牌时重新推理，这是有意接受的无状态恢复代价。

### D3. 接受是服务端原子业务操作

- 客户端提交选中 RLE、prompt 摘要、实际路由与模型 lineage、源 annotation version 和
  idempotency key；服务端重新验证任务可见性 / 可编辑性、Raster 写闸、类别、媒体尺寸、RLE、
  assignment、对象锁与 `If-Match`。
- 服务端先写不可变 RLE，再在一个数据库事务中写 Prediction、决定快照、Annotation、lineage 与
  审计。事务失败只留下受 24 小时宽限 GC 管理的孤儿对象，不留下半个业务决定。
- task + idempotency key + candidate digest 的重试返回同一响应；同 key 不同 digest 返回 409。
  接受决定快照保留 24 小时，过期后由每日 Raster Mask GC 删除并释放其中引用。

### D4. 视频 Tracker 使用 staged 局部决定

- Tracker result 先写 `video_tracker_jobs.staged_result`，不直接修改 Annotation。候选以
  `instance_id + frame_index + digest` 形成稳定 key，review API 接受明确 instance 集合、闭区间、
  `accept | reject`、job revision 与源版本快照。
- 第一次局部决定后进入 `partially_reviewed`，只删除已经决定的 staged slice；所有候选决定完成后
  才进入 `accepted` 或 `discarded`。接受仅合并所选目标 / 窗口，拒绝只移除候选。
- manual keyframe 默认受保护。命中时返回 `manual_keyframe_protected`；只有具备编辑权限并显式
  `override_manual=true` 才能覆盖，审计保存帧、before / after digest 与源 / 结果 version。
- decision 事务锁 job、task、segment 与受影响 annotations，复核 job revision、source version、
  assignment 和 segment lease；任一漂移稳定返回 409，不执行部分写入。

### D5. 人工纠错关键帧与定向重传播分离

- 用户先把当前帧作为 `source="manual"` 的纠错关键帧保存，再选择仅保存、向前、向后或双向重传播。
  已保存人工关键帧不是 correction job 的临时产物，取消 job 时仍保留。
- correction job 冻结 track、纠错帧、方向、窗口、corrected RLE digest、源 version、backend / pool /
  model、segment lease 与 `protect_manual=true`；同一 track 同时只允许一个活跃 correction job。
- backend 声明 `correction_frame` / `mask` 时使用原生 Mask seed；不支持时必须由用户确认 bbox fallback，
  并记录 `fallback_reason=mask_prompt_unsupported`、seed digest 与 bbox。禁止静默降级。
- correction 只生成所选窗口的 staged candidate，之后仍走 D4 的 preview / local decision；窗口外、
  其它轨迹、outside / occluded 段和受保护人工帧保持不变。

### D6. 生命周期、可观测性与隐私

- 待审或已取消且仍带 staged result 的 Tracker job，自 `completed_at` 起最多保留 24 小时。每日 GC
  先清 staged result、递增 revision 并释放引用；`pending_review` / `partially_reviewed` 转 `discarded`，
  `cancelled` 保持取消状态，然后才扫描对象引用并执行宽限删除。
- 平台指标只使用受控低基数 operation、prompt、geometry、candidate bucket、decision、fallback、
  outcome 与 phase；HTTP path 使用路由模板。数据库 Gauge 输出固定零序列，避免库存为零时误报缺失。
- backend 指标按受控 model role、tracking / correction、fallback、candidate bucket 与 outcome 统计。
  task id、annotation id、object key、digest 和用户提示不得进入 Prometheus label。
- 普通日志不写完整图片、RLE counts、scribble 点集、文本提示或 logits。结构化审计只保存定位业务决定
  所需的对象 id、版本、窗口、摘要、路由与 fallback，不保存正文。

## Consequences

正向：

- 原生 Mask 从候选到持久 Annotation 保持逐像素一致，未接受候选不会污染持久数据。
- 单帧重试、Tracker 局部审核与视频纠错共享明确的幂等、版本、锁和 manual 保护边界。
- staged reference、决定快照和 logits 都有可证明的释放路径；指标、告警和 dashboard 能区分推理、
  响应处理、提交、冲突与 GC 积压。
- API 与 GPU backend 保持无状态路由，不需要为 logits 引入粘性 session 服务。

负向：

- 单帧候选不能跨浏览器刷新恢复；用户需要重新推理。
- Fernet 令牌比签名明文包更大，仍受 1 MiB token 上限约束；部署 secret 轮换会使旧会话立即失效。
- Tracker 待审候选超过 24 小时会被自动丢弃，用户必须从已保存人工关键帧重新发起。
- `/metrics` 为库存指标执行数据库聚合查询，需要监控查询成本并在数据量增长后评估预聚合。

## Alternatives Considered（详）

**方案 B（候选立即持久化）**：multimask、多轮 point / scribble 和快速切帧会产生大量从未接受的
Prediction 与对象引用；即使增加 TTL，也会把纯 UI 暂态变成数据库状态机。拒绝。

**方案 C（polygon 中转）**：与 ADR-0052 的无损约束冲突，无法证明 hole、多连通与像素边界一致；
也不能表达视频 Mask seed 和逐帧 staged reference。拒绝。

**方案 D（有状态 GPU session）**：能够复用完整模型状态，但要求实例粘性、session 迁移、显存配额、
故障恢复和跨进程清理；当前五分钟无状态令牌已覆盖 low-resolution refine。延后到有直接性能证据时再议。

## Notes

- 平台实现：`apps/api/app/services/ai_mask_session.py`、`apps/api/app/services/ai_mask_accept.py`、
  `apps/api/app/services/video_tracking/runner.py`、`apps/api/app/workers/cleanup.py`。
- API：`apps/api/app/api/v1/tasks/ai_masks.py`、`apps/api/app/api/v1/tasks/video.py`、
  `apps/api/app/api/v1/video_tracker_jobs.py`、`apps/api/app/api/v1/ml_backends.py`。
- 观测：`apps/api/app/observability/metrics.py`、`apps/api/app/observability/mask_ai.py`、
  `infra/prometheus/alerts.yml`、`infra/grafana/dashboards/`。
- 数据迁移：`0138_video_tracker_review_revision.py`、`0139_video_correction_jobs.py`。
- 后续大画布 / tile / Worker 性能决策顺延为 ADR-0054，由独立基准冻结参数。
