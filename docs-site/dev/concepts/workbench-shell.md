---
audience: [dev]
type: explanation
since: v0.9.21
status: stable
last_reviewed: 2026-09-05
---

# 工作台 Shell 架构

视频的帧范围由 `annotationFrameScope` 和 `aiBoxFrames` 识别单帧与轨迹几何，画布和轨迹清单共用 `videoFrameViews.visibleInReviewMode` 的复审来源规则。标注持久化的 `is_hidden` 与会话内的隐藏轨迹集合共同约束渲染、预览和参考虚影；隐藏不删除列表条目，也不改变对象身份。图片原生 Mask 和点云邻帧参考输入同样排除隐藏标注。

Workbench 是图片标注、视频追踪、审核流共用的页面壳。它的边界不是按角色复制页面，而是把「模式」和「Stage」拆成两条正交轴：

```
WorkbenchShell
  -> useAnnotateMode() / useReviewMode()
  -> WorkbenchLayout
       -> WorkbenchBanners
       -> Topbar
       -> WorkbenchDockWorkspace
            -> 3D shared render surface（单 GPU canvas）
            -> canvas: ToolDock / WorkbenchStageHost / StatusBar
                 -> stageOverlay（候选审阅条）
            -> task-queue: TaskQueuePanel（任务队列）
            -> class-palette: TaskQueuePanel（类别面板）
            -> inspector: AIInspectorPanel
            -> discussion: DiscussionPanel
            -> ai-task: AIPredictionPopover
            -> video-tracker: VideoTrackerPropagateDialog
            -> tri-view: TriViewPanel（整体三行）
            -> camera-view: CameraDockPanel（整组相机图库）
  -> WorkbenchOverlays
```

## Shell 的职责

`WorkbenchShell.tsx` 只负责路由参数、项目与任务数据、React Query mutations、history、离线队列、快捷键注册，以及把这些依赖装配到子模块。

装配 model（`state/useWorkbenchShellModel.tsx`）把可复用的行为规则委托给同目录领域模块，自己只保留「取数 → 权限 → 会话键 → 视图槽位接线」。各业务规则的单一归属：

| 规则                                                                                           | 归属模块                                                                                                                                         |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| 任务切换准入（latest-wins 调度、离开守卫、本地 URL 同步）                                      | `state/taskNavigation.ts`（`LatestTaskNavigationScheduler`、`commitAfterNavigationGuard`、`runWorkbenchLeaveGuards`、`resolveLocalTaskUrlSync`） |
| 相对/智能任务步进、提交质检闸门                                                                | `state/useWorkbenchTaskFlow.ts`                                                                                                                  |
| Mask 原子变更错误策略（重试/刷新判定、错误文案、草稿快照契约）                                 | `state/maskMutationPolicy.ts`                                                                                                                    |
| 原生 Mask 变更工作流（实例原子操作、视频剪贴板、关键帧操作、幂等键、迟到刷新拒绝、破坏性确认） | `state/useMaskMutationWorkflows.ts`                                                                                                              |
| 视频 Mask 纠错对话框与 `commitVideoMask` 归属守卫                                              | `state/useVideoMaskCorrection.ts`                                                                                                                |
| PVS 种子采集与传播对话框                                                                       | `state/useTrackerSeedCollection.ts` + 纯模块 `state/trackerSeedPrompts.ts`                                                                       |
| 批量线 AI 后端选择（手动粘滞/项目默认跟随）                                                    | `state/useBatchBackendSelection.ts`                                                                                                              |
| 图片/视频创建更新语义                                                                          | `stages/image/useImageAnnotationActions.ts`、`stages/video/useVideoAnnotationActions.ts`                                                         |
| Mask 编辑会话（阶段机、rebase、单飞 save）                                                     | `state/useMaskEditorSession.ts` / `state/useMaskEditor.ts`                                                                                       |

它不直接渲染 `ImageStage` 或 `VideoKonvaStage`，也不直接拼装某个 Stage 的 annotation payload。图片和视频的创建、更新、改类、撤销相关语义分别下沉到：

- `stages/image/useImageAnnotationActions.ts`
- `stages/video/useVideoAnnotationActions.ts`

## Mode 轴

`mode: "annotate" | "review"` 由入口页传入，Shell 通过 mode hook 得到页面策略：

- `useAnnotateMode()`：提交、跳过、撤回、重开、smart next。
- `useReviewMode()`：领取审核、通过、退回、review diff、审核快捷键 slot。

这样审核模式继承同一套 Stage、任务队列、右栏、状态栏、离线队列和 history，不需要维护 `AnnotateWorkbench` / `ReviewWorkbench` 两套页面。

## Stage 轴

Stage 由 `StageKind` 分派：

```ts
type StageKind = "image" | "video" | "3d";
```

`WorkbenchStageHost` 根据 `stageKind` 选择具体实现：

- `ImageWorkbench`：包装图片 `ImageStage`，持有图片专属的 FloatingDock、CanvasToolbar、Minimap。
- `VideoWorkbench`：包装视频 `VideoKonvaStage`，持有视频时间轴、轨迹与 keyframe 操作。
- `ThreeDWorkbench`：包装 Three.js 点云工作台，持有 3D 框绘制、gizmo、三视图及相机投影的业务状态；将辅助视图 portal 到稳定的布局内容宿主。

`stages/types.ts` 里的 `StageCapabilities` 用来描述外围能力，例如是否有 class picker、AI 预标、timeline、viewport、comments。它不是内部编辑协议。

## 3D 约束

3D Stage 只复用外围壳：任务流、模式策略、右栏、状态栏、全局 overlay 和快捷键入口。

不要在 3D 接入前抽统一 geometry 或统一 editor 接口。图片 bbox / polygon 是平面 shape；视频 track 是 keyframe 派生的时间序列；3D 可能是 cuboid、点云选择、相机视锥或多视角联动。当前只统一 `StageKind`、`StageCapabilities` 和 `WorkbenchStageHost` 这一层边界。

### 3D 共享渲染 surface

`ThreeDWorkbench` / `usePointCloudScene` 保持唯一 Scene、renderer、geometry、草稿及回退 owner。`WorkbenchDockWorkspace` 提供覆盖整个工作区的 GPU canvas 宿主和稳定的三视图、相机内容宿主；`Workbench3DLayoutContext` 只传递宿主、可见区域及布局动作，不复制业务 session。三视图移到主画布之外也使用同一 renderer。

渲染 surface 与主画布交互宿主分离。主相机宽高比、OrbitControls、TransformControls、拾取及选点使用主画布的完整内容矩形；三视图各使用自己的完整行矩形。遮挡后的可见矩形只用于 scissor，不能用剩余面积重新计算投影或命中坐标。每次提交先清理共享 surface，再按可见区域绘制主 pass 和三视图 pass，移动或隐藏后不保留旧位置像素。

共享 canvas 位于 Dockview DOM 后方，结构背景与当前 GPU 面板内容透明。`workbenchViewportRegions` 按浮窗实际层级做矩形相减，将互不重叠的可见区域同时用于 DOM 裁剪和 GPU scissor。裁剪覆盖较低 group 的背景、标题、边框及独立 `dv-render-overlay`，不裁剪含上层浮窗的公共祖先；独立 CSS 变量承载应用遮罩，避免与 Dockview 自己写入的 `clipPath` 冲突。几何与层级变化合帧测量，纯位置移动及原生 overlay 延迟定位也会触发更新，结束后停止调度并清理旧遮罩。

