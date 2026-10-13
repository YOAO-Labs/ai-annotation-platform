# 仓库优化 P5：工作台职责收敛与领域 hook 归属

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P5 工作包，§4.2 / §4.4）
> 基线提交：`af3afdfb5`（P4 后端归属已合入点，等于本工作树起点）
> 输入：`docs/research/26`（不变量映射）、`27`（台账）、`30`（P3 前端测试）、`32`（P4 后端归属）
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[GAP]** 未执行或留待后续阶段
> 并行边界：不改 E2E 与后端（P7）、非 Workbench 页面（P6）、`docs/research/26`/`27`/README；`stage/shared/geometry/maskOperations.test.ts` 与 `maskRle.test.ts` 归 P7，未触碰。

## 0. 结论

1. 装配 model `useWorkbenchShellModel.tsx` 从 8919 行收敛到 6647 行（−2272 行，−25%），八条职责离开装配层：六条业务规则进入同目录领域模块/纯模块，两组局部视图（Mask 确认弹窗簇、选中浮卡内容分派）迁入 shell 组件，每条规则单一归属、调用点逐个更新（§2）[V]。
2. 任务导航准入收敛为 `state/taskNavigation.ts`：latest-wins 调度器、离开守卫执行器、提交闸门、本地 URL 同步判定与冷却常量单一归属；此前散落在 `useWorkbenchShellModel.helpers.ts` 万能杂文件与 model 两处 [V]。
3. 原生 Mask 全部变更工作流（实例原子操作、视频剪贴板、关键帧单飞操作、幂等键、单飞提交、迟到刷新拒绝、破坏性确认）收敛为 `state/useMaskMutationWorkflows.ts`（约 1500 行领域 hook，参数按 Pick/结构化 ref 收窄）；错误策略纯化到 `state/maskMutationPolicy.ts` [V]。
4. 视频 Mask 纠错对话框状态机与 `commitVideoMask` 写归属守卫收敛为 `state/useVideoMaskCorrection.ts`；纠错时序继续复用纯模块 `videoMaskCorrectionFlow.ts` [V]。
5. PVS 种子采集状态机 + 传播对话框生命周期收敛为 `state/useTrackerSeedCollection.ts`；种子→prompts 分组（obj→frame、xyxy→wh、确定性排序）进入纯模块 `state/trackerSeedPrompts.ts` 并配正反测试 [V]。
6. 批量线 AI 后端选择（手动粘滞/项目默认跟随/首位回落）收敛为 `state/useBatchBackendSelection.ts`，粘滞规则两侧均有测试钉住 [V]。
7. 每次抽取都做了与原实现的逐行等价核对：函数体字节级一致，仅有意的差异（依赖数组补充稳定 ref、`videoSegments` 参数化、纯模块替换内联分组、§7.1 版本叙事注释删除）。C5 等价核对发现并修复了一处真实遗漏——`openPropagateDialog` 原有 `setPropagateBrush(null)` 刷选范围清空，改为向章节域注入 `clearPropagateBrush` 窄命令后恢复 [V]。
8. 全量前端测试 560 文件 / 5605 用例通过；`tsc --noEmit` 与 `eslint`（state 目录零告警）通过；`git diff --check` 干净 [V]。

## 1. 范围与边界

- 只做 P5 / §4.2 / §4.4：不改 API 路径、协议、数据库模型与产品语义；不改 E2E、后端、非 Workbench 页面。
- 领域 hook 的依赖契约按「实际消费的字段/命令」收窄（`Pick<WorkbenchState, …>`、结构化 ref 类型、`Pick<ReturnType<typeof useVideoTrackerJobs>, …>`），不制造第二套状态；`history` 只要求 `push`。
- 面板显隐（workspaceCommands）、章节圈选（chapterDraftArmed/propagateBrush）、临时工具准入（useVideoToolCommands）仍归原领域，向采集 hook 注入窄命令/引用，不复制所有权。

## 2. 领域迁移图

