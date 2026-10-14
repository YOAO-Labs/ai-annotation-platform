# 媒体清单（用户手册）

> 这个文件位于 `docs-site/maintainers/image-checklist.md`，不参与文档站发布，仅作为 maintainer 的工作清单。
>
> 标 `[auto]` 的图由 `pnpm --filter @anno/web screenshots` 自动生成（脚本：`apps/web/e2e/screenshots/`），输出到 `docs-site/user-guide/images/`。
>
> 所有流程录制先归档标准源或高清营销母版，再由 `pnpm docs:media:derive` 统一派生 GIF、文档 MP4、首页 WebM/MP4 和封面；`screenshots:flows` 不再直接覆盖 GIF。标准源需显式选择 `--quality standard`，发布时（包括 GIF）指定审阅过的 `--clip start:duration`，不能冒充 4K60 母版。
> 更新流程：
>
> 1. 启动 docker / api / dev 三件套（同 `pnpm test:e2e`）
> 2. 修复截图 seed 并按能力绑定 live backend；无 GPU 时显式使用协议 stub
> 3. `pnpm --filter @anno/web screenshots:matrix`、`pnpm --filter @anno/web screenshots:flows` 和 `pnpm docs:media:derive`；首页 Hero 源图有变化时再运行 `pnpm --filter @anno/docs-site media:home-hero`
> 4. 人眼审阅全部 PNG，并检查 MP4/GIF 的起点、核心动作、结果落地和终点
> 5. 运行 `pnpm docs:media:audit -- --release`、manifest release gate 和 orphan strict gate 后再提交

## 拍摄约定

- **分辨率**：desktop 1440×900、mobile 390×664；高清母版 3840×2160 / 60fps；文档 MP4 1280×720 / 30fps
- **格式**：截图 PNG；不超过约 6 秒的简单动作可用 GIF；AI、视频、3D 和长链路使用 H.264 MP4 + WebP 封面
- **GIF 编码**：全局调色板配合有序抖动与矩形差分，减少静止 UI 的帧间噪点和文件体积；保持已审阅的分辨率、帧率与动作窗口，单文件遵守仓库 2 MiB 上限，不改动采集源或高清母版。
- **浏览器**：Playwright 固定版本 Chromium；时区、语言、DPR、时钟和动画由 driver 统一固定
- **数据来源**：只使用 `screenshots` seed catalog 的固定逻辑键；不从开发库随机选择项目或任务
- **动态信息**：账号、时间和通知数等不稳定区域由统一 mask 规则处理
- **标注红框**：引导读者注意的位置用红框（#FF3333，2px）
- **主题**：普通页面默认浅色；图片、视频、点云工作台统一以暗色作为无后缀主产物，其他主题按 scene 声明生成后缀变体
- **保存路径**：`docs-site/user-guide/images/<page>/<name>.png`

矩阵数量以 manifest、scene、磁盘文件和文档引用四方一致为准；每次重录后重新核对各 project 数量，避免在清单中保留会漂移的手工统计。

### 暗色工作台重录

视频基础重录范围为 `public/media/video/{video-draw,video-track-carryover,mask-track-edit,video-timeline-zoom,video-chapter}.mp4` 及对应海报，使用视频追踪停靠预设和展开时间轴。旧 GIF 配方仅归档，不恢复已退休的引用；此前已确认的视频总览保留。

- [x] 图片工作台静态图：`workbench/layout-overview.png`、`mask-brush/toolbar-overview.png`、`sam/{smart-point-toolbar,interactive-toolbar,magic-box-toolbar,exemplar-output-mode}.png`、`workbench/ocr-real-scene.png`、`review/{workbench,reject-form}.png`
- [x] 图片工作台流程：短交互保留 GIF；Magic Box、OCR、AI 预标与候选审阅已迁移到 `public/media/**/*.mp4`
- [x] 首页图片工作台媒体：`public/home/ai-assisted-annotation.*`、`public/home/sam-tools/{smart-point,smart-box,exemplar}.*` 与 `public/home/ocr-real-scene.*`，按各自已验证来源派生 WebM、H.264 MP4 fallback 和 WebP 封面；标准录制保留来源的尺寸与帧率上限，不标记为 4K60 母版
- [x] 首页 Hero 派生图：`theme/assets/home/hero/*.webp`，由对应用户手册截图生成
- [x] 视频工作台：静态总览保留 PNG；工作台播放、AI 面板、多目标种子和 Mask 轨迹编辑已迁移到 `public/media/video/*.mp4`
- [x] 点云工作台：`workbench/pointcloud-real-scene.png`（nuScenes 六相机环视）+ `public/media/pointcloud/{controls,orbit,billboard-label,crossframe-track,panel-layout}.mp4`
- [x] 删除过期图：旧 `sam/text-three-modes.png` 与未展示命名状态、带权限告警的 `polygon/{vertex-edit,close-hint}.png`

## Batch 1 · 数据集 / 导入导出（新增于 2026-06-10 · IA 重构）

> 文档侧已在对应位置打 `<!-- TODO(v0.14.18) IMAGE_CHECKLIST: ... -->` 注释占位（未嵌 `![]()`，不破图）；下面是配套拍摄清单。新增图片目录 `images/datasets/`。

### 数据集 · 导入