可见性取原生 group 的活动标签、panel/group visibility 和 parking 状态；不能使用表示全局焦点的 `panel.api.isActive`，也不以延迟更新的 React `aria-hidden` 作为渲染真值。隐藏三视图停止正交 pass，隐藏相机图库暂停投影与深度覆盖层工作。相机编辑模态层和桌宠关联的 PSR 面板通过 portal 避开工作区内容裁剪，保留其坐标和编辑行为。

三视图管线预热、geometry generation、单帧调度及 device-lost 回退仍由现有渲染 owner 管理。布局移动不重建 Scene、不重复下载点云、不增加 renderer 或 GPU context。决策见 [ADR-0073](../adr/archive/0073-shared-surface-for-3d-docking)。

### Scene 连续浏览

Shell 持有当前会话的 `scenePlaybackActive`，将预览状态接到 `useTaskLock(enabled)` 和既有写操作边界。播放时停止心跳并释放旧锁；暂停后仅恢复当前帧的正常锁流程，在锁就绪前保持只读。锁请求按任务串行执行，取消后迟到的获取结果先释放，再允许同任务的新会话获取，防止旧清理释放新锁。

`ThreeDWorkbench` 汇总 PSR 的防抖提交、无效输入、在途保存、绘制草稿和编辑面板，向 `SceneTimeline` 提供启动阻塞原因；同时用任务身份、标注查询和已加载点云 URL 派生 `loading/ready/error`。相机图和渐进上色不阻塞点云就绪。播放中的编辑快捷键或画布点击先暂停，本次手势不提交编辑。

`SceneTimeline` 复用摘要窗口和唯一导航执行器，人工点选保留 160ms 合并，`useScenePlayback` 在目标帧就绪后按所选最高浏览速率停留，再串行导航。它以真实 Scene 身份区分内部切帧和外部跳转；页面隐藏、画布隐藏、资源错误或等待超过 15 秒均暂停，恢复后不自动播放。摘要每次最多 200 帧，展开轨道继续虚拟化，默认紧凑总览不假装掌握全 Scene 的标注密度。

### 3D 快捷键不走集中式 dispatchKey

工作台的集中式快捷键派发（`hotkeys.ts` 的 `dispatchKey`，由 `useWorkbenchHotkeys` 全局注册）是为 2D 画布 / 视频设计的：`HotkeyAction` 联合类型只有 `setTool` / `arrowNudge` / `acceptAi` / `video*` / `submit` 等平面语义，没有 gizmo 模式、3D 工具、点云几何或相机切换。3D 的工具与编辑键（W/E/R gizmo、B/P/V 工具切换、Q 系列框拟合、Shift+→/← 跨帧延续、放大浮层 ←/→ 切相机、Delete/Backspace 删框）因此**由 `ThreeDWorkbench` 组件内 `addEventListener` 本地接管**，原因有三：

1. **键位与 2D 语义冲突**：如 `E` 在 dispatchKey 里是「提交质检」，3D 想用它切旋转 gizmo。`useWorkbenchShellModel` 用 `threeDOwnedKeys`（`w/e/r/b/p/v/Delete/Backspace`）在 `stageKind === "3d"` 时作为 `ignoredKeys` 传入，让 `useWorkbenchHotkeys` 对这批键提前 return，dispatchKey 不再消费它们。
2. **操作对象在组件内**：W/E/R 直接调 `sceneRef.current?.setTransformMode()`（three.js TransformControls），Q 系列调点云专属的 autofit 几何算法——壳层的快捷键 hook 拿不到这些引用。
3. **一键多变体**：如 Q / Shift+Q / Alt+Q 是三种不同拟合，←/→ 只在放大浮层状态下才是切相机，超出 dispatchKey「一键一 action」的表达力。

代价：这些键不在 `HOTKEYS` 派发表里，只作为**纯展示条目**（无 `actionType`）登记进 `?` 帮助面板的「3D / 点云」分组。它们同时登记在 `hotkeyBindings.ts` 的 Fixed 清单中，参与账号级改绑的冲突检查。

## Overlay 边界

跨 Stage 的弹窗放在 `WorkbenchOverlays`：待选类别、改类、SAM 接受、批量改类。图片画布自己的浮动控件仍放在 `ImageWorkbench` 内部。

这个边界保证视频 bbox / track 新建时也能显示 class picker，不再依赖 `ImageStage.overlay`。

视频候选审阅条仍使用 `WorkbenchLayout.stageOverlay`，相对中间 Stage 定位。当前题 AI 与视频追踪是独立 Dockview panel，视频标注中可以同时显示；打开入口会显示或聚焦已有实例，不创建第二份业务 session。

## 交互 AI 顶栏与状态来源

`InteractiveToolBar` 的主层显示提示输入和当前候选决策，高级区只持有展开状态。后端、模型、variant、文本和阈值继续由既有路由、偏好、项目配置与 `useInteractiveAI` 保存；折叠使用 `hidden`，不调用配置 setter、重跑推理或取消会话。视频当前帧提示与图片提示都透传 `interactiveVariantSlice`。

能力错误来自 `useBackendRouting` 的各后端 setup query 和 `useMLCapabilities` 的同一查询缓存；重试只 refetch 失败 query，不改变偏好或启动推理。本轮推理错误由 `useInteractiveAI` 的现有请求代次校验后发布，启动、成功、取消及 owner 变化时清除；`canRetry` 仅决定重试入口，不能代表错误是否存在。全部协商失败时，未选 AI 工具也展示恢复条；正在使用的工具不会因网络协商失败被当作能力不支持而退出。

图片 `requestSamAccept` 与视频 `requestVideoSamAccept` 各自复用已有待选类别 owner，顶栏按钮和画布 Enter 调同一入口，保留原生 Mask 原子接纳与 point / Exemplar 各自的消费语义。`data-workbench-ai-toolbar` 只保护控件事件路径，不在顶栏显示时全局屏蔽画布。候选捕获监听还让出原生输入、按钮、弹窗、IME 和重复事件。

## 当前视频轨迹上下文

`VideoKonvaStage` 从完整 `annotations` 与 `selectedId` 获取 bbox、polygon、polyline 或 Mask 轨迹身份；当前帧的可见几何、ghost、隐藏或 outside 过滤不决定轨迹是否存在。`videoTrackContext` 复用现有几何解析与可见关键帧导航，派生关键帧、插值、保持、outside 和本帧无几何。`VideoTrackContextBar` 在画布面板的正常布局流中占据上方一行，不持有第二份选择、播放或持久化状态，也不依赖详细时间轴的显隐。视频的公共工具浮层与 `stageOverlay` 一起传入下方的画布容器，与原绘制节点互为兄弟；它们使用相同画布坐标，不覆盖轨迹条，也不把工具事件冒泡到绘制节点。

来源读取原始关键帧字段；Mask 保持使用实际解析锚点，缺字段不采用 renderer 的人工兜底。`AnnotationOut` 的 JSON 序列化保留旧关键帧来源缺失，写入 schema 默认值和存量数据不变。补关键帧走既有几何 upsert 与 `onUpdate`；Mask outside 继续走版本校验和历史 owner。入口共同检查任务只读、标注锁、会话轨迹锁、工作帧范围及绘制/播放状态；条内事件通过 `data-workbench-track-context` 让后台快捷键退让，条仅可见时不屏蔽画布按键。

## 当前题 AI 请求与 Inspector 阶段

