# G2 · 视频 Issue 完整上下文恢复（计划草案）

> Status: Done
>
> 创建日期：2026-09-07；代码核验基线：80a0c0e2。
>
> 所属 Epic：[图片与视频工作台交互改进](1788766038_image-video-workbench-interaction-epic.md)。
>
> 执行授权：2026-09-07 用户已要求按草案逐步实施，实测后提交；实施状态：已完成；浏览器验收：2026-09-08 通过，见 Outcome。

## 1. 交付范围与依赖

交付结果：视频 Issue 完整上下文恢复。扩展现有 JSONB，无新增列/迁移。接口输入校验、序列化、OpenAPI和 SDK 反馈合同需同提交更新。

硬依赖：[G1](1788769060_workbench-g1-video-issue-frame.md)

## 2. 设计与实现合同

保留 `anchor_type=pixel`、顶层 `task_id/annotation_id` 和 `anchor_position.x/y/frame`，在 `anchor_position` 中新增可选 `video_context`。现有 JSONB 与 pixel CHECK 允许此扩展，**不新增列、不要求历史回填、不需要数据库迁移**。

写入时仅允许视频任务的 pixel 锚携带 `video_context`，且必须同时提供旧 `frame`。新嵌套模型拒绝未知字段和非有限数；任务媒体类型由反馈 service 校验，锚点 JSON 由 `schemas/annotation_feedback.py::serialize_feedback_anchor` 编码，应用反馈命令负责创建。旧锚的输入和读取保持兼容。

| 字段                              | 明确语义                                                                    |
| --------------------------------- | --------------------------------------------------------------------------- |
| `schema_version`                  | 本合同为 1；读端遇到未知版本只使用旧帧/像素锚，不推断其内部结构             |
| `track_id`                        | 可选字符串，非 UUID；作为对象缺失后的软定位信息                             |
| `annotation_version`              | 可选 ≥1 整数；输入时要求顶层有 annotation_id；读取允许原对象已被删除        |
| `frame_range.from_frame/to_frame` | 可选，从 0 起的源帧整数闭区间，包含 anchor frame；与采样网格无关            |
| `viewport.center_x/center_y`      | 视频坐标归一化的视口中心；允许平移到图像外，但必须有限                      |
| `viewport.zoom`                   | 正且有限的 `scale / fitScale`；恢复时按新容器尺寸计算，不持久化屏幕像素平移 |
| `timeline_window.from/to`         | 沿用现有 `TimelineWindow` 的源帧窗口，允许分数帧；不重复保存缩放倍数        |

像素锚 x/y 仍限制在 `[0,1]`。范围与窗口在服务端校验顺序和视频边界；恢复时按当前媒体长度及既有窗口/缩放规则夹取，并对媒体变化给出提示。已有 `region_bbox` 可存紧凑几何快照；保留既有 Mask `compare_locator`，本轮不新增通用候选 locator。

**恢复顺序与失败行为**

1. Issue 点击事件携带目标 task 与锚点交给现有导航所有者，不能假设当前任务查询里已有目标 Issue。
2. 通过 `selectTask`、latest navigation scheduler 和未保存草稿保护进入目标任务，等待目标标注、媒体尺寸及首次 fit 就绪。
3. 确认 `seekToFrameReady` 返回 ready，随后恢复对象选择、视频 viewport、timeline window；通过 `VideoStageControls` 与 Overlay 的有限 capture/restore 接口调用各自现有所有者。恢复请求存活期间抑制由此次对象选择触发的普通自动聚焦 effect，最终只应用一次 Issue viewport，完成/失败/取消均释放该请求的抑制标记；不能依赖延时覆盖争抢。
4. 每次等待后检查导航代次与 task；连续点两个 Issue 时仅最后请求生效，用户主动导航取消旧恢复。
5. 对象已删或版本变化时，仍恢复帧和像素/region 锚并提示“对象已变化”；缺少新字段的旧 Issue 使用旧定位。无权限时保留当前位置并给出权限错误；解码失败提供重试，不宣称现场已恢复。

**验收**：10 个单帧问题和范围问题可直接定位；F120–F160 用一条范围 Issue 表达；刷新、不同画布尺寸、outside 对象、对象删除、采样网格、跨任务、快速连续点击、媒体失败与草稿保护均覆盖。