| 提交        | 领域                                   | 新归属模块                                                                | 迁出行数（约） | 测试                                                                                                  |
| ----------- | -------------------------------------- | ------------------------------------------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------- |
| `b7294c7ad` | Mask 变更错误策略（纯）                | `state/maskMutationPolicy.ts`                                             | 77             | `maskMutationPolicy.test.ts`（8 例：reason×状态码判定矩阵、文案回落）                                 |
| `9705db587` | 任务导航准入                           | `state/taskNavigation.ts`                                                 | 141            | `taskNavigation.test.ts`（15 例，自 helpers 测试原样迁入）                                            |
| `e2beceff8` | 原生 Mask 变更工作流                   | `state/useMaskMutationWorkflows.ts`                                       | 1405           | 新增 `useMaskMutationWorkflows.test.ts`（4 例：草稿发布、冲突失败保留草稿、迟到刷新拒绝、本地锁判定） |
| `3d2c59a83` | 视频 Mask 纠错 + 保存 owner            | `state/useVideoMaskCorrection.ts`                                         | 304            | 既有对话框/纠错链路测试继续覆盖                                                                       |
| `a84c0ff68` | PVS 种子采集 + 传播对话框              | `state/useTrackerSeedCollection.ts` + `state/trackerSeedPrompts.ts`（纯） | 247            | `trackerSeedPrompts.test.ts`（4 例：分组、同帧点+框、bbox-only、空集）                                |
| `49d15616f` | 批量线后端选择                         | `state/useBatchBackendSelection.ts`                                       | 22             | `useBatchBackendSelection.test.ts`（4 例：初始化、手动粘滞、切项目重置、对象解析）                    |
| `bcfc461fc` | Mask 本地确认弹窗簇                    | `shell/MaskConfirmDialogs.tsx`                                            | 83             | 既有 shell 测试 542 用例通过；标记与 handler 原样迁移                                                 |
| `0ce27419c` | 选中浮卡内容分派（10 分支 + 本地判定） | `shell/SelectionCardContent.tsx`                                          | 286            | 归一化 diff 逐分支核对；全量 Workbench 3720 用例通过                                                  |

model 余量 6647 行的构成：URL/取数/权限装配、会话键派生（消费路由参数与选中/帧/工具等装配层输入）、视图槽位 props 接线（`WorkbenchLayout` 各 slot、对话框 props）与 result 对象——均为 §4.1 保留给装配层的职责。

### 2.1 `renderVideoTrackSidebar` 保留装配层的归属证据（C7 评审结论）

该回调是对 `<VideoTrackSidebar/>` 的单点实例化：35 个 props 全部是装配层已算好的值（可见标注、选择、帧、锁/显隐集合、各类领域命令），回调体内**没有任何分支判定或规则**，仅存在一处既有 cast（roster 批量源列表）。JSX 本体在 `stage/VideoTrackSidebar.tsx` 组件内；抽一层「props 转发组件」只会在 model 与组件之间增加无信息量的转发体（§4.2「明确不做」），因此判定为装配接线而非领域逻辑，保留在 model [V]。

## 3. 异步归属清单（§4.4 末段五问）

| 领域              | owner                                              | cancel                                        | stale 拒绝                                                                         | 草稿保留                                  | 清理                                                      |
| ----------------- | -------------------------------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------- | --------------------------------------------------------- |
| Mask 实例原子操作 | 预览时快照 scope+版本，提交只用快照                | 新预览重置幂等键；transition/commit ref 单飞  | 刷新 token + 会话上下文（task/frame/tool/selection/generation）逐 await 断言       | 失败保留 Buffer 与草稿（「草稿已保留」）  | 预览消失 effect 清草稿；会话清理由 `useMaskEditorSession` |
| 视频 Mask 纠错    | 打开时捕获 annotation/frame/session/segment        | 提交拒绝上下文漂移，要求重开                  | `commitVideoMask` owner 守卫（task/frame/tool/selection/mode/path/锁）+ 单飞 save  | 保存失败保留稿件可重试                    | 关闭对话框复位纠错状态                                    |
| PVS 种子采集/传播 | 对话框记录自建 jobId                               | 关闭/切题清空采集态（`closePropagateDialog`） | job 候选就绪/失败/移除才关闭；`propagateDialogRef`+seedMode 断言临时工具会话仍有效 | 提交失败回滚 submitting，保留种子与对话框 | 关闭/进行态转换清空种子                                   |
| 任务切换          | `resolveLocalTaskUrlSync` 区分外部导航与本地未写回 | scheduler abort 旧导航 guard                  | `commitAfterNavigationGuard` 校验 signal 仍存活                                    | 不适用（无草稿）                          | scheduler dispose                                         |

## 4. 明确保留在装配层的模块（归属理由，非体量理由）

- **Mask 会话键派生（D3）**：键由路由参数、URL task、选中、帧、工具、批次共同决定，输入全部归装配层；`useMaskEditorSession` 已持有编辑器生命周期。迁移只会把八个装配层输入传进 hook 再原样返回，不消除任何耦合。`handleMaskLeaveDirty` 是导航守卫（taskNavigation 域）的 Mask 侧适配器。
- **视图槽位接线（`renderVideoTrackSidebar`；C7 评审结论）**：JSX 本体已在 `VideoTrackSidebar` / `SelectedAnnotationCard` / `ImageBatchCardContent` 等 shell 组件中；model 侧的 `renderVideoTrackSidebar` 是单点实例化的纯 props 传递（35 个 props 全为已算好的值，无任何分支判定，见 §2.1 证据）。选中浮卡的内容分派**含本地判定**，已按 §4.4(4) 迁入 `shell/SelectionCardContent.tsx`（提交 `0ce27419c`）；Mask 确认弹窗簇迁入 `shell/MaskConfirmDialogs.tsx`（提交 `bcfc461fc`）。
- **遗留缺口（P6 残余处置，已读/诊断）**：`canBatchConvert` 为 SelectionCardContent 内单一消费者视图资格判定（无服务端授权重复），KEEP；`useWorkbenchShellModel.helpers.ts` 剩余 18 个导出全部有真实消费者（state/侧栏/图钉/图片动作 + 4 测试文件），死导出簇已删除，KEEP 共享模块形态。证据见 §7。