`AIPredictionPopover` 从当前题请求及现有候选数量派生 idle / running / review / error 展示；`useWorkbenchAiRequest` 只持有普通预测的请求归属，不接管 SAM 或 tracker 执行器。图片与项目编排仍调用 `useTriggerPreannotation`，视频仍调用 `mlBackendsApi.predictFrame`。

提交时深复制并冻结任务、视频帧、后端、模型及输入，视频在首次异步抓帧之前占用请求，重试复用捕获的 JPEG。当前请求在 project / task / frame 变化或卸载时失效；迟到响应不能修改新视图。图片返回的 Celery ID 通过 `async_jobs.celery_task_id` 精确匹配，再使用 `useAsyncJob` 查询进度和终态。项目 WebSocket 继续服务项目进度，不能作为当前请求身份。

图片只在匹配到支持取消的作业后开放取消，等待服务端终态；视频使用 AbortSignal。查询失败保留原作业，重试只恢复查询；候选刷新失败只重试刷新，执行失败才重放原输入。候选刷新完成后进入审阅，移动或隐藏 Dockview 面板不会改变请求归属。

Inspector 的候选属性按候选自身的工具单位读取 schema，由原候选选择持有；输入立即更新本地草稿，主接受按钮、面板内 A 与列表采纳共用 `acceptWithReviewEdits`。本地 A/D 让出输入、菜单、修饰键、IME、重复事件，并使用 `aiBoxOnFrame` 保留视频当前帧范围；全局监听通过 `data-workbench-ai-toolbar` 让出，避免重复执行或绕过草稿。高级表单保持同一挂载实例；未引入新的候选列表、Dockview grammar 或业务面板实例。

## 可停靠工作区

### 设置窗口与工具菜单的输入隔离

设置窗口内容与遮罩带 `data-workbench-settings`，工具溢出菜单带 `data-workbench-tool-menu`，入口单独标记为 `data-workbench-tool-menu-trigger`，打开时 `data-state="open"`。独立的背景键盘和全局滚轮监听先调用 `workbenchInteractionGuards.ts` 的 `isWorkbenchInteractionBlocked(event)`；有打开的标记，或事件的 `composedPath()` 包含标记时直接返回，不阻止传播，让浮层自身的键盘、焦点与滚动行为继续工作。事件路径判断保留已卸载的标记，防止关闭菜单的同一次事件落到背景。类别选择器的外部点击也遵循此边界，打开工具菜单不取消待选类别的草稿。关闭后的入口只接管 Enter、Space、向下方向键等打开菜单的输入，其余画布快捷键恢复；菜单的 React portal 冒泡事件不触发布局保存。

主快捷键还通过 `disabled` 暂停，开窗时清空空格平移与视频按住状态，并提交此前已有的方向键微移。`keyup`、`pointerup`、`mouseup` 中的释放和拖拽收尾继续执行。视频入口调用 `pausePlayback({ snapToGrid: false })`，暂停但不对齐采样网格、不切换帧；关闭设置后保持暂停。这一边界只负责设置窗口，不改变其它弹窗或后台任务的生命周期。

快捷键面板（`HotkeyCheatSheet`）遵循同一契约：内容与遮罩带 `data-workbench-hotkeys` 并进入共享事件卫兵，主快捷键 dispatch 在面板打开期间整体 `disabled`（与设置窗口同款开窗清held-key语义）；Escape 在录制改绑时先取消录制、面板保持打开，外部点击先取消未确认录制再关闭。

### 工具坞容量

图片与视频的 `ToolDock` 共享纯函数 `splitToolDock`，输入是能力过滤后的工具描述、当前工具和实际容器尺寸。`ResizeObserver` 测量工具坞及共用 CSS 的非交互尺寸样本，计算按钮、间距、分隔线和分组标题的完整高度。发生溢出后优先保留选择与当前工具，并为“更多”预留位置；极小高度保留可滚动兜底。主栏和本地 Radix 菜单调用同一动作，不拥有几何草稿或 Stage。容量改变时关闭菜单，卸载焦点圈后回到“更多”；入口消失时回到当前工具。详细时间轴作为画布浮层，不改变工具坞高度；停靠面板调整和浏览器缩放按实际工具坞高度重新计算。

### 视频工具作用范围

`useWorkbenchState` 以一个会话状态保存 `VideoTool` 与 `frame / track` 范围。`videoToolUnits` 从现有能力映射派生范围，只对矩形框、多边形、折线、Mask 做同几何配对；`select` 保留最近范围。工具坞按能力和范围投影，不保存第二份工具状态，也不把范围加入布局偏好。

`useVideoToolCommands` 为工具坞、明确目标的快捷键和实际对象选中事件提供统一准入。四类轨迹只在新的选中事件中映射对应工具，不通过选中状态 effect 覆盖后来的单帧命令。Shift 多选仍保留集合；移除一个成员不会把较早选中的轨迹重新解释为新命令。不可用目标保留选择工具、请求范围和说明。

准入直接查询 `VideoStageControls.getDrawingDraft`，丢弃时调用 `discardDrawingDraft`，不复制 Stage 的顶点、关键点或拖拽状态。已完成几何仍使用 `pendingDrawing.kind / frameIndex`；确认期间松开拖框所形成的类别待选稿按原工具和源帧识别，同属草稿一并丢弃，替换稿件则保留并撤销旧命令。Mask 使用原 `maskNavigationGuardRef`，待选类别的 Mask 保存仍由原 writer 的 resolver 持有。确认或保存返回后校验任务、分段、源帧、路由、请求代次和待选稿件，过期决定不得应用到新上下文。追踪种子采集通过同一准入临时借用 AI 工具，使用追踪后端能力；确认期间面板关闭、替换或种子模式变化则不再开始旧采集，退出时恢复原工具与范围。

列表通过 `VideoSelectionCommand` 一次传入对象、可选源帧和获准回调，不在准入前调用跳帧；轨迹侧栏原有的批量选择集合也等到获准后才更新。范围按钮、对象行和种子入口共用外部点击边界，避免待选类别在确认前自动保存。

已有矩形框轨迹的续画直接由原更新入口在松手时落关键帧。此类拖拽携带 `continuingTrack`，鼠标未松开前拒绝切换命令并说明原因；松手先结束拖拽所有权再提交，保留原续画自动前进，不把内部的下一轨迹选择误判为仍在绘制。

视频工具命令带 `data-workbench-video-tool-command`，类别弹窗在外部点击捕获阶段让位，避免确认前保存未知类别；标记只接管原生激活和导航键，普通工具字母仍可使用。绘制确认框使用 `data-workbench-video-tool-confirm`，打开时所有背景监听让位。

### 视频追踪审阅范围

`TrackerJobStore` 在原有 jobs、candidates 和提交生命周期内保存当前审阅作业，以及每个作业的目标 ID、源帧窗口和意图代次。`videoTrackerReviewScope` 只派生可用目标、选区内候选、未决计数和真实剩余帧区间，不持有另一份候选数据。Shell 将同一个 projection 传给审阅条、当前轨迹条和时间轴，bbox 与 Mask 画布预览先按这个选区过滤，再按当前源帧过滤。

新候选只在没有当前审阅作业时选择默认作业；后续到达或刷新不能抢走已选作业。切换作业保留各自范围，revision 更新只与仍合法的目标求交，不添加未选目标或扩大窗口。选区为空时保留零未决状态，剩余区间入口只导航，不隐式改变选区。参考轨迹通过候选的 source / target annotation ID 映射，只有明确加入或替换命令才更新目标。