主要路径：`api/feedbacks.ts`、`shell/IssueCreateModal.tsx`、`state/useIssuePins.ts`、`state/useActiveIssueStore.ts`、`shell/WorkbenchStageHost.tsx`、`stage/VideoKonvaStage.tsx`、`stage/videoStageControls.ts`、`stage/useVideoPlaybackController.ts`、`stage/useFrameClock.ts`、`stage/VideoPlaybackOverlay.tsx`，以及 API feedback schema/service/序列化路由。同步 OpenAPI、生成客户端和 SDK 受影响的反馈合同；没有新外部账号或服务依赖。

## 3. 主要实现位置

- [apps/web/src/pages/Workbench/shell/IssueCreateModal.tsx](../../../apps/web/src/pages/Workbench/shell/IssueCreateModal.tsx)
- [apps/web/src/pages/Workbench/state/useIssuePins.ts](../../../apps/web/src/pages/Workbench/state/useIssuePins.ts)
- [apps/web/src/pages/Workbench/state/useActiveIssueStore.ts](../../../apps/web/src/pages/Workbench/state/useActiveIssueStore.ts)
- [apps/web/src/pages/Workbench/stage/VideoKonvaStage.tsx](../../../apps/web/src/pages/Workbench/stage/VideoKonvaStage.tsx)
- [apps/web/src/pages/Workbench/stage/videoStageControls.ts](../../../apps/web/src/pages/Workbench/stage/videoStageControls.ts)
- [apps/web/src/pages/Workbench/stage/VideoPlaybackOverlay.tsx](../../../apps/web/src/pages/Workbench/stage/VideoPlaybackOverlay.tsx)

新增内部模块只服务本步职责；不得复制完整 Workbench/Mask/SAM/tracker 状态树。公共边界、失败语义和回滚约束沿用本文件设计及 Epic 跨步骤不变量。

真实帧夹具复用 `video-webcodecs-precise-frame.spec.ts` 的 seed.videoWebCodecs 和帧标记。原 review-feedback-loop 只验证任务退回，不能证明视频图钉定位。

## 4. 浏览器实测验收

每个里程碑必须同时具备真实浏览器交互与持久结果证据，不能用单测、截图存在或 API 成功替代整条用户路径。使用当前 worktree 的 Web/API 和经验证的一次性测试数据库；记录 URL、实际代码目录/提交、浏览器、视口、测试任务与逐项结果。正常路径不拦截业务 API；确定性 ML 夹具或人为注入的失败必须明确标注，不能称为真实模型质量验证。

按用户已确认的 Playwright Chromium 方案，通过真实点击、按键和拖动执行下表，检查相关控制台与 API 错误。新增自动化场景使用真实 Chromium 与 API，禁止用 DOM dispatchEvent 替代用户输入。涉及保存的步骤刷新后读回；涉及性能的步骤对同一数据做前后比较。

本步使用当前 worktree 的 3010 Web / 8011 API，并核对实际进程目录；3000/8000 主工作区服务与 3001 Grafana 未参与验收。浏览器、任务和清理结果见 Outcome，完整隔离约定见 Epic 的“浏览器实测环境”段。

| 编号 | 操作与通过条件                                                                                                    | 当前结果 |
| ---- | ----------------------------------------------------------------------------------------------------------------- | -------- |
| G2-1 | 选择轨迹、缩放/平移画布并缩放时间窗，创建 Issue；改变所有状态后点击，恢复 task/frame/object/viewport/window。     | 通过     |
| G2-2 | 创建 F120–F160 范围 Issue，只需一条记录；刷新与改变浏览器容器尺寸后仍能恢复归一化视口和源帧范围。                 | 通过     |
| G2-3 | 修改或删除被锚对象再点击 Issue，仍落到帧和像素/region 锚，提示对象已变化；旧 Issue 和未知版本上下文使用兼容定位。 | 通过     |
| G2-4 | 跨任务连续点击两个 Issue，并在导航期间存在草稿；保护生效，旧导航取消，选择聚焦不会覆盖最终 Issue viewport。       | 通过     |
| G2-5 | 无权限、帧解码失败、上下文字段非法时，分别核对权限/重试/验证错误，不宣称完整恢复。                                | 通过     |

## 5. 自动化检查与文档

下面列的是仓库标准命令；本机执行 E2E 时按 Epic 环境约定附加临时端口覆盖配置，不直接占用 Grafana 的 3001。只运行本步相关套件；Mask native 场景使用 `PLAYWRIGHT_RASTER_MASK_MATRIX=native`，按既有配置启用原生写入。

新增功能 spec（实施时创建）：`apps/web/e2e/tests/video-issue-context.spec.ts`。现有 spec 只提供夹具和相邻回归，不证明本步新行为已覆盖。

