# 待实现路线图

> 本文件只记录当前源码仍缺失的能力。历史状态与交付记录见
> [CHANGELOG.md](CHANGELOG.md) 和 [docs/changelogs/](docs/changelogs/)。
>
> 判断以当前数据模型、API、执行链路和可达 UI 为准；只有协议或字段地基、但没有完整用户链路的，
> 仍视为未完成。

## 当前主线

### 视频工作台收尾

详见[视频工作台路线](ROADMAP/2026-05-21-video-workbench-roadmap.md)。

- 在可获得的 Windows Edge 与 macOS Safari 客户端补齐 1080p/4K correctness、可见延迟、资源 plateau
  与 fallback rate。

### 超大图 Tile 与 Raster Mask 客户端计算

详见
[v0.23.21–v0.23.25 超大图 Tile 与 Raster Mask 客户端计算 Epic](docs/plans/archive/2026-07-31-v0.23.21-v0.23.25-large-image-tile-webgpu-epic.md)。

- 在可获得的无 flag Linux Wayland 与 Windows D3D12 客户端补齐 one-pass correctness、长会话、p95、
  fallback rate，以及超大图 tile 首屏、pan/zoom 帧率、长会话 plateau 与 DPR A/B；没有机器时保持
  `not tested`，不外推也不改变 capability-first fallback。已完成平台与实测口径见
  [macOS WebGPU 性能实测](docs/research/25-macos-webgpu-performance.md)。
- Raster Mask 仍受单边 8192、总计 67,108,864 pixels 和 morphology ROI 16,777,216 pixels 约束；
  超大图背景可超过该上限，扩大 Mask 协议另立版本。

### 3D 标注工作台 0.24.x

详见[v0.24.x 3D 标注工作台优化 Epic](docs/plans/archive/2026-08-14-v0.24.x-3d-workbench-optimization-epic.md)。

- WebGPU 点云渲染保持默认关闭的实验功能：跨厂商验证仍缺 Intel/AMD（`not tested`），Safari 长序列
  曾停在 geometry 完成前，待复现诊断；验证完成前不得移除实验标记或默认启用。已完成平台与实测口径见
  [macOS WebGPU 性能实测](docs/research/25-macos-webgpu-performance.md)。
- 多模态几何扩展：相机 polygon / mask 与跨相机批量复核。
- nuScenes 官方 benchmark ontology / split 映射独立立项。
- 地面预览需先形成可解释的地面分割、残差和置信合同，不复用放框高度估计伪装测量事实。

## 独立 Epic

### 数据闭环与训练

- 数据集不可变 snapshot、lineage 与训练复现。
- 主动学习选样、智能切批、批次级 IAA / 共识合并。
- 训练任务状态机与 UI；当前 `/training` 仍是占位入口。
- 严格批次暂停：统一任务可见性、锁归属和写入门禁，补足现有 soft hold 语义。

参考[大规模数据集切批调研](docs/research/12-large-dataset-batching.md)和
[长期规划 L1/L2](ROADMAP/2026-05-12-long-term-strategy.md)。

### 大文件浏览器直传

- 为大于 5GB 的视频和点云提供 multipart upload init / part / complete / abort 协议。
- 保留失败清理、断点续传、内容校验和重复文件检测。

### 多模态工作台扩展

- 为 `video-mm` / `mm` 建立专用工作台分流和交互模型。
- 多 LiDAR 融合标注按真实项目需求单独立项。

## 需求触发 Backlog

### AI 与模型平台

- **模型变体实验**：按稳定实验分组路由非等价变体、记录实验标签，并在工作台并排比较同输入结果。
- **受保护的运行时观测**：让 env-only backend 的 observe 探测支持认证 token。
- **预测导入延伸**：仅在跨实例匹配不再能依赖 `display_id` / `file_path` 时增加 Task `external_id`；按审计需求补 `predictions_import` 取证明细。
- **批量推理**：吞吐压测证明瓶颈后，将 `batchable` 分块派发与 backend 真 GPU batch 一起设计，避免只摊销 HTTP 开销。
- **编排新数据源**：支持既有矩形标注作为 crops 源；scene 跨帧聚合作为独立执行单位。
- **生产存储地址策略**：首个复杂部署出现时扩展 `ML_BACKEND_STORAGE_HOST` 的 endpoint 选择规则。
- **类别确认建议**：Magic Box 产出后提供模型类别 hint，由标注员确认。

### 项目模板

- 模板版本快照、变更对比和审计轨迹。
- organization admin 提交 public 模板的审核流。
- 模板使用明细与传播路径；总使用次数不重复建设。
- AAP JSON 携带模板，实现跨实例导入。
- 审计期确有取证缺口时补模板专项 detail。

### 账号、组织与安全

- 用户级语言和时区偏好。
- 可操作的组织 / 工作区切换器。
- OAuth2 / 企业 SSO；2FA / TOTP 由客户安全要求触发。
- i18n 文案体系，以及可量化的无障碍审计和 CI 门禁。

### 集成、治理与运营

- Webhook 订阅、outbox、重试投递和签名校验；复用现有事件信封 schema。
- Bug Report 的 LLM 聚类、邮件摘要投递和用户邮件通知偏好。
- AnnotationFeedback 旧表退役并切为单一写入源。
- 首次进入标注工作台的一次性 onboarding。

### 标注体验

- 视频单帧折线、polygon track、polyline track 的无冲突快捷键。
- `U` 键改为服务端按真实预测置信度查找下一最不确定任务，不再按预测数量近似。
- 明确 outside 帧是否显示 ghost 参考几何；产品决策后统一实现与测试。
- 评估视频连接层 wedge 后自动硬刷新的收益与标注中断风险。

## 规模或监控触发

- **学习式动静分割**：邻帧点云中的未标注动态目标确实影响生产时再引入。
- **模板治理升级**：误改、公共模板数量或跨组织发布达到现有人工流程上限时启动。
- **标定历史分页**：单相机 revision 数量使历史接口或管理面板出现可观测延迟时，引入服务端硬上限的游标分页和前端渐进加载。

## 工程维护

- 提升 `BatchesSection`、`useWorkbenchShellModel`、`useImageAnnotationActions` 等复杂页面和 hook 的有效测试覆盖，不在路线图固化易过期的覆盖率快照。
- 为 `vite` 代理 `/ws` 的多并发 CONNECTING 卡死制作最小复现；生产链路不受影响。
- 在已建立的 Playwright 视觉基线上，拆分 `useStageViewport` 相关的首帧 paint 时序。

## 战略与研究输入

- [长期规划（12 个月以外）](ROADMAP/2026-05-12-long-term-strategy.md)
- [CVAT / Label Studio 取经合集](ROADMAP/2026-05-18-cvat-labelstudio-inspiration.md)
- [视频工作台路线](ROADMAP/2026-05-21-video-workbench-roadmap.md)
- [v0.24.x 3D 标注工作台优化 Epic](docs/plans/archive/2026-08-14-v0.24.x-3d-workbench-optimization-epic.md)

架构反模式与决策底线统一维护在
[取经合集 §6](ROADMAP/2026-05-18-cvat-labelstudio-inspiration.md#6-避坑清单保持当前选择不要走回头路)，
不再与路线任务混排。