任务 epoch、作业 generation 与请求所有权保护恢复、预览及决定后的异步写入；旧任务的响应不得复活候选，新 preview 不被旧 revision 覆盖。同一任务内旧作业已经落库的结果仍可更新其缓存，但不能重选该作业或覆盖当前范围。人工帧覆盖重试绑定点击时的任务、作业、范围意图和候选 revision，确认前后均检查是否仍有效。Mask QC 保留独立区域 selector 与原决定入口。

剩余区间导航经 `useVideoToolCommands.requestFrame` 使用原 Stage 和 Mask 草稿保护，并在等待后复核审阅意图；它保留工具范围与参考对象多选。审阅控件使用 `data-workbench-tracker-review` 让背景键盘监听让位，非模态审阅打开时仍可操作背景画布。

### 图片手工创建事务

图片 Polygon 的点集仍由 `useWorkbenchAnnotationActions` 管理；`usePolygonDraftPoints` 同步发布点集引用，使撤销、取消和任务切换能立即使旧批次失效。`usePolygonAutoPoints` 只保存本次 Shift 拖动的采样余量和待刷新的点，按累计 8 CSS px 距离重采样，一帧批量追加；追加时检查原点集引用、图像身份、视口和编辑许可。松手或释放 Shift 保留终点，Enter / Backspace 通过原快捷键所有者先完成当前批次再操作最新草稿。20,000 点预算只暂停自动采样，已有点保留；超过 500 个草稿顶点时使用一个 Konva Shape 绘制全部顶点，避免逐顶点创建 React 节点。手工单击、吸附、闭合与创建事务保持原入口。

`usePolygonBoundaryTrace` 只持有沿边界追踪的预览：冻结来源 ID、version、完整 geometry、起止点和待追加草稿引用。`polygonBoundaryTrace` 按屏幕坐标投影并枚举顺逆两条弧，不改变来源的绕序、孔洞或外环结构；复杂几何在选择时拒绝。确认通过现有 annotations GET 核验来源，网络失败保留可重试预览，来源变化使预览失效。追加仍由 `useWorkbenchAnnotationActions` 一次更新原草稿；任务、工具、锁、待保存状态和点集引用共同阻止过期操作。取消、卸载及上下文切换中止核验并检查迟到结果；`useWorkbenchHotkeys` 的 `beforeInput` 桥接让 Esc 先退出预览，Enter 先确认路径，Backspace 退出预览后逐点撤销。

普通矩形框的 `bboxCreationMode` 保存在 `useWorkbenchState` 会话中；`BboxTool` 在按下时锁存中心选项或 Alt，`ImageStage` 的预览与提交共用 `imageBoxFromDrag`。中心创建按两侧最近图像边界限制半径，松手读取最终指针坐标，避免最后一次 move 尚未渲染时丢失终点。几何随后仍进入原创建事务，过小、零面积和属性校验由已有漏斗处理；起点选项不写入用户偏好或标注合同。

`useWorkbenchState` 保存会话级连续创建意图 `(projectId, tool_unit_id, class_name, tool)`，只允许矩形框、旋转框、多边形、折线和模板关键点；区域单元恢复 Polygon。`useWorkbenchAnnotationActions` 仍是几何创建 owner。完成的几何附带唯一草稿 ID、任务、工具单元、类别、独立属性和 `class / attributes / saving / error` 阶段，类别和属性均从该单元读取，不使用展示层的跨单元兜底。默认值逐对象复制，缺失必填项复用 `AttributeForm.getMissingRequired`，草稿表单同步更新以支持输入后立即 Enter；旧草稿 ID 的更新无效。

同步 ref 防止一次完成动作触发重复请求。在线成功或 `enqueueDurably` 确认 IndexedDB 事务提交后才释放草稿，失败保留同一几何重试。任务代次保护选择、history 和弹层；切题清空原题草稿，同项目保留仍合法的意图。底层 `useCreateAnnotation` 将任务与视频分段放进每次调用的内部变量，缓存与请求都使用调用时的归属，避免等待 `onMutate` 或网络时切题把写入转移到新题。公开 mutation 调用仍接收普通 annotation payload。

离线队列沿用已有存储键。入队、出队和 ID 替换通过同一个原子读改写事务执行；网络 handler 在事务锁外运行，防止重入死锁或覆盖期间新增的操作。旧 `enqueue` 保留尽力写入的兼容接口，要求保存确认的流程必须等待 `enqueueDurably`。这保证本机接收语义，不新增远端幂等协议；远端成功后本机提交失败、跨标签页并行 drain 等既有边界仍不能保证远端只执行一次。

## 右栏：AI 检查器 + 讨论面板

`WorkbenchDockWorkspace` 是 Dockview 的唯一 React 适配层。`workbenchPanelRegistry` 定义稳定面板 ID、渲染槽、生命周期和布局能力；`workbenchLayoutExecutor` 负责移动、停靠、浮动、隐藏、预设与紧凑布局重放。Shell 继续提供业务状态和回调，布局快照不保存 React props、工具、选择、任务、播放位置或编辑草稿。

工作区宿主和 Dockview 分栏容器使用不可滚动的裁剪，避免焦点定位改变布局坐标原点；列表、属性和讨论等面板内容仍各自管理滚动。布局重放继续校验实际位置与尺寸，不因容器偏移放宽误差。

| Panel ID        | 内容                            | 生命周期与约束                                                         |
| --------------- | ------------------------------- | ---------------------------------------------------------------------- |
| `canvas`        | ToolDock、当前 Stage、StatusBar | `always`；独占稳定 group，可从菜单换到根边缘，禁止浮动、关闭或标签化   |
| `task-queue`    | 任务队列                        | `onlyWhenVisible`；允许停靠、标签、浮动与隐藏                          |
| `class-palette` | 类别面板                        | `onlyWhenVisible`；允许停靠、标签、浮动与隐藏                          |
| `inspector`     | 标注详情、人工标注与 AI 候选    | `always`；隐藏时保留未完成的属性编辑                                   |
| `discussion`    | 评论、历史、Issue               | `always`；隐藏时保留未发送输入                                         |
| `ai-task`       | 当前题 AI 运行与候选审阅        | `always`；仅图片、视频标注 context 提供入口                            |
| `video-tracker` | 视频追踪配置与运行              | `always`；仅视频标注 context 提供入口                                  |
| `tri-view`      | 俯视、侧视、正视三行精修        | `always`；仅 3D context 提供入口，作为一个面板移动与隐藏               |
| `camera-view`   | 相机停靠图库                    | `always`；仅 3D context 提供入口，禁止原生图库浮窗，模式由整组命令切换 |

- **上段 `.rightSplitTop`**：`AIInspectorPanel`，与下段之间有一个上下拖拽 handle。上段高度持久化到 localStorage `workbench.rightSplit.topHeight`（默认 360px，范围 160–720px）。
- **下段 `.rightSplitBottom`**：`DiscussionPanel`，承载评论 / 历史 / issue 的统一讨论入口。
- **列宽拖拽 handle** 提升到 `.rightSplit` 全高层级，覆盖两段，不再只贴在 AI 检查器一侧。
- **布局偏好**：左右栏开合、左右栏宽度、任务队列 / 类别面板 / 标注详情 / 讨论面板浮窗、3D 三视图浮层、2D 相机面板布局和点云主视角快照写入 `user.preferences.workbench.layout`；前端提交全量 `workbench` 子树，后端递归合并偏好字典，列表覆盖；`workbench.layout.cameraPanels` 按原子 map 替换，`workbench.layout.workspace.namedPresets` 还使用独立 revision 防止陈旧整图覆盖。
- **侧栏区块分离**：`TaskQueuePanel` 内的任务队列和类别面板、`AIInspectorPanel`、`DiscussionPanel` 都可由 `WorkbenchLayout` 改用 `FloatingPanelShell` 渲染。分离操作默认收起对应侧栏；后续展开只显示仍嵌入的区块，不会自动合并浮窗。合并回侧栏只恢复嵌入状态，不主动展开侧栏。若一侧两个区块都已分离，侧栏 toggle 是可见 no-op。