- [x] `images/datasets/import-images-wizard.png` — 导入数据集向导基本信息步选「图像」+ 来源选「ZIP 上传」；红框：数据类型选择、文件拖放区 `[auto]`
- [ ] `images/datasets/import-wizard-3d-type.png` — 导入向导基本信息步选「3D 点云」+ 勾「声明为时序数据集」；红框：数据类型、时序开关、axis_convention 选择器 **[Tier B]** 向导第 1 步须先上传文件「下一步」才可点，需文件上传 fixture 才能进到基本信息步
- [ ] `images/datasets/pointcloud-dir-layout.png` — 单 scene vs 多 scene 目录树并排对比（建议矢量示意图而非真截图）

### 数据集 · 存储连接器

- [x] `public/media/datasets/storage-connector-create-test.mp4` — 数据连接器空列表 → 新建脱敏 S3 / OSS 数据源 → 密钥已加密 → 真实测试返回「连接成功 · 样本 4」，覆盖新建表单与测试结果 `[derived-mp4]`
- [ ] `images/datasets/connector-import-step.png` — 导入向导「连接器导入」子面板；红框：source_path / 递归开关 / include_globs
- [ ] `images/datasets/connector-allowlist.png` — 超管连接器主机白名单配置面板 ⚠️ **前端 UI 尚未实现（仅超管 API），待 UI 就绪再拍**

### 预测导入 / 导出

- [x] `images/projects/prediction-import-wizard.png` — 导入预测弹窗（AAP JSON / COCO / YOLO 格式选择 + 文件上传 + 替换开关）；红框：格式下拉、YOLO 变体下拉、替换开关 `[auto]`
- [x] `images/projects/prediction-purge-modal.png` — 清理预测弹窗三种来源范围 + ML Backend 风险确认复选框 `[auto]`

### 导出格式

- [ ] `images/export/yolo-dir-tree.png` — 解压后 YOLO 单目标导出包目录树（terminal 截图）；红框：labels/ 镜像层级

### 项目 · 工具单位

- [x] `images/projects/tool-units-panel.png` — 项目设置「类别与属性」面板，按工具单位 tab 切换；红框：工具单位切换 tab、某工具下的类别列表、属性 schema 区 `[auto]`

## Batch 2 · 视频/点云 AI 审阅 + 时间轴交互（新增于 2026-07-06） <!-- PR #50 · v0.21.9–17 -->

> 配套视频/点云工作台 AI 审阅体验与时间轴交互。可重现的缩放、刷选、续写与键盘流转已逐步收录到 `[auto-gif]`；需要特殊审阅状态的画面仍保留为手工清单。

### 时间轴交互（对应 `workbench/video-playback.md`）

- [~] ~~`images/video-timeline/horizontal-zoom.gif`~~ — **已由** `public/media/video/video-timeline-zoom.mp4` **替代**：完整时间轴缩放流已迁移到 DocsVideo
- [x] `public/media/video/prediction-density-navigation.mp4` — 五帧真实 YOLO 车辆候选 → 展开 violet AI 预测密度轨 → 连续前后跳预测帧 → 核对帧号与画布候选 `[derived-mp4]`
- [~] ~~`images/video-timeline/brush-create-chapter.gif`~~ — **已由** `public/media/video/video-chapter.mp4` **替代**：章节创建与边界调整完整流已迁移到 DocsVideo
- [~] ~~`images/video-timeline/chapter-resize-hover.gif`~~ — **已由** `public/media/video/video-chapter.mp4` **替代**：章节创建与边界调整完整流已迁移到 DocsVideo

### 视频 AI 审阅（对应 `workbench/video-track.md`）

- [ ] `images/workbench/video-track-candidate-render.png` — 画布渲染检测式轨迹候选 `video_track_bbox`（violet，采纳前逐帧核对态）；红框：候选框 + 单条采纳/拒绝入口 [manual]
- [x] `public/media/video/track-batch-propagate.mp4` — 两条真实 bus 轨迹多选后同时显示画布浮动批量卡、右栏工具条和高亮轨迹框 → 一个真实 SAM3 作业生成双目标候选 → 将审阅窗口设为 F1–F10 保留人工 F0 → 跨帧核对并一次回填两条原轨迹 `[derived-mp4]` **[Tier A]**
- [x] `public/media/video/tracker-cross-frame-points.mp4` — 两个公交车目标分别设置 F0/F4 正点 → 同时追踪 → 时间轴复核 → 接受 `[derived-mp4]`
- [x] `public/media/video/tracker-positive-negative.mp4` — 两个公交车目标分别设置跨帧正负点（左车正点落在车身上下部，避开挡风玻璃）→ 同时追踪 → 时间轴复核 → 接受 `[derived-mp4]`
- [x] `public/media/video/tracker-box-seed.mp4` — 两个公交车目标分别绘制完整车身框 → 同时追踪 → 时间轴复核 → 接受 `[derived-mp4]`
- [ ] `images/workbench/video-track-keyframe-source-bar.png` — 右栏「关键帧来源迷你条」近景（紫=AI / 灰=人工）+ 画布 AI 关键帧角标；红框：迷你条色段、画布角标 [manual]
- [~] ~~`images/workbench/video-track-carryover-ghost.gif`~~ — **已由** `public/media/video/video-track-carryover.mp4` **替代**：跨网格帧续写完整流已迁移到 DocsVideo
- [ ] `images/workbench/video-track-sticky-hint.png` — 「粘轨迹」态画布顶部常驻提示条；红框：提示条 [manual]

### 审阅键盘化（对应 `workbench/index.md` 或 `review/index.md`「视频任务审核」）