后端定向回归：在 apps/api 的本地虚拟环境与已核验测试库运行 `uv run pytest tests/test_annotation_feedbacks.py`。

```bash
pnpm --filter @anno/web test src/pages/Workbench/state/useIssuePins.test.tsx src/pages/Workbench/stage/useVideoPlaybackController.test.ts
PLAYWRIGHT_RASTER_MASK_MATRIX=native pnpm --filter @anno/web test:e2e e2e/tests/video-webcodecs-precise-frame.spec.ts --project=chromium
PLAYWRIGHT_RASTER_MASK_MATRIX=native pnpm --filter @anno/web test:e2e e2e/tests/video-issue-context.spec.ts --project=chromium
pnpm openapi:export
pnpm codegen
pnpm openapi:check
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
git diff --check
```

文档同步：对应用户指南、开发者合同（如改变）和 CHANGELOG Unreleased；具体路径沿用 Epic 对应步骤的文档表。

## 6. 执行、记录与回滚

- 用户于 2026-09-07 更新执行要求：独立草案形成后按序实施，每个里程碑浏览器实测通过后提交。本草案已包含在该执行授权中。
- 按依赖核对当前代码与已交付合同，实施本文件范围，保持原有状态所有者、任务锁与异步代次保护。
- 运行定向回归、浏览器验收和受影响文档检查，修复发现的问题，再复核最终 diff。
- 浏览器所有必测项通过后才将本里程碑标为完成；失败或未测明确保留为未完成，不以其它检查代替。
- 追加 `## Outcome`，记录实际改动、文档位置、测试结果、浏览器逐项结果与证据、清理情况；只有实际存在才记录提交号。更新 Epic 状态索引。
- 每次测试后清理本次中间产物和测试数据；保留明确交付的最小证据。停止本任务启动的进程，恢复浏览器缩放，清理临时配置，不修改共享 .env/依赖或停止主工作区服务。
- 本里程碑验收并提交后，按依赖继续下一份草案。版本与发布日期仍由维护者决定，本轮不作版本发布。

回滚：按本里程碑回退实现；保留已保存的标准标注/反馈数据及后续客户端可选读取字段，不清空用户数据。

## Outcome

提交：`ed5a1c81`（`feat(workbench): persist and restore video issue context`）。

2026-09-08 完成 G2。视频问题在既有 JSONB 中保存可选的上下文：对象与捕获版本、源帧范围、归一化视口和时间窗。创建时冻结快照，F120–F160 只需一条记录；任务级和图片锚保留原合同。公开 API 严格验证字段、归属和媒体边界，读端保留未知历史版本；原问题的回复可继承原锚。OpenAPI、前端类型、Python SDK 的反馈创建/分页入口及覆盖清单同步，无数据库迁移或版本递增。

项目范围的问题列表使用完整目标载荷。恢复先预检目标权限，再经现有任务调度器和普通绘制 / Mask 草稿保护，等待目标标注、首次适配与实际源帧。恢复租约抑制本次选择引起的自动聚焦，沿用原有视图和时间窗所有者；提交完成后才报告恢复成功。真正用户操作取消旧意图，任务初始化的清空选择有明确来源，不误取消跨任务恢复。对象变化、历史未知上下文和媒体边界变化分别降级或提示；失败保留重试目标。

### 浏览器证据

最终完整批次于 10:25–10:27（Asia/Shanghai）通过 7 个场景，0 skipped、0 flaky、0 unexpected。基线为 `80a0c0e2` 加本次源码；运行时核对 Web / API 分别来自当前 checkout 的 `apps/web` / `apps/api`。地址为 `http://127.0.0.1:3010` / `http://127.0.0.1:8011`，任务路由为 `/projects/{project_id}/annotate?task={task_id}`。

浏览器 Chromium `147.0.7727.15`，视口 1440×1000，resize 路径为 1180×820，DPR 1。主机为 Precision 7920 / Xeon Gold 6238R / 双 RTX 3090；CDP 实际报告软件图形与视频解码路径，本结果不认证硬件解码性能。新增 180 帧、160×120 的真实 H.264 夹具使用中央 8 位明暗标记区分全部源帧，FFmpeg/ffprobe 与 PNG 读回核验；浏览器从实际媒体变换映射采样区域，核对 WebCodecs 和原生位图的真实像素。