外围面板可放到画布左、右或底部，也可与其他外围面板组成标签。同窗口浮窗可以包含标签，但浮窗内部不支持再次切分网格。画布换位命令将同一个 `canvas` group 放到整棵可见树的左、右、上、下边缘；画布继续隐藏 header 并锁住原生拖放。`parking` 不显示 header，也不接收用户拖放。适配层不提供 popout，不调用 `addPopoutGroup`，快照清洗也不保留外部窗口描述。

隐藏面板时，executor 先记录原 group、tab index、停靠锚点与尺寸或浮窗矩形，再移动同一实例到不可见的 `parking` group。恢复优先使用仍存在的合法返回位置，否则按保存的锚点或 registry 默认区放置。标题栏常驻 × 与菜单共用隐藏命令，只隐藏该组当前活动标签；上下分割的其他组不受影响，画布不提供 ×。布局入口不调用 `close` 或 `removePanel`；`always` 面板保持 DOM，隐藏时停止非必要的持续工作。

顶部“布局”菜单提供“标准标注”“专注画布”“审核协作”，并按 context 提供“图片 AI 审阅”或“视频追踪”、面板列表和“重置为标准布局”。3D context 另提供框体精修、传感器融合、点级分割、整组相机模式切换与“恢复相机排列”。三种 3D 工作方式只改变辅助面板显隐，保留画布和其他面板的位置及当前工具；相机沿用上次呈现模式。专注画布使用现有 canvas group 的 maximize / restore。预设替换与一次会话级撤销都通过 executor 原位重排现有面板，不触发全树 `fromJSON`，也不改变标注内容和 Stage 的 React 身份。

相机有互斥的 `floating` 与 `docked` 两种呈现：前者使用现有逐路相机浮层，后者只挂载一个 `CameraDockPanel`。图库在窄列单列滚动，按最小图宽 240px、间距 8px 自适应网格；名称、朝向、图像与投影都来自当前帧。切换只改变整组呈现，不支持逐路混合；缺帧、加载或错误不移除布局节点，也不沿用上一帧图片。相机编辑大图继续复用现有种框、标定与人工 2D 成员流程。

### 讨论面板与已有入口

顶部左右按钮按画布左右两侧的实际停靠位置收起或展开整个区域，不绑定任务队列或标注详情名称。它们保留列宽、上下比例和标签顺序，不影响浮窗。单个面板由标题栏 × 或菜单隐藏，再从“布局”菜单找回。Issue FAB、评论跳转和 `requestIssuesTab()` 先打开或聚焦 `discussion`，再切换其内部 tab。

`DiscussionPanel` 的内部业务 tab 保持原边界：

| Tab      | 内容                            | 实现                                                  |
| -------- | ------------------------------- | ----------------------------------------------------- |
| comments | 标注级 / 任务级评论             | `CommentsPanel`（`hideTabs` + `forceTab='comments'`） |
| history  | 标注 / 任务级 audit 历史        | `CommentsPanel`（`forceTab='history'`）               |
| issues   | `kind=issue` 反馈列表与图钉联动 | `DiscussionIssuesTab`                                 |

图钉和讨论入口通过 `useActiveIssueStore` 的 `tabRequestTick` 请求切到 issues tab。评论画布与图钉的业务交互见[审核模块](./review-module)。布局不拆分 `AIInspectorPanel` 或 `DiscussionPanel` 的内部业务 tab。

### 桌面与紧凑布局

工作区宽度不超过 1024px 时，适配层在初始偏好读取结算后进入紧凑模式：先锁存已清洗的桌面快照，再把外围面板移到 `parking`；面板菜单每次只将一个现有实例放入 `compact-overlay` 同窗口浮窗。预设、重置、布局撤销、停靠、浮动与 resize 都禁用，进入紧凑模式时丢弃已有预设撤销点。

返回桌面宽度后，executor 以固定 canvas group 为锚点，使用 move / visibility / size API 重建原有 group、tab 顺序、活动标签、浮窗和隐藏状态。切换过程不使用 `fromJSON`，不重新挂载 Stage；3D 主视图和三视图继续遵守单 renderer / canvas 的约定。

紧凑投影、重放中间状态和临时 `compact-overlay` 都不提交给服务端。重放失败时保留桌面快照并进入只读标准布局；用户返回桌面宽度后显式重置，才允许写入恢复结果。小于 768px 的窗口继续显示现有阻断页。

## 布局偏好与恢复

可停靠布局存放于 `user.preferences.workbench.layout.workspace`：

```ts
{
  engine: "dockview@8",
  contexts: {
    "annotate:image": {
      schemaVersion: 5,
      snapshot: {
        layout: serializedDockview,
        returns: panelReturnPositions,
        visibilityIntent: {
          "ai-task": "shown", "video-tracker": "hidden",
          "tri-view": "hidden", "camera-view": "hidden"
        },
        cameraPresentation: "floating"
      }
    }
  }
}
```

context 是 `annotate|review × image|video|3d` 的六项闭集，按账号分别保存。`snapshot.layout` 只保留引擎布局字段，`returns` 保存隐藏面板的 group、index、停靠锚点与尺寸或浮窗矩形，`visibilityIntent` 记录四个工具面板的显示或隐藏意图。`cameraPresentation` 独立记录相机整组模式；floating 时 `camera-view` 位于 parking，但相机仍可按整体显隐意图显示为逐路浮层。

当前客户端解释 schema 1–5 并统一写入 schema 5。旧五面板或七面板快照补齐缺少的固定节点，保留原树与尺寸；九个节点在六个 context 中均存在，不适用的工具停在 parking 并隐藏入口。schema 6 或更高版本显示只读标准布局，提示刷新到新版，也不允许重置覆盖。损坏快照和引擎恢复失败同样使用只读标准布局，但允许用户在桌面模式显式重置。

`workbenchLayoutSnapshot` 对读写执行同一套清洗：UTF-8 JSON 上限 64 KiB、树深度不超过 12、最多 40 个树节点；持久化包含 9 个 panel、最多 9 个用户 group 和 1 个 parking group。非有限尺寸、非法 canvas、重复 panel 和不合法树会触发恢复路径，业务 params、popout 与不支持的引擎字段不进入快照。浮窗边界按工作区实际 client rect 夹取。外围停靠组可保留位置并收起，canvas 及其祖先必须可见，活动组必须可见。左右区域由 canvas 祖先路径上的横向兄弟子树确定；收起前的尺寸写回隐藏节点的缓存尺寸，展开时由画布吸收空间变化。

### 单一写入者

`useWorkbenchWorkspaceLayout` 独占当前 context 的 workspace 写入，复用 `useUserPreferences` 的账号级 React Query key 与请求缓存：