- [ ] `images/workbench/review-two-level-cycle.png` — 两级键盘循环示意（建议矢量示意图）：`Tab`/`Shift+Tab` 同类流转，`` ` ``/``Shift+` `` 跨类跳转（AI 待审 → 人工 → 轨迹）[manual]
- [~] ~~`images/workbench/review-auto-advance.gif`~~ — **已由** `public/media/ai/candidate-keyboard-review.mp4` **替代**：完整键盘审阅流已迁移到 DocsVideo

### 两类传播术语（对应 `workbench/video-propagate.md`）

- [x] `public/media/video/propagate-track-vs-copy.mp4` — 同一条中间卡车轨迹先纯几何复制 F0→F30，展示固定框与变大车身的偏离；撤销后运行真实 SAM3 延展，跨帧复核随目标变化的候选并回填 F1–F30 `[derived-mp4]`
- [~] ~~`images/video-propagate/shift-brush-range.gif`~~ — **已由** `public/media/video/tracker-range.mp4` **替代**：完整范围选择流已迁移到 DocsVideo

### 点云文字标签（对应 `workbench/3d-box.md` / `workbench/settings.md`）

- [x] `public/media/pointcloud/billboard-label.mp4` — 标签内容切到“轨迹”段并启用属性 → 标签显示 `object · 车辆 · 清晰可见` → 环绕侧视、俯仰与低角度核对文字持续正对相机 `[derived-mp4]`

### 全局 Pipeline 库（对应新建 `projects/pipeline-library.md` 或 `projects/ai-preannotate.md`）

- [x] `public/media/pipeline/template-create.mp4` — `/ai-pre/pipelines` 命名公共模板 → YOLO 车辆检测 → 车辆属性分类 → 车型/颜色写回 → 保存后进入编排库 **[Tier A]** `[derived-mp4]`
- [x] `public/media/pipeline/apply-project.mp4` — 选择公共两阶段模板 → copy-on-write 套用为项目默认 → 工作台按项目编排运行 → 查看车型与颜色候选 **[Tier A]** `[derived-mp4]`

## Batch 3 · AI 任务入口 / 候选生命周期 / 运营恢复（新增于 2026-07-11）

> 对应本轮 AI 文档分层和实现对齐后的新增缺口。优先表现“从哪里进入、结果落到哪里、失败后去哪里恢复”，避免再补只有静态全页、没有任务上下文的截图。建议先拍 **Tier A**，再补状态型或需要特殊种子数据的 **Tier B**。

### AI 入口与当前题执行

- [x] `public/diagrams/shared/ai/task-entry-map.svg` — AI 五类入口关系图：图片交互式 AI / 当前题 AI / 二次推理 / 批量预标 / 视频 AI 追踪；用箭头标出工作台、`/ai-pre`、模型市场三类页面边界 **[Tier A]** `[excalidraw]`
- [x] `public/media/ai/current-task-image-inference.mp4` — 图片工作台展开当前题 AI 面板 → 运行项目编排 → 同时核对画布与候选列表 → 采纳为正式标注 `[derived-mp4]` **[Tier A]**
- [x] `public/media/ai/current-frame-video-inference.mp4` — 视频工作台定位清晰帧 → 运行当前帧 AI → 核对 violet 候选与置信度 → 采纳后切换相邻帧确认单帧作用域 `[derived-mp4]` **[Tier A]**
- [x] `public/media/ai/secondary-inference-attribute.mp4` — 选中已确认父框 → 展开二次推理面板 → 写回带 AI 溯源的属性 → 人工修正文案并保留其余属性来源 `[derived-mp4]` **[Tier A]**
- [ ] `images/ai/current-task-model-availability.png` — 当前题模型选择器的可用 / 置灰项对照；红框：能力不匹配原因、项目未启用提示、跳转 ML 模型设置入口 **[Tier B]** [manual]

### 图片候选审阅与数据边界

- [x] `public/media/ai/candidate-review-lifecycle.mp4` — 三个 violet 候选与右侧审阅入口同时可见 → 跳过、采纳、驳回 → 焦点自动前进 → 正式 Annotation 与最终候选计数可核对 `[derived-mp4]` **[Tier A]**
- [x] `public/media/ai/candidate-keyboard-review.mp4` — `Tab` 选中候选 → `A` 接受 / `D` 拒绝 → “决策后自动前进”切到下一项的完整键盘流 **[Tier A]** `[derived-mp4]`
- [ ] `images/ai/candidate-source-badges.png` — 候选列表按来源展示 ML Backend / 外部导入 / 交互式结果；红框：来源徽标、模型版本与置信度 **[Tier B]** [manual]

### 后台任务与失败恢复

- [x] `public/media/jobs/jobs-bell-active.mp4` — 顶栏「后台任务」：进行中预标与预测导入进度 / 取消入口 + 已完成双格式导出 ZIP 摘要 / 下载入口 **[Tier A]** `[derived-mp4]`
- [x] `public/media/jobs/video-tracker-job-states.mp4` — `/ai-pre/jobs?tab=video` 视频标签：`running / pending_review / accepted / discarded` 状态对比 → 项目与状态筛选 → 返回对应视频工作台 **[Tier A]** `[derived-mp4]`
- [x] `public/media/workflows/jobs-retry-recovery.mp4` — 失败任务详情、错误摘要、真实重试、新 retry 作业完成与进入工作台审阅 OCR 候选的完整闭环 **[Tier A]** `[derived-mp4]`
- [ ] `images/jobs/notifications-vs-jobs.png` — 「通知中心」与「后台任务」双面板对照图；标注前者负责业务提醒，后者负责长任务进度 / 取消 / 下载，建议拼图 **[Tier B]** [manual]