## 5. 测试与等价核对方法

- 每个提交独立跑受影响 vitest + `tsc --noEmit` + `eslint`；C5/C6 后全量前端套件 560 文件 / 5605 用例通过 [V]。
- 抽取等价核对：以 `git show <base>:model` 取原块，与 hook 内对应段落做归一化 diff（仅归一化参数引用改名、依赖数组补充的稳定 ref、注释版本前缀删除）；C3/C4 残差为 0 行，C5 残差暴露 `setPropagateBrush` 遗漏并当即修复 [V]。
- 新增行为测试聚焦「迁移后新增的接线风险」：hook 参数注入、草稿发布、冲突分类、迟到拒绝；未削弱任何既有断言（helpers 测试逐条迁入 taskNavigation.test.ts，无删除）[V]。

## 6. 命令与结果（本地，HEAD=本文档提交）

```text
pnpm --filter @anno/web vitest run src/pages/Workbench/   # 312 files / 3712+ 用例通过（分阶段多次）
pnpm --filter @anno/web vitest run                         # 560 files / 5605 passed（C6 后全量）
pnpm --filter @anno/web exec tsc --noEmit                  # 通过
pnpm --filter @anno/web exec eslint src/pages/Workbench/   # 0 告警（state 目录）
git diff --check                                           # 干净
```

## 7. P6 残余处置（证据支撑的结论，非待办转交）

浏览器验收（已执行，`exec --mode e2e`，Chromium，retries=0，真实后端）：`test:e2e:mask-native` 20 通过 / 2 配置性跳过（readonly 闭门矩阵行，native 配置下属预期）；`test:e2e:mask-ai-native` 7 通过；`video-mask-keyframe-operations` 1 通过；`mask-session-guard` 2 通过（脏离开对话框、Enter 单次提交）。合计 30 通过 / 2 配置跳过，三组退出码均为 0（`PIPESTATUS` 取证）。覆盖：Mask 预览→提交→刷新、失败保留稿件重试、迟到内容不回闪、锁定拒绝输入、离开/取消/草稿保留、tracker 局部接受/拒绝与人工帧二次确认、关键帧复制/outside/删除撤销/拆轨原子性。AI-native 行使用标注的 AI backend fixtures（非真实模型训练验证，不作 GPU/资格声明）。

两条 P6 残余项的**读/诊断处置**（基于消费者证据，非强行抽象）：

- **`canBatchConvert`（`shell/SelectionCardContent.tsx`）— KEEP。** 全仓唯一消费者就是本文件（定义于 L158，消费于 L177 的 `onConvert` 门控）；它是视图层资格判定（同几何类型 + 原生 Mask 持久化模式 + 无锁定），不与转换中心的的服务端授权重复——提交后仍由转换接口独立鉴权。谓词只读 props，无动态调用/反射/间接引用；为其单独建模块只会产生单一调用点的转发层（§4.2 排除项）。
- **`useWorkbenchShellModel.helpers.ts` 剩余纯函数 — KEEP（共享模块形态）。** 消费者证据：`clamp`→`useWorkbenchSidebarSizing`；`resolvePinViewport`→`useIssuePins`；`resolveSamCandidateClass`/`samCandidateGeom`→`useImageAnnotationActions`；`buildPipelineRunPayload`/`selectProjectPipelineStages`/`missingBackendIdsForStages`/`promptOfTool`/`resolveFloatingSelectionRect`/`resolveVideo*`/`annotationsForTask`/`resolveMaskEditorSize`/`classifyAccessLookupError`→装配 model；另有 4 个测试文件直接引用。两个死导出簇（floating-rect 五兄弟、`omitVariantFields` 导出）已在 R2 删除/收回。剩余函数无重复实现、无混合职责，按消费模块拆散需要改 ≥6 个文件的 import 而不合并任何规则，维持共享纯函数模块是当前语义最清晰的形态。

## 8. 回退边界

六个提交均可在 model/领域模块边界独立 revert；revert 后 model 恢复内联实现，无共享状态残留（hook 返回值仅被 model 消费）。文档与本报告随对应提交回退即可。