1. 冷启动先读取 `workbench.<userId>.workspace.<context>` 本地缓存，但初始 preferences GET 和该账号尚在途的旧布局保存结算前，禁用全部布局 mutation，不发 workspace PATCH。
2. 初始 GET 返回受支持、清洗通过且不同于本地的快照时，适配层最多进行一次 `fromJSON(remote, { reuseExistingPanels: true })` 回灌。初始化完成后的 refetch 不再替换当前树，GET 失败则沿用本地布局并显示提示，有效快照可以继续调整。
3. 布局操作结束后防抖 300ms，只 PATCH 当前 context。一个请求在途时合并后续变化，只保留最新 dirty 快照，前一请求结束后再提交，避免响应乱序覆盖较新调整。
4. 普通 PATCH 失败保留本地调整与 dirty 状态，不回滚用户看到的布局，也不循环重试；下一次调整可再次保存。`409 layout_schema_downgrade` 会丢弃待写内容并进入不可重置的只读状态。

本地缓存按账号和 context 隔离，切换后取消旧 context 尚未发送的定时写入，旧请求回包不回灌新会话。`useWorkbenchConfig` 的 `setLayout`、`setFields` 和完整偏好保存继续负责其他字段，但通用本地与服务端 writer 都剔除 workspace 副本和已退役的 `triViewFloat`，不产生第二份布局写入。

后端复用 `GET/PATCH /auth/me/preferences`。PATCH 在事务中锁定并刷新当前用户偏好，将 `workspace.contexts.<context>` 作为原子替换路径，锁内拒绝 schemaVersion 降级；不同 context 仍分别合并。同 context、同 schema 的多标签页和多设备采用最后一次写入生效，不引入 revision 或 ETag。部署时先上线后端 schema，再上线可写 workspace 的前端。

### 命名布局预设

`workspace.namedPresets` 与 `contexts` 并列，保存用户显式另存的布局：每账号至多 5 条，键是预设 ID，值在 context 快照信封上增加 `name`（不超过 40 字）与 `context`，同一账号内名称不重复。后端把它当作整份原子替换的 map，省略某条即删除；`contexts` 因此可以缺席，只存过预设的账号不算损坏快照。响应根部的 `namedPresetsRevision` 是该 map 独立的 opaque compare-and-swap token：预设写入必须回传最近 token，后端在刷新并锁定用户行后比较，成功写入生成新 token，陈旧写入返回 `409 named_presets_conflict`。其他偏好写入不推进该 revision。

`useWorkbenchNamedPresets` 是它唯一的写入者，与 `useWorkbenchWorkspaceLayout` 共用同一个 React Query key，但两者提交的键互不相交：布局 PATCH 不带 `namedPresets`，预设 PATCH 不带 `contexts`，各自只在缓存里替换自己那一部分。预设写入遇到 revision 冲突时不自动重放陈旧 map，而是刷新权威清单并要求用户重做该次操作。同名保存视为更新既有条目，不占新名额。无法解析或来自更高 schema 的条目原样参与每次整表提交，界面上只允许删除，不允许应用，避免旧客户端洗掉新版预设。应用预设走与内置预设相同的原位重排路径，结果照常落入 `contexts.<context>`，不持久化“当前预设 ID”。

### 初次迁移与保留字段

没有当前 context 快照时，从旧左右栏开合、四个分离面板、栏宽和右栏 split 构造初始布局，并由 workspace owner 保存。转换后这些旧字段不再驱动 Docking UI，也不反向同步新树；旧字段与 localStorage key 保留供前端回滚使用，回滚恢复的是升级前的旧布局。

3D 首次迁移仅在当前账号的权威 preferences 返回后读取一次 `triViewFloat` 显隐意图，采用新的默认停靠位置；旧绝对坐标不直接转换为列宽，也不使用无账号归属的历史坐标。相机初次迁移继续使用悬浮模式并保留 `cameraPanels`。后续三视图、相机图库空间状态和相机整组模式 / 显隐只由 workspace writer 管理；旧 `triViewFloat` 值留作回滚资料，不再更新。

下列布局状态仍由原有业务组件和 `useWorkbenchConfig` 管理：

| 字段                | 作用                                               |
| ------------------- | -------------------------------------------------- |
| `floatingSelection` | 选中信息卡的位置、尺寸和折叠态                     |
| `cameraPanels`      | 仅按相机 role 保存悬浮位置和折叠态，停靠切换不清空 |
| `pointcloudCamera`  | `persistCameraView` 开启时记录与恢复点云主视角     |

三视图和相机图库进入 Docking 树，逐路悬浮相机仍使用原有浮层系统。PSR、选中信息卡、桌宠、Drawer、Modal、候选审阅条与 Toast 不进入 Docking 树；其中需要越过内容裁剪的 PSR 与相机编辑层通过 portal 渲染。

## Mask 阶段动作所有者

`maskPrimaryActions` 从现有编辑 phase、revision、区域 / 实例预览、权限和恢复状态派生主次动作，不保存第二份 Mask 状态。
`useMaskPrimaryActionOwner` 接管按钮和图片 / 视频 Enter、Esc 分派、空结果确认及整个提交入口的单飞保护；准备 RLE、选类和网络保存都属于同一次动作。
区域应用只修改草稿，实例提交与普通保存仍使用原有持久化 owner；失败或过期预览不能降级为普通保存。低内存造成的像素只读与持久提交权限分开判断。

### 原生 Mask 变更工作流（useMaskMutationWorkflows）

实例原子操作（合并/重叠/拆分/复制/切割）、视频 Mask 剪贴板与关键帧单飞操作统一由 `state/useMaskMutationWorkflows.ts` 持有。每个异步入口回答五个归属问题：

- **owner**：草稿在预览时刻快照 scope 与成员版本（`PendingMaskAtomicDraft.members`），提交与重试只使用快照版本，不读后续 refetch。
- **cancel**：新预览重置幂等键；预览消失的 effect 丢弃草稿；`transitionInFlightRef` / `commitInFlightRef` 保证提交与刷新单飞，切换不会在写入中途发生。
- **stale 拒绝**：`refreshMaskInstanceOperation` 每次运行打 token，并在每个 await 之后核对 Mask 会话上下文（task/frame/tool/selection key + generation）；会话已切换则丢弃迟到结果，不把旧 Buffer rebase 到新会话。
- **草稿保留**：提交/刷新失败保留编辑器 Buffer 与待提交草稿（提示「草稿已保留」），只有显式刷新或取消才丢弃。
- **清理**：会话切换清理由 `useMaskEditorSession` 负责；预览消失时本模块清空草稿与幂等状态。

纯策略（哪些服务端 reason 只允许刷新、错误文案映射）在 `state/maskMutationPolicy.ts`；几何规划在 `stage/shared/geometry/maskInstanceOperations` 与 `maskMutationDraft`。

### 视频 Mask 纠错（useVideoMaskCorrection）

纠错对话框上下文（annotation/frame/session/segment）在打开时捕获；提交前核对上下文仍与活跃会话一致，否则拒绝保存并要求重开。`commitVideoMask` 的写归属守卫（task/frame/tool/selection/mode/path/锁）防止退役上下文的迟到保存写入新上下文；传播创建委托给 tracker owner，纯模块 `videoMaskCorrectionFlow.ts` 负责保存→传播的时序。

### 任务切换与迟到结果

`selectTask` 是所有切题入口（列表点击、通知、URL、讨论跳转）的唯一准入通道：离开守卫按视频 → Mask 顺序等待，任何阶段发现上下文过期即放弃提交；`resolveLocalTaskUrlSync` 区分外部 URL 导航意图与本地切题尚未写回的旧值，防止旧 URL 把当前任务拉回去。切换后的旧异步结果（标注 refetch、Mask 刷新、tracker 预览）分别由各自领域 hook 的 token/generation 断言拒绝。