### 项目、数据与审核入口

- [x] `public/media/projects/project-actions-menu.mp4` — 项目行 `⋮` 菜单：依次定位「导出标注数据」「复制项目配置」「导入预测」三个高频入口，并保留项目语境 **[Tier A]** `[derived-mp4]`
- [x] `public/media/projects/create-existing-resources.mp4` — 新建图片项目 → 配置 `car` 类别 → Step 5 关联已有道路数据集 → Step 6 选择现有标注员与审核员 → 成功页核对摘要 **[Tier A]** `[derived-mp4]`
- [ ] `images/datasets/list-filters-and-repair.png` — 数据集页类型筛选 + 数据集卡操作；红框：图像 / 视频 / 3D / 多模态筛选、缺失视频元数据补生成入口 **[Tier B]** [manual]
- [ ] `images/datasets/prediction-match-preview.png` — 导入预测预览校验：匹配成功、未匹配、同名歧义三类结果；红框：实际匹配键与确认导入按钮 **[Tier B]** [manual]
- [x] `public/media/projects/ml-routing.mp4` — 项目启用 YOLO、即时设为主后端，并在工作台核对当前题 YOLO 与 Smart Point → SAM3 的能力分流 **[Tier A]** `[derived-mp4]`
- [x] `public/media/review/reject-flow.mp4` — 质检审核 landing 的批次卡片（待审 2/2）→ 进入任务列表与审核画布 → 结构化退回 → 返回卡片确认待审降为 1/2 `[derived-mp4]` **[Tier A]**
- [ ] `images/review/bulk-review-toolbar.png` — 选中多条待审任务后的批量操作栏；红框：已选数量、批量通过 / 退回、跨页选择边界 **[Tier B]** [manual]
- [x] `public/media/superadmin/platform-overview.mp4` — 超级管理员「平台概览」：平台统计与项目 / 角色分布 → 注册来源 → ML 成本 → 近期审计活动 → 全平台项目；系统健康保留为独立页面 **[Tier A]** `[derived-mp4]`

## 综合截图清单（按区域维护）

> 新截图统一在这里按区域维护；已废弃截图保留删除原因，避免后续重复补拍。

### 入口

- [x] `images/getting-started/login.png` — 登录页全屏 [auto]
- [x] `images/getting-started/forgot-password.png` — 忘记密码页 + 成功 toast [auto]
- [x] `images/getting-started/e2e.gif` — 登录 → Dashboard → 打开项目 → 标 bbox → 提交并看到下一题 `[auto-gif]`（flows/e2e-quickstart）
- [ ] `images/getting-started/annotator-dashboard.png` — 标注员仪表盘全屏，标注红框：产能/质量分区 + 「打开」按钮 **[Tier B]** 截图 driver 把所有角色 token 收敛为 super_admin，role=annotator 仍渲染 AdminDashboard，需真实标注员 seed token
- [x] `images/getting-started/role-dashboard-overview.png` — 四种角色 Dashboard 拼图 [auto]
- [x] `images/getting-started/platform-nav-overview.png` — 平台主界面侧边栏各分区 [auto]

### 数据集

- [ ] `images/datasets/lidar-axis-wizard-step2.png` — 向导第 2 步选「3D 点云」后出现 AxisConventionPicker + 自动检测按钮 [manual]
- [ ] `images/datasets/lidar-axis-mismatch-banner.png` — 3D 工作台顶部坐标系不一致 banner + 重投影按钮 [manual]

### 项目

- [x] `images/projects/create-entry.png` — 「新建项目」按钮高亮 [auto]
- [x] `images/projects/wizard-steps.png` — 6 步 wizard 关键截图 [auto]
- [x] `images/projects/wizard-step4-backend.png` — wizard 第 4 步 ML Backend 配置 [auto]
- [x] `images/projects/empty-state.png` — 项目列表空状态 [auto]
- [x] `images/projects/error-state.png` — 项目列表错误状态 [auto]
- [x] `images/projects/ai-pre-stepper.png` — AI 预标注步骤条 [auto]
- [x] `images/projects/ai-pre-history-search.png` — 预标注历史搜索 [auto]
- [~] ~~`images/projects/ai-pre-empty-alias.png` — 预标注 alias 为空提示~~ — **已废弃**：PromptComposer（alias 警告所在）在 ai-pre 重构中删除，无对应 UI
- [x] `images/projects/ai-pre-config-panel.png` — 项目详情面板（批次列表 + 配置区 + 跑预标按钮 + 导入预测按钮）`[auto]`
- [x] `public/media/ai/preannotate.mp4` — 选择项目与批次 → 运行 AI 预标 → 查看任务历史 → 返回工作台核对候选 `[derived-mp4]`
- [ ] `images/projects/ai-pre-pipeline-dag-canvas.png` — AI 预标项目详情「批跑预标设置」两列 DAG 编排画布 + 右侧节点参数；红框：节点 +/删除/改父级、兼容性警告、运行计数 [manual]
- [ ] `images/projects/ai-pre-project-pipeline-save.png` — 「保存为项目编排」成功态；红框：保存按钮、已保存编排阶段数 badge、清除按钮 [manual]
- [~] ~~`images/projects/ai-pre-variant-selector.gif`~~ — **已由** `public/media/projects/ai-pre-variant-selector.mp4` **替代**：VariantSelector 完整流已迁移到 DocsVideo
- [x] `images/projects/batch-status-list.png` — 批次列表各状态彩色徽标 `[auto]`（指向 P-0001，有 BT-260/261/262 多批次）
- [~] ~~`images/projects/batch-bulk-actions.gif`~~ — **已由** `public/media/projects/batch-bulk-actions.mp4` **替代**：批次多选批量操作完整流已迁移到 DocsVideo
- [ ] `images/projects/batch-export-dialog.png` — 导出格式选择面板 [manual]
- [x] `images/projects/data-manager-overview.png` — 整体布局（视图列表 + 过滤条件栏 + 任务表格）`[auto]`
- [x] `images/projects/data-manager-filter-rules.png` — 过滤条件行编辑器字段选择器展开 `[auto]`
- [ ] `images/projects/copy-config-banner.png` — 向导顶部预填 banner + 复制指引 checkbox [manual]
- [x] `images/projects/ml-backends/register-form.png` — 注册表单（URL 示例 + 最大并发 + 测试连接）`[auto]`
- [ ] `images/projects/ml-backends/limit-modal.png` — 多后端共存限制弹窗 [manual]
- [x] `images/projects/template-library-overview.png` — 模板库页（管理组入口 + 新建/导出按钮 + 四 tab）`[auto]`（本地无种子模板，呈空态）
- [ ] `images/projects/template-apply-banner.png` — 从模板创建 Wizard 顶部 banner [manual]