| 场景           | 真实操作与持久结果                                                                                                                                   | 主测试任务 ID                          |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| 十条问题与范围 | 表单保存 F3/17/33/63/93/120/129/143/159/173；F120–160 保持一条记录，刷新后逐项定位，真实像素一致，记录总数仍为 10，没有标注写入。                    | `67459b31-fd1a-43f1-8555-f2b5fbdf3330` |
| 对象与视图     | 保存 outside 对象及版本，改选另一个对象、平移/缩放后恢复；刷新和 resize 后仍恢复同一对象、归一化视口与分数时间窗，后续精确换帧不被普通自动聚焦覆盖。 | `178afdaf-d200-4f0e-b139-83022cd0459a` |
| 对象改版/删除  | 正式 API 修改对象版本、刷新定位，再删除对象并刷新定位；始终保留 F129 原像素区域与原问题记录，显示“对象已变化”。                                      | `fd2c3d07-8db6-4eb5-848d-9d1d1375cee5` |
| 历史和非法输入 | 旧锚与测试历史版本 999 按旧帧定位且保留现视图；8 类非法上下文经公开 API 返回 422，不产生额外记录。                                                   | `edd984d1-c67e-4550-bd17-5e074fddd565` |
| 跨任务草稿     | 两点多边形阻止离开；继续绘制后保留原任务、F5、视图和两点，再补第三点真实保存；随后定位目标任务 F143 并恢复其视图/窗口。                              | `271186e4-abb5-46f9-9976-4cf0165d02f8` |
| 最后点击/重试  | 明确延迟实际 MP4 响应，跨任务 B→A 连点后只有 A/F33 发布 ready；另一轮等待超时后放行并重试，恢复 B/F143 及其上下文。                                  | `f177f8a0-b725-4874-840e-d25dd07cdd25` |
| 权限撤销       | 管理 API 撤销目标批次分配，标注员点击此前可见的项目卡片，正式任务 API 返回 404；原任务、F35、视口/窗口保持，刷新后的列表不再泄露目标问题。           | `73b1652c-e172-4f43-a68d-5aea6b2ca48f` |

第一条项目 ID 为 `629927f2-5fc4-4dca-9a8a-976e5d863944`。视图用例保存并恢复 `center=(0.4791833985336408, 0.4932036235826489), zoom=1.2100000000000002`，时间窗为 `[35.800000000000004,143.2]`；另从实际 Konva 媒体几何独立计算相同比值，未只检查状态属性。outside 对象由原始标注集合恢复；切换列表为“全部”后核对选择，未改变现有“当前帧”过滤语义。

草稿用例确认弹窗完全关闭、指针恢复后再续画，验证点数从 2 到 3。按现有平移暂停规则，开始草稿前采样步长 5 已使源帧从 F3 到 F5；验收核对实际起草帧，不把它误报为 F3。权限场景同理核对实际 F35。G2 问题落点本身仍在非采样网格帧保存和精确定位。

所有用例未预期的控制台、HTTP 和请求错误为 0。只单独记录明确的媒体延迟、任务切换/登录换页取消的读取、心跳及释放锁，以及真实权限撤销对应的 404；反馈和标注业务写入未拦截、错误未忽略。历史未知版本仅由测试数据接口写入，公开写入校验保持严格。早期种子测试与浏览器共享媒体清理曾互相干扰，该轮不计入验收；后续测试和清理均串行执行。

### 自动化、文档与清理

- 11 个前端相关文件累计 276 个用例通过；最初 274 个，补上生命周期清空选择与旧锚媒体缩短回归后复验受影响文件 118 个通过。完整 Web TypeScript、ESLint、CSS token 检查通过。
- 专属一次性 PostgreSQL 测试库的反馈/schema/service/API/Mask 兼容回归 88 个通过；真实编码与种子路由 49 个通过。SDK 反馈与独立 OpenAPI 合同 34 个通过。
- 相邻精确视频的 9 个浏览器场景通过：开关边界、baseline、B 帧重排、GOP、VFR、pending→ready、unsupported 和 malformed 回退。未将单测当作真实帧证据。
- OpenAPI 导出、生成客户端和同步检查通过；计划 freshness、文档 codegen 同步通过。README、用户审核指南、开发审核合同、API 使用示例、SDK README 和 CHANGELOG 同步；没有发布或远端 CI 声明。
- 测试种子逐例清理；最终数据库 `annotation_workbench_0dc6_e2e` 核对所有者与零连接后删除。Web/API 随验收结束退出，测试 Redis 与临时配置、报告、缓存和调试文件清理；共享 `.env`、Node 依赖与主工作区服务保持原状。最小验收证据保留在本 Outcome。
- 本里程碑通过验收后提交；实际提交记录以 Git 为准，随后继续 H1。