空结果确认固定到当前会话 owner、preview ID 和 revision，切换任务、帧、对象或预览后失效。区域预览也属于待处理草稿，切换和普通 Esc 退出复用现有未保存 guard；计算只取消当前运算，保存或刷新期间不销毁会话。
图片提交在 RLE 合并、选类、上传完成后核对原归属，写入成功后的历史、选择和清理只作用于当前 owner。视频上传后的关键帧写入和成功回调使用同样的归属检查；旧任务的缓存可接纳自己的结果，当前任务的历史和选择不受影响。
快捷键在菜单、弹窗、输入控件、组合输入和长按期间让位，避免关闭菜单的同一次 Esc 继续操作背景编辑器。

## 图片切割与原子历史

`ImageStage / usePolygonSlice` 持有切线、来源版本、两块预览与不可变提交请求。开始时清除当前选择以收起遮挡画布的对象浮窗，靠近来源边界时复用边界吸附；取消只销毁预览。切题、切工具和只读状态变化使旧会话失效，迟到请求不能清空新预览。提交失败仍保留同一 payload 和幂等键，来源刷新也不能改写已发送请求。

Shell 通过 `annotationSlicesApi` 提交，在原任务记录一条受限 `slice` history 命令。命令不允许嵌入 create / delete 叶批次，只保存原操作 ID、完整结果版本、恢复期限与待重试请求键。undo / redo 成功后才移动栈，失败保留位置；任务切换期间每个异步操作只结算原任务历史并刷新原任务缓存。恢复响应先更新历史，缓存刷新异步进行，避免用户立即刷新时丢失已成功撤销的历史位置。

服务端快照、幂等、版本保护与不可收窄的账本迁移见 [ADR-0074](/dev/adr/archive/0074-atomic-annotation-slice-restore)。

## 偏好四分树与设置窗口

`user.preferences.workbench` 从平铺字段重构为四个模态子树 + 顶层 `layout`：

```
workbench
├── common      # 跨模态通用（longTaskSampleRate / confirmDelete / recentClassesLimit / crossFrameOverlay*）
├── image       # 图像渲染与交互（smoothImage / cssImageFilter / controlPointsSize / autoFitOnResize / ...）
├── video       # 视频播放与步进（defaultPlaybackRate / largeFrameStep）
├── pointcloud  # 点云渲染、导航、上色与深度（pointSize / persistCameraView / colorize* / showDepthHint / ...）
├── layout      # 壳层布局，保持顶层不动（见上节）
└── shortcuts   # 账号级快捷键覆盖（schemaVersion + common / image / video 命令桶；见下节）
```

- **后端**：`apps/api/app/schemas/user.py` 四个子树 Model 均 `extra="forbid"`；存量 JSONB 由 alembic `0103` 数据迁移就地改写（up/down 可逆、幂等）。`update_preferences` 入口保留一层 legacy 平铺键提升器兼容旧 tab。
- **`ProjectRenderingConfig` 保持平铺**：项目侧不迁移；`useWorkbenchConfig.applyProjectOverride` 把平铺的项目覆盖映射到 `image.*` 子树字段,`lockedFields` 语义不变。
- **字段注册表**：`state/workbenchSettingsFields.ts` 是设置 UI 的单一来源（key / 分类 / section 分组 / 控件类型 / 是否可锁定）。Settings 页「标注偏好」与工作台设置窗口共用它 + `components/SettingsFieldControl` 渲染，**新增设置项 = 后端子树加字段 → `auth.ts` 类型同步 → 注册表加一行 → 消费点读配置**。
- **设置窗口**：`shell/WorkbenchSettingsDialog.tsx` 复用 Radix Dialog / Tabs。桌面居中双栏，窄屏全屏；固定显示界面布局、标注显示、编辑与辅助、画布与视角、播放与轨迹、性能与实验六类，搜索匹配名称、说明、分组与选项，并保留命中子项的父开关。`WORKBENCH_SETTING_GROUPS` / `WORKBENCH_SETTING_SECTIONS` 和 `groupWorkbenchSettings()` 统一窗口、搜索与个人页的展示顺序，`category` 仍只表示原有偏好子树。窗口不接收 Stage 类型，不做模态可见性过滤。`SettingsFieldControl` 的 `settings` 布局显示说明，个人页保留默认 `compact` 布局。
- **关闭与焦点**：`DialogContent.overlayProps` 为当前窗口配置遮罩层级和事件。仅从遮罩开始的完整点击触发关闭，避免滑块拖出窗口误关闭；关闭与切换分类前 blur 活跃字段。Dialog 接管焦点陷阱与恢复，组合输入期间不响应 Esc，背景输入按上文统一隔离。
- **保存**：写路径仍走 `useWorkbenchConfig.setFields()`（本地立即生效、300ms 防抖 PATCH、卸载 flush）；各实例经模块广播同步，滑块提交后画布更新。hook 不随窗口关闭卸载；初次加载失败提供 `loadError` / `retryLoad` 并禁止写入，保存失败通过 toast 告知未同步。二次推理面板显隐沿用 `useSecondaryBarHiddenPref`，各任务均可调整但仅影响图片工具条；隐藏孤儿标注仍是会话回调。
- **shortcuts 子树**：`workbench.shortcuts` 承载账号级快捷键覆盖（`schemaVersion` + `common` / `image` / `video` 三个命令桶）。它不进 `useWorkbenchConfig` 的整树 PATCH；唯一的写方是 `state/useWorkbenchShortcutPreferences.ts` 的定向 PATCH（只提交变更的命令条目，不带 `schemaVersion` 以免把更新版本存量树降级）。后端对变更条目严格校验（命令 ID、键格式、修饰键、每命令 ≤2 条），未触碰的存量条目按 opaque 原样保留；GET 对损坏 / 更新版本子树整体透出，不 422、不用默认值覆盖。

## 快捷键生效绑定与账号级改绑

快捷键系统分四层，单一真值链路如下：

```
HOTKEYS 目录（state/hotkeys.ts，稳定 id + 展示元数据）
      │
hotkeyBindings.ts（可编辑命令注册表 + Fixed 清单 + 规范化 + 冲突引擎）
      │
useWorkbenchShortcutPreferences（共享 preferences query + 定向 PATCH 写路径）
      │
dispatchKey(ctx.matchCommand) → 既有 action 处理器
```