### 参考

- [x] `images/export/format-select.png` — 导出对话框 + 格式选项 [auto]
- [~] ~~`images/export/progress.png` — 进度条 + 下载链接 toast~~ — **已废弃**：导出异步化，进度移至右上角任务铃 JobsBell，无独立进度条页（待补 JobsBell 截图）
- [x] `images/notifications/panel-overview.png` — 通知面板展开态（筛选 tab + 分组 + 加载更多）
- [x] `images/settings/profile.png` — 设置 / 个人资料 区
- [x] `images/settings/workbench-prefs.png` — 标注偏好（自动保存周期 + 颜色策略）
- [x] `images/settings/notification-prefs.png` — 通知偏好的 in-app / email 开关矩阵
- [x] `images/settings/system-smtp.png` — 系统设置 / SMTP 配置 + 发送测试邮件按钮（仅 super_admin）
- [x] `images/settings/my-feedback.png` — 我的反馈：用户视角看自己提交的 BUG 工单与状态

### 审核

- [x] `images/review/workbench.png` — 审核三栏全图 + 操作面板 [auto]
- [x] `images/review/reject-form.png` — 退回备注表单 [auto]
- [x] `public/media/review/reject-flow.mp4` — 批次进度 → 审核画布最终 / diff 核对 → 选择结构化原因 → 填写补充说明 → 真实退回与计数更新 `[derived-mp4]`
- [x] `images/review/review-list-page.png` — ReviewPage 左侧批次树 + 任务列表（缩略图 + 批量操作按钮）`[auto]`

### 平台管理