- **稳定命令 ID**：`HOTKEYS` 每条目带 `id`（如 `image.tool.box`、`video.frame.next`），另带用途分类（`category`）、工作台类型（`stages`）、生效条件（`applies`）与精选标记（`common`）。`dispatchKey` 中可编辑命令的按键匹配改经 `matchCommand`（按生效绑定表），Fixed 命令保留显式分支；两条路径都要求修饰键完全一致，Shift/Alt 变体不再顺延命中已记录的组合。IME 组合期与 AltGr 字符不参与任何 Workbench 命令。
- **可编辑范围**：本增量只开放图片 / 视频工具选择、通用切题、图片选中态锁定 / 隐藏、视频选中轨迹状态与书签、视频帧 / 网格 / 源帧微调 / 关键帧导航。其余全部是 Fixed：`hotkeyBindings.ts` 的 `FIXED_SHORTCUTS` 登记 Fixed 命令的默认键与生效上下文（含项目类别数字键、3D 本地键、播放手势、剪贴板 / 历史键），参与冲突检查但不可改绑。审核 A/R、方向键微调、视频首尾帧和 SAM/Mask 的修饰键变体同样登记；固定动作按互斥上下文判断，保留明确的默认工具重叠。派发显式接收图片、视频或 3D 类型，并在原有编辑保护通过后按生效命令匹配，不再依赖默认物理键分支。启用和恢复默认先验证整份目标组合。Backquote / Enter / Esc / Tab 等物理键位例外不可录制。
- **冲突模型**：域（common ∩ image/video/threed）与谓词（选中态、预测态、轨迹态、采样态）可证明互斥时允许共用按键（如 L 键的「锁定选中」与「折线工具」互补共存）；否则按绑定 token 精确相等判定冲突。改绑在面板内即时预览冲突并拒绝确认，给出对方名称、生效上下文和跳转入口；不提供自动「顶掉对方」。加载与项目 / 上下文变化时复查全部生效组合，互相争用的覆盖成对停用（保留存量值供修正，运行期都不执行）。
- **项目类别数字键**：类别直选只有十个数字槽 1-9 加 0，按工具绑定单元的配置顺序取前十个；槽位不随搜索、选择或最近使用重排。字母不再切类别（画布字母全部是工具 / 动作键），类别弹层的直选同步收窄到数字槽，并新增 ↑/↓ 移动高亮、Enter 确认高亮项（查询为空时维持默认类别）、组合输入不确认。视频 `0`（实际尺寸）与 3D 三视图 `0`（聚焦视图缩放重置）让位改绑为 `Shift+物理 Digit0`；`Mod+0` 视图重置要求修饰键完全一致。图片属性数字键只在选中卡「属性快捷键」区域显式聚焦后生效（`AttributeForm.shortcutRegion`，仅图片选中卡两个消费者 opt-in），区域失焦立即交还画布类别键。
- **偏好契约**：`useWorkbenchShortcutPreferences` 以账号为 key 读写共享 preferences query；同命令重复编辑按命令串行合并（旧完成不覆盖新值），面板通过 `previewEffective` 合并待保存编辑并预检冲突，画布仅使用 `effective` 中的已确认值。成功响应只回填对应命令，避免覆盖其他偏好；失败保留待写编辑供重试 / 放弃。每次派发和完成均检查账号与挂载归属，退出工作台后取消尚未发送的请求；旧请求不能删除新请求的队列。偏好加载失败禁止写入默认值。工具坞角标与 tooltip 按生效组合展示（`toolHotkeys`），停用命令清空角标。

<!-- history: DiscussionPanel and the split right rail shipped through the v0.11 workbench slices. FloatingPanelShell + layout preferences shipped in v0.13.10. The four-subtree preferences split + settings window shipped in v0.15.3. The shortcut reference dialog + account-level customization shipped in v0.24. -->

Mask 切割复用 `MaskToolbar`、`useMaskEditor` 的实例预览和 D 的主动作解析器。画布只持有两点直线的拖动状态；切题、来源 / 工具 / buffer 变化或取消时清理。`slice_mask` 预览计算只分区本地 alpha；确认才沿 `mask-mutations:commit` 上传两个结果并提交固定请求。收到 `slice_restore` 即向原任务入栈同一种受限 slice 命令；客户端不创建独立的 Mask 回滚栈。服务端恢复通过 `MaskAnnotationRevision` 解析前后版本，先锁定并校验内容，再锁对象，并在触发器捕获旧版本后保护引用期限，保持现有 GC 合同。

## 可复用的上下文工具栏

`shell/ContextToolbar.tsx` 负责左侧胶囊、悬停常用操作、顶部居中的完整栏，以及展开/收起的位移和缩放动画。Mask、交互式 AI、二次推理及视频候选审阅共用该容器。停靠面板、原生相机浮层与状态提示仍由各自系统管理。

工具提供 `id`、`label`、`summary`（图标和必要状态）、`summaryLabel`、`quickActions`，以及接收 `close` 的内容渲染函数。可选 `primaryContent` 在收起时保留必要输入与当前会话决策，位于摘要按钮之外；业务控件必须受控，不能依赖呈现子树保存状态：

```tsx
<ContextToolbar
  id="example"
  label="示例"
  summary={<ToolIcon />}
  summaryLabel="示例常用工具"
  quickActions={[{ id: "pick", label: "点选", icon: <PickIcon />, onSelect: selectTool }]}
>
  {(close) => <ToolSettings onCollapse={close} />}
</ContextToolbar>
```

参数、草稿、保存、撤销和异步任务仍由工具原有 owner 持有。收起只改变显示；完整栏内容在关闭动画结束后卸载，因此需要保留的状态应放在容器外。`id` 在同时挂载的实例之间保持唯一；切换到不同 owner 时按原有生命周期卸载或使用对应的 React key。

容器使用通用 `data-workbench-context-*` 标记接入事件守卫，允许点击画布关闭并开始绘制；关闭后恢复焦点到胶囊，点击外部时不抢回焦点。动画在挂载后测量真实起终点，关闭前重新测量以适应缩放与窗口大小变化；快速反向切换沿用当前变换，组件卸载时取消待执行帧，系统开启减少动态效果时跳过明显位移过程。

完整面板按实际画布宿主限制宽高；胶囊保持统一高度，收起宽度按摘要内容测量；快捷操作在同一表面向右、向下同步展开，窄空间内换行；必填输入与候选操作仅在胶囊展开后可见，默认只保留头部；展开区与头部共享背景、边框和圆角，不绘制独立卡片。关闭动画以当前胶囊头部为目标，退场中的完整面板不接受焦点或输入。组合输入期间不处理 Esc。

`workbenchContextToolbar` 只从现有状态派生呈现资格：活动 Mask／交互式 AI 优先，种子采集期间不显示竞争工具栏；无待处理编辑时显示追踪审阅，其次是适用的二次推理和能力恢复。后台追踪结果不能自动抢占编辑。临时抑制仅卸载工具栏呈现，保留组件内的参数、配置展开状态和请求保护。

二次推理的 mutation variables 固定来源任务与标注，成功结果只更新来源任务缓存；每个任务／标注有同步提交保护，旧对象的完成通知不会套用到当前选择。交互式 AI 在任务、工具、视频帧或精修来源变化时重置呈现，修改模型与参数仍留在当前设置面板。追踪审阅按任务／作业重置呈现，不按范围 revision 重建；待提交 intent 和人工帧覆盖确认继续由原审阅组件管理，呈现被其他编辑工具隐藏后不弹迟到确认。

交互 AI 的摘要按五种工具的有效能力派生：点与笔迹可显示正负状态，示例框遵守 `negative_box` / `text_combination` 门控；普通框提示与魔法收紧不使用残留极性。摘要只读取现有 owner 的文本、几何与候选状态，不新增会话状态。`ContextToolbar.panelSize` 允许交互 AI 与二次推理使用紧凑设置宽度，其他消费者保留原有宽度；必填字段和候选动作在主区，后端、模型、档位和诊断在“模型与参数”折叠区。

AI 示例框的置信度控件同时用于胶囊快捷区与完整参数区，由原有会话回调统一更新。二次推理快捷区按模型参数的 `x-platform-role: confidence` 筛选可编辑数值字段，兼容已有置信度键；它与完整表单共用按模型保存的参数，更新单个置信度时保留其他参数。二次推理采用与交互 AI 相同的紧凑完整面板和图标关闭按钮，收起摘要显示能力与当前文本。