- [x] `images/superadmin/bugs/list.png` — `/bugs` 列表全图，含状态/严重度筛选 + 重开徽标 `↻N`
- [x] `images/superadmin/bugs/detail-panel.png` — 详情面板：元信息行、Markdown 描述、截图附件、状态切换按钮
- [x] `images/superadmin/bugs/status-transitions.png` — 状态切换按钮高亮 + 关闭后再重开的 reopen 徽标 `[auto]`
- [x] `images/superadmin/users/list.png` — `/users` 卡片列表 + 顶部 4 张统计卡 + 角色徽标颜色
- [x] `images/superadmin/users/invite-modal.png` — 邀请用户对话框
- [x] `images/superadmin/users/edit-modal.png` — 编辑用户对话框（角色选择 + 停用按钮）`[auto]`
- [x] `images/superadmin/users/permission-matrix.png` — 权限矩阵预览（PERMISSION_GROUPS × ROLE_PERMISSIONS）`[auto]`
- [x] `images/superadmin/users/groups-tab.png` — 用户组 tab，演示组创建与成员添加 `[auto]`（数据组 tab，本地无组呈空态）
- [ ] `images/superadmin/users/api-keys.png` — API Keys 对话框（明文一次显示）
- [x] `images/superadmin/analytics/overview.png` — 分析页全屏（时间范围下拉 + 4 面板）
- [x] `images/superadmin/analytics/heatmap.png` — 7×24 工时热力图网格
- [x] `images/superadmin/audit-logs/detail-modal.png` — 详情 Modal detail_json + 时间线追溯按钮 `[auto]`
- [x] `images/superadmin/audit-logs/filter-bar.png` — 筛选栏（scope 切换 + detail 键名/键值输入框）
- [ ] `images/superadmin/failed-predictions/list.png` — /ai-pre/jobs?status=failed 列表（状态筛选 + 重试/放弃/显示已放弃 toggle） **[Tier B]** 带 status 时客户端筛选清空 mock，不带又混入真实成功 job，需真实 failed 种子数据；失败列表已由 `images/workflows/failed-prediction-recovery-jobs-list.png` 覆盖
- [ ] `images/superadmin/failed-predictions/dismiss-restore.png` — 显示已放弃后含「已放弃」badge + 恢复按钮 [manual]
- [x] `images/superadmin/ml-backend/register-form.png` — 注册表单全貌含 GPU 物理资源、显存预算、驱逐优先级、当前预算、desired → effective、max_concurrency 与 extra_params **[Tier A]** `[auto]`
- [x] `public/media/superadmin/model-market/gpu-resources.mp4` — 切换 GPU 资源 → 核对 runtime ready / 全局期望模式 → 对比静态预算与运行时 committed → 展开 blocker 卡查看受影响实例 **[Tier A]** `[derived-mp4]`
- [ ] `images/superadmin/ml-backend/health-card.png` — 实时 `/health` 卡片（GPU / video_pool meta）
- [ ] `images/superadmin/ml-backend/health-state-badges.png` — connected/error/disconnected 三状态徽章对比 [manual]
- [x] `images/superadmin/model-market/list.png` — 模型市场 3 个 tab 全图
- [ ] `images/superadmin/model-market/protocol-card-details.png` — 能力目录协议卡复用 ModelCard 的详情态；红框：可接受输入、输出属性/几何、资源/变体、`⚠ 协议` 诊断 badge [manual]
- [~] ~~`images/superadmin/model-market-runtime-card.png` — backend 运行时大卡~~ — **已废弃**：运行时观测已改为服务池摘要卡 → 实例面板 → 详情 Sheet，不再按单 backend 展示大卡
- [x] `public/media/superadmin/model-market/runtime-pools.mp4` — 运行时摘要、完整数据来源、两列服务池、双实例展开与详情 Sheet；使用固定脱敏快照，并保留“暂无路由指标”的真实未知语义 **[Tier A]** `[derived-mp4]`
- [x] `public/media/superadmin/model-market/runtime-data-sources.mp4` — 4/5 来源新鲜 → 展开「数据来源」部分失败态 → 对比 fresh 拓扑与 stale/error 路由账本的更新时间、超时和退避 **[Tier A]** `[derived-mp4]`
- [ ] `images/superadmin/model-market/runtime-instance-detail.png` — 打开实例详情 Sheet；红框：路由状态、并发/延迟未知语义、health/compute、GPU claim、驻留与原始诊断 **[Tier B]** [manual]
- [ ] `images/superadmin/model-market/registry-issue-center.png` — 注册管理「问题中心」；展示按稳定键去重后的主问题、严重度、受影响对象计数与筛选 **[Tier B]** [manual]
- [x] `public/media/superadmin/model-market/video-pool.mp4` — 运行时服务池 → SAM 3 视频追踪实例 → 独立 `_video_pool` 容量 / 活跃会话 / GPU 驻留 → 视频权重与预热入口 `[derived-mp4]`
- [ ] `images/superadmin/public-templates/scope-selector.png` — 可见范围下拉「公共」选项 disabled（非超管视角） [manual]
- [x] `images/superadmin/public-templates/templates-list.png` — 模板库四 tab + scope chip + usage_count `[auto]`（本地无模板呈空态）
- [x] `images/superadmin/system-monitoring/health-panel.png` — 4 组件卡 + Celery 队列表 + Workers 心跳表
- [x] `images/superadmin/system-monitoring/workers-table.png` — Workers 表（名称/Heartbeat/Pool/状态） `[auto]`

### 工作台

- [x] `public/media/workbench/layout-basics.mp4` — 标准布局 → 拖动分隔条 → 讨论与详情合并标签 → 标题栏隐藏 → 菜单找回 → 专注画布及恢复；保持标注选择和未发送草稿 `[derived-mp4]`
- [~] ~~`images/bbox/draw-in-progress.gif`~~ — **已由** `public/media/workbench/bbox-draw.mp4` **替代**：矩形绘制完整流已迁移到 DocsVideo
- [ ] `images/polygon/vertex-edit.png` — 多边形选中态 + 边悬停 + 图标；旧图未展示命名状态且带权限告警，已删除，待建立可验证场景后暗色重拍
- [ ] `images/polygon/close-hint.png` — 三顶点后下一点贴近首点的闭合提示；旧图未展示命名状态且带权限告警，已删除，待建立可验证场景后暗色重拍
- [ ] `images/keypoint/human-pose.png` — COCO 17 点人体姿态 + 骨架连线；旧 1×1 占位图已删除，待重拍
- [ ] `images/keypoint/hand.png` — 21 点手部骨架；旧 1×1 占位图已删除，待重拍
- [x] `images/sam/text-three-modes.png` — 已删除；输出选项现按模型任务和后端能力动态裁剪，不再保留误导性的固定三选参考图
- [ ] `images/3d-box/workbench-overview.png` — 3D 工作台全局（主视图 + 相机面板 + PSR 面板 + 自动贴合按钮组） [manual]
- [ ] `images/3d-box/psr-panel.png` — PSR 面板近景，标注红框：l/w/h 尺寸字段 [manual]
- [ ] `images/3d-box/autofit-buttons.png` — 贴合/收尺寸/贴地/朝向按钮组 [manual]
- [x] `images/workbench/layout-overview.png` — 四区布局全图（顶栏/左工具栏/画布/右抽屉） [auto]
- [x] `public/media/ai/ocr-current-task.mp4` — 真实 RapidOCR 当前题从派发、推理中到生成文本多边形候选 `[derived-mp4]`（P-OCR）
- [x] `images/workbench/ocr-real-scene.png` — OCR 面板无脚本静态备用图 `[auto]`（scene: `workbench/ocr-real-scene`，P-OCR）
- [ ] `images/workbench/task-status-labels.png` — 六种状态标签竖列

#### 超大图片

> 固定项目/数据集为 `P-LARGE-IMG` / `DS-LARGE-IMG`。它在截图数据库中存在时会作为可选项目加入 catalog；录制前必须确认 Cosmic Cliffs 金字塔已 ready，不得用整图加载冒充渐进 LOD。

- [x] `public/media/large-image/progressive.mp4` — NASA Cosmic Cliffs 从 overview 到目标 LOD 的渐进清晰过程，包含平移、连续缩放和 edge 区域且无白缝/闪烁 **[Tier A]** `[derived-mp4]`
- [x] `public/media/large-image/pyramid-recovery.mp4` — 一个当前视口高清切片真实返回 503 后保留 overview、自动重签恢复，并继续缩放和平移 **[Tier A]** `[derived-mp4]`
- [x] `public/media/large-image/mask-limit.mp4` — Cosmic Cliffs 分块浏览 → 完整落库矩形框 → 悬停置灰 Mask 入口核对实际尺寸、单边上限与总像素上限 **[Tier A]** `[derived-mp4]`

- [x] `images/mask-brush/toolbar-overview.png` — Mask 笔刷浮动工具栏全貌（笔刷图标、笔刷/橡皮按钮与半径 slider；按实际可访问标签标注） [auto]
- [~] ~~`images/mask-brush/draw-in-progress.gif`~~ — **已由** `public/media/mask-brush/mask-draw.mp4` **替代**：Mask 绘制与提交完整流已迁移到 DocsVideo
- [x] `public/media/video/mask-track-edit.mp4` — 点击「Mask 轨迹」从空白创建 Mask → `Enter` 生成首个关键帧 → 跳到保持帧编辑同一轨迹 → 笔刷修正 → `Enter` 物化第二个人工关键帧 **[Tier A]** `[derived-mp4]`
- [x] `public/media/video/mask-correction-propagate.mp4` — 在 F5 漂移帧补入漏分区域、扣除外溢区域 → 使用原生 Mask seed 向后传播 → 跨帧审阅 → 采纳后原轨迹保留 17 个关键帧 **[Tier A]** `[derived-mp4]`
- [ ] `images/pointcloud-crossframe/crossframe-propagate-toast.png` — 按 Alt+→ 跳帧自动选中新框 + toast [manual]
- [ ] `images/pointcloud-crossframe/overlay-k3-triview.png` — K=3 时主视图 + 三视图半透明虚线参考框 [manual]
- [ ] `images/workbench-pointcloud-projection/overlay-wireframe.png` — 相机面板线框投影 overlay + 「正对」角标 [manual]
- [ ] `images/workbench-pointcloud-projection/click-to-select-3d.png` — 点击投影框联动主视图高亮 [manual]
- [x] `images/workbench/pointcloud-real-scene.png` — nuScenes 激光雷达 + 6 相机环绕布局全景 `[auto]`（scene: `workbench/pointcloud-real-scene`，P-PC-MULTI）
- [x] `public/media/pointcloud/orbit.mp4` — 点云视图导航：保留两侧面板，左键拖拽 orbit 环绕 + 滚轮缩放并复位视角 `[derived-mp4]`（P-PC-DEV）
- [x] `public/media/pointcloud/controls.mp4` — 工作台设置窗口点云控件演示（相机上色 / 点大小 / 深度提示逐项切换）`[derived-mp4]`（P-PC-DEV）
- [x] `public/media/pointcloud/panel-layout.mp4` — 选中真实 3D 框后整体停靠 / 浮动 / 隐藏恢复三视图，整组切换六路相机并切帧核对布局与渲染 `[derived-mp4]`（P-PC-DEV）
- [x] `public/media/pointcloud/crossframe-track.mp4` — nuScenes 3D 框跨帧延续、中心修正和邻帧轨迹参考 `[derived-mp4]`（P-PC-DEV）
- [x] `public/media/pointcloud/billboard-label.mp4` — 轨迹标签启用属性后多角度核对 billboard 文本持续正对相机 `[derived-mp4]`（P-PC-DEV）
- [x] `images/workbench/pointcloud-rgb-colorize.png` — 相机上色前后对比（同上 `public/media/pointcloud/controls.mp4` 内含青蓝高度色→相机 RGB 的切换）`[derived-mp4]`
- [ ] `images/workbench/pointcloud-depth-heatmap.png` — 深度热力图 + figcaption 深度读数（控件 GIF 已演示开关，相机视图悬停深度读数特写仍 [manual]） [manual]
- [~] ~~`images/polygon/draw-in-progress.gif`~~ — **已由** `public/media/polygon/draw.mp4` **替代**：多边形绘制与提交完整流已迁移到 DocsVideo
- [ ] `images/polygon/vertex-insert-alt.png` — 按住 Alt 悬停边上光标变「+」的瞬间
- [~] ~~`images/polyline/draw-in-progress.gif`~~ — **已由** `public/media/polyline/draw.mp4` **替代**：折线绘制与提交完整流已迁移到 DocsVideo
- [ ] `images/polyline/vertex-edit.png` — 折线选中态 Alt 插入/Shift 删除提示
- [~] ~~`images/workbench/rotated-bbox.gif`~~ — **已由** `public/media/workbench/rotated-bbox.mp4` **替代**：旋转框绘制完整流已迁移到 DocsVideo
- [ ] `images/workbench/rotated-bbox-rotate.png` — 旋转约 30° 后状态 + 角度值
- [x] `images/sam/smart-point-toolbar.png` — 智能点交互工具条（正负极性 + 引擎/档位 + 状态灯）`[auto]`（scene: `sam/smart-point-toolbar`，P-COCO8 + live backend）
- [x] `images/sam/interactive-toolbar.png` — 智能框交互工具条（引擎/模型/档位 + 状态灯）`[auto]`（scene: `sam/interactive-toolbar`，P-COCO8 + live backend）
- [x] `images/sam/magic-box-toolbar.png` — Magic Box 交互工具条（紧凑 bbox 输出提示 + 引擎/档位 + 状态灯）`[auto]`（scene: `sam/magic-box-toolbar`，P-COCO8 + live backend）
- [x] `images/sam/exemplar-output-mode.png` — Exemplar 示例交互工具条（输出形态三选一 + 示例能力控件）`[auto]`（scene: `sam/exemplar-output-mode`，P-COCO8 + live backend）
- [~] ~~`images/sam/smart-point-interaction.gif`~~ — **已由** `public/media/sam/smart-point.mp4` **替代**：智能点真实推理完整流已迁移到 DocsVideo
- [~] ~~`images/sam/smart-box-interaction.gif`~~ — **已由** `public/media/sam/smart-box.mp4` **替代**：智能框真实推理完整流已迁移到 DocsVideo
- [~] ~~`images/sam/smart-scribble-interaction.gif`~~ — **已由** `public/media/sam/smart-scribble.mp4` **替代**：正负笔迹精修完整流已迁移到 DocsVideo
- [x] `public/media/ai/assisted-annotation.mp4` — 无侧边栏的真实 SAM3 Magic Box 粗框、候选收紧与类别确认 `[derived-mp4]`（P-COCO8）
- [~] ~~`images/sam/exemplar-interaction.gif`~~ — **已由** `public/media/sam/exemplar.mp4` **替代**：Exemplar 示例推理完整流已迁移到 DocsVideo
- [ ] `images/sam/exemplar-yoloe-toolbar.png` — YOLOE exemplar 交互工具栏能力裁剪态；红框：仅正样例、无负框/叠加文本、输出形态 [manual]
- [ ] `images/workbench/current-task-project-pipeline.png` — 工作台「当前题 AI」面板按项目编排运行入口；红框：运行当前题（按项目编排 · N 阶段）按钮、项目编排来源提示 [manual]
- [ ] `images/video-playback/sampling-config.png` — 项目设置帧采样配置区（mode/target_fps/frame_step） [manual]
- [ ] `images/video-playback/chapter-sidebar.png` — 章节侧栏含彩色色带 + 章节列表 [manual]
- [x] `public/media/video/ai-tracker-panel.mp4` — 自由布局中的浮动、停靠、隐藏恢复与 AI 标签合并，已接入工作台布局指南 `[derived-mp4]`
- [ ] `images/video-propagate/tracker-job-badge.png` — 进度 badge + 取消按钮 [manual]
- [x] `public/media/video/workbench-overview.mp4` — 视频工作台整体（双向逐帧 + 播放暂停 + 手动建轨迹并刷新核对）`[derived-mp4]`（开源 P-VIDEO-DEV，seed_video.py）
- [~] ~~`images/workbench/video-track-trajectory.gif`~~ — **已由** `public/media/video/video-draw.mp4` **替代**：轨迹关键帧与插值完整流已迁移到 DocsVideo
- [x] `images/workbench/video-real-scene.png` — 真实城市交通视频任务工作台 `[auto]`（scene: `workbench/video-real-scene`，P-VIDEO-DEV）
- [x] `images/workbench/video-track-timeline.png` — 视频轨道时间轴 + 关键帧 + 软网格（同上 `video-track-trajectory.gif` 画关键帧时时间轴同步呈现）`[auto-gif]`
- [ ] `images/workbench/video-track-compose-dialog.png` — 跳连对话框两种 gap 模式 [manual]
- [ ] `images/workbench/video-track-qc-warnings.png` — 画布左上角质量提示浮层 [manual]

### 工作流

- [x] 复用 `images/projects/ai-pre-config-panel.png` — ProjectDetailPanel（批次勾选 + predict_mode 三 tab + 跑预标按钮） [auto]
- [x] `images/workflows/failed-prediction-recovery-jobs-list.png` — /ai-pre/jobs?status=failed 列表 [auto]
- [x] 复用 `images/projects/wizard-steps.png` — 向导类型选择（7 种项目类型卡） [auto]
- [ ] `images/workflows/batch-assign-dialog.png` — 批次分配对话框（标注员/审核员选择）

### Dev / 协议图

> 这些是开发文档里的架构图候选，建议用 mermaid 而非真截图（直接写入对应 .md 文件）。

- [x] `dev/reference/video-frame-service.md` § Chunk 状态机：补 ready/pending/failed 三态图，含 smart_copy / transcode 注释
- [x] `dev/reference/video-frame-service.md` § Tracker job 事件序列：补 sequenceDiagram，含 FE / API / worker / ML / Redis Pub-Sub 五道
- [x] `dev/reference/ws-protocol.md` § 6.2 事件序列：补 stateDiagram-v2 可视化 tracker 事件流
- [x] `dev/concepts/state-machines.md` § Task 状态机：共享 Task canonical 已含 `rejected` 与批次级回退路径

## 新增图片时

1. 把图保存到 `docs-site/user-guide/images/<page>/<name>.png`
2. 在上表新增一行并勾选
3. 如果是可自动化的场景，在 `apps/web/e2e/screenshots/scenes.ts` 添加 scene，并标注 `[auto]`
4. `pnpm docs:build` 验证 dead-link 检查通过
