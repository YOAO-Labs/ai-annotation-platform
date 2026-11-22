# 0181 — 工作台的交互归属、帧身份与渲染边界

- **Status:** Accepted
- **Date:** 2026-11-21（回填现行实现的约束）
- **Deciders:** core team（Web Workbench、视频媒体、标注与 3D 渲染维护者）
- **Supersedes:** —

## Context

工作台复用任务队列、审核、偏好、快捷键与讨论，但图片、视频和场景有不同的编辑事务、媒体身份和绘制时序。屏幕上“选中了对象”“暂停在某帧”“显示了修改”都不等于有写入权、画到了指定像素或已接受持久化。反向推断会导致误写下一题、画面与标注错帧、撤销后的离线创建复活，或为浮动面板创建第二个 GPU context。本记录承接局部实现约束，不替代既有领域协议，也不把已知限制写成已修复行为。

## Decision

[按源码路径查阅局部决策与理由](../evidence/0181-decisions.tsv)。编号对应原始实现位置，相关 ADR 仍拥有其领域合同。

下文源码短路径相对 `apps/web/src/pages/Workbench/`。

### 1. Shell 只装配，任务、面板与命令各有一个 owner

- [0017](0017-workbench-shell-mode-and-stage-adapters.md) 所定 Shell/Mode/Stage 边界继续成立。`state/useWorkbenchShellModel.tsx` 组装窄命令，`modes/` 持有标注或审核动作，`shell/WorkbenchStageHost.tsx` 与 `stages/{image,video,three-d}/` 适配媒体，不复制队列或授权。3D stage 懒加载，图片/视频首屏不带 Three.js。任务锁和审核流转以 [0005](0005-task-lock-and-review-matrix.md) 与服务端授权为准；人工纠错和调用推理是独立权能。点云质量治理看当前项目权限，不从全局 reviewer 角色推断可写。
- `state/taskNavigation.ts`、`state/useWorkbenchTaskFlow.ts` 持有 latest-wins 的离开流程：按钮、热键、URL/通知和时间线共用离开检查；每次异步决策后检查取消信号以及原任务，再提交 URL/任务。迟到 URL 是旧意图时不反向切题；同项目通知链接由 URL 入场处理，不弹两次离开提示。视频“选中对象并跳帧”是一个受草稿守卫的动作（`state/videoSelectionCommand.ts`）。浏览另一人的讨论不等于领取其视频段或改租约。
- `state/workbenchInteractionGuards.ts` 和 `state/useWorkbenchHotkeys.ts` 在实际入口执行守卫，不以按钮禁用为代替。对话框刚开启但 portal 未挂载、或关闭后同一键事件还在冒泡时，后台仍让出事件；输入、IME、弹出菜单保留原生退格/方向键/Escape。`state/hotkeys.ts` 的稳定命令 ID、`state/hotkeyBindings.ts` 的物理按键和互斥上下文共同决定调度；录制键位隔离后台，固定命令也参与冲突检查。只能在可证明不相交的上下文复用组合键；冲突不执行、消费事件并诊断一次，坏版或未知覆盖留给用户修复。裸数字 `0` 仍是第十类别；实际尺寸/三正交视图重置使用 Shift + 物理 `Digit0`。输入区的属性数字键须有明确焦点范围；3D gizmo/删除键由 3D 局部处理，全局切题与帮助仍可用。用户文档生成器读取命令和设置字面量，不把章节注释放进运行时或生成协议。
- `shell/WorkbenchOverlays.tsx` 区分 video 已换算好的固定屏幕锚点与 image 几何锚点，避免二次 transform。弹出的 class picker、工具栏、拖动浮窗须同时保持 portal 的外部点击判定、焦点回收和画布点击穿透；不能仅凭 `z-index` 重建命中关系。`shell/toolDockOverflow.ts` 的宽度预算与真实 DOM 的标签、分隔线、分组间隙一致，优先留当前工具和选择工具。图片/视频可模糊 backdrop，3D 对话框只调暗；不要让软件渲染环境的合成清理卡住下一次打开。引导的已读来自显式确认，并随项目/版本归属，不以打开面板推断。

### 2. 偏好、布局与讨论恢复不以挂载为身份

- [0072](0072-workbench-dockable-layout.md) 规定 `state/useWorkbenchWorkspaceLayout.ts` 是自动布局快照的唯一写者；`state/useWorkbenchNamedPresets.ts` 独立写 `workspace.namedPresets`、对照 revision、保留未知项，不能用当前 layout patch 覆盖用户配置的其余上下文。快照是恢复输入，`layout/workbenchLayoutSnapshot.ts` 与 API `apps/api/app/schemas/workbench_workspace.py` 共守版本、允许面板、归一整数浮框边界。只有确认缺少 live 上下文时才一次性从旧偏好导入；损坏、较新版本和加载失败走显式恢复，不能写默认值覆盖它们。离开时脏快照归原上下文结算，旧响应不能进新上下文。
- `layout/WorkbenchDockWorkspace.tsx` 和 `layout/workbenchLayoutExecutor.ts` 只在初始化恢复整树；运行时移动原有 panel，画布 DOM 不 remount。隐藏 parking 分支不占布局但保留回泊几何；紧凑模式投影不写回桌面布局。序列化以真实归位后的 grid 为准（最大化是表现态，Dockview 移除最后一个子项可能留下零尺寸枝）；重放先冻结祖先尺寸和当前宿主测量、再有序解除约束并重测，避免内部通知先于尺寸上限更新。HTML5 dragover/drop 由可停靠 DOM 收到；Shift+pointer 浮出与普通画布输入走原交互面，不把整层 overlay 长期设为不可命中。仅真实窗口收缩才可夹紧并更新已存浮窗位置，初始化测量／HMR 不是用户移动。
- `state/useWorkbenchConfig.ts` 与 `state/useUserPreferences.ts` 共享按用户的读取。合并顺序是默认 → 个人 → 非空项目覆盖；只提交个人值，显示用的锁定覆盖、分组标签、本机实验开关均不是个人偏好字段改名。仅首次成功读取后允许写；后续刷新失败不抹掉已有快照。已接纳的写入不因关设置弹窗而取消；防抖、卸载 flush、失败反馈、缓存回灌必须按原账号/修订结算。`shell/AttributeForm.tsx` 捕获创建草稿的最新字段以便立即提交；已有对象以离开整表或一次有效防抖结算，不将 Tab 切到下一字段视为离开。Schema 参数表只实现所用的 Draft-07 子集，reset 不写只读或由 AI 面板拥有的 variant 轴。
- 旧渲染偏好的防抖写尚未完全绑定到发起登录会话；跨标签页换账号时不能假定卸载 flush 仍持原凭据，见 [#180](https://github.com/YOAO-Labs/ai-annotation-platform/issues/180)。设置文档默认值另有 [#161](https://github.com/YOAO-Labs/ai-annotation-platform/issues/161)；`WORKBENCH_SETTING_FIELDS` 和展示字面量可被生成器读取，运行时本机初值以实际解析器为准。
- `state/DiscussionDraftProvider.tsx`、`state/useDiscussionDraftStore.ts` 与 `state/useAuthenticatedDiscussionSession.ts` 以 tab 登录会话及完整目标为 owner；同用户重新登录也换代，短期续 token 不换代。`shell/CommentInput.tsx` 控制 contentEditable、mention 芯片及选区；composition 期间不拿 React 值覆盖 DOM、不拿 Enter 提交，compositionend 读最终文本，重挂载按原目标恢复。`state/useCanvasDraftPersistence.ts` 与 `state/discussionCanvasRecovery.ts` 管原目标的限时恢复；未加载不是空草稿，禁用/满额存储不阻塞内存编辑，不认领无主旧数据。挂载视图不拥有保存回调；有效迟到完成归旧目标，切换后不能附到新对象。hover、正在绘制、固定标记有独立优先级，不随面板切换制造新的目标。
- `shell/CommentsPanel.tsx`、`shell/DiscussionIssuesTab.tsx`、`state/useDiscussionNavigation.ts` 区分读取范围、可写目标与定位激活：不把 AI 候选和乐观临时 ID 发给持久 annotation API；讨论编码、能力和 scope 在写入端再次校验。服务端先做状态过滤再分页，未处理角标用准确总数而非已加载行；读权限缺少写能力时依然可定位。pin/深链是一次性激活，可跨列表筛选与分页，但普通打开浮窗不重放旧请求；保留原目标/意图并在每次 await 之后重新确认，确认期间消失的选择不参与批量删除。

### 3. 离线与历史：确认依据是原事务，不是乐观显示

- `state/offlineQueue.ts` 的权威值是 IndexedDB 已提交的事务；模块内写串行，跨 tab 重放锁跨越读取、发请求和可靠出队，成功提交才更新缓存、订阅和跨 tab 通知。提交/退出 gate 使用 `countDurably`，存储错误是未知而非零；抽屉与状态条的缓存计数只是展示。ownerless 历史行保留但任何当前账户均不认领；按已验证账号、项目/任务范围读取，并在每项发送前重新检查租约与权限。永久拒绝与临时传输失败分别处理；失败项保留并计数、停止有序 drain，删除目标已不存在可结算，不能把一般冲突或业务拒绝认作离线。
- 视频几何、类别和轨迹属性只有服务端成功或 `enqueueDurably` 提交后才记入历史；写入失败显示「本机保存失败」和针对原编辑的重试，不把 HTTP 409 放入队列。撤销未同步创建与重放互斥，等待原账号、任务和片段的队列删除提交，才移除临时标注；若已同步，按持久化的 ID 映射撤销服务端对象。重放在同一个 IndexedDB 事务中保存映射并替换后续 update/delete 使用的临时 ID；退出时同步以及切换任务后的重放也保存映射。抽屉清空仅删除确认时展示的条目，失败不报告清空。
- `state/useAnnotationHistory.ts` 的栈按账号、任务与适用的视频片段隔离。同一历史不能同时回放两次。普通批量撤销逆序、重做正序，每完成一项即保存实际结果；遇到失败立即停止并显示已完成数量，刷新或返回原任务后可重试剩余项，反向只回放已完成项。不能把普通批量当作原子操作。切割遵循 [0074](0074-atomic-annotation-slice-restore.md)，原生 Mask 采纳遵循 [0053](0053-native-mask-ai-candidate-lifecycle-and-video-correction.md)，视频恢复边界见 [0160](../0160-coordinated-video-restore.md)；这些原子恢复先保存版本及幂等键，失败不移动栈。新编辑不能因旧撤销完成而恢复已失效的重做记录。
- 待同步创建在发送前持久化「已开始同步」；若服务端可能已接收但本机结算失败，不能将它当作从未同步的草稿取消。使用原幂等键重新确认后，同一事务删除创建行、替换依赖项 ID 并保存映射，再允许撤销服务端对象。

### 4. 图片与通用几何：坐标、绘制、命中、编辑与存储分开

- [0004](0004-canvas-stack-konva.md)、[0040](0040-shared-annotation-visual-spec-not-stack-merge.md) 确定 Konva 分层与共享视觉规格；`stage/annotationVisual.ts` 共享参数，图片和视频分别应用自己的坐标、更新与性能模型。状态持久化为归一坐标，绘制使用自然媒体像素，视口 transform 最后应用；描边、边角手柄、标记保持屏幕尺度，Canvas 字体/颜色需解析成真正可绘制的值。`stage/ImageStage.tsx` 按媒体身份而非 URL 变化重新 fit；尺寸可信时在加载前 fit，显示前同步 draw 避免变换旧帧闪现；坏图也到达可交互的降级状态。经鉴权的原图和金字塔由 `useWorkbenchImageSource`、`useImageTileScheduler` 等持有（[0062](0062-immutable-image-pyramid-assets.md)、[0063](0063-konva-viewport-image-tiles.md)、[0064](0064-task-scoped-raster-resource-coordination.md)），仅续签 URL 不清已解码 tile；绘制时确认 bitmap 未被 LRU 淘汰。不要从绝对 URL 拼鉴权下载请求。
- `stage/ImageStage.tsx` 区分事件时点草稿和 React 画面。显示优先用当前拖拽/微调、待提交覆盖，再用持久几何；快速 pointerup 用最后事件坐标，不等 RAF；跨边界拖拽、停止捕获和 mouse fallback 同一个手势结算。Shift 约束比例、Alt 围绕中心；父对象 Alt 拖动时取 clamp 后的真实差值应用于当时仍关联的直接子对象；不同拓扑不借 bbox 近似写回。Issue 图钉消费同份当前几何但只有图钉/布点状态拦截画布，候选命中不能在普通绘制手势中抢选。`stage/shared/viewport/` 把 fit、zoom、裁剪和逆变换作为纯几何供 overlay 和事件调用，不在 DOM 与 Konva 维护两套坐标。
- [0021](0021-polygon-lod-and-spatial-index.md) 限定多边形 LOD 与顶点索引仅用于表现；大图可视裁剪不改持久顶点，选择和编辑用原几何。`stage/ImageStageShapes.tsx`、`stage/shared/geometry/evenOddFill.ts` 以真实多环偶奇填充，显式空孔数组覆盖继承，multiPolygon 实体胜过兼容用主外环；无效环不制造伪洞。仅有 points 的编辑器不得无损承诺带洞/多组件结构，几何指标缺媒体尺寸时只展示可信相对值。`stage/shared/geometry/` 的快速自交检测以原环干净为前提；闭合环和开放线的完成、吸附、切割与预览不同，局部剪切成功也不等于服务端验证并集、面积守恒和版本通过。遮挡/交叠诊断可以明确降级到 bbox 近似，不能让近似覆盖实际形状或像素命中。
- 视觉上临时 `pending` 覆盖桥接清掉拖拽状态与异步 cache/mutation 回调的时间差；不要先直接写查询缓存，否则 mutation 会取得修改后的“旧值”，失败便无法回滚。缓存命中或 mutation settle 正常终结，时间限制只防悬挂。UI 层 CSS 保持 overlay 叠层和点击穿透，不移植成依赖任意内联 style 的布局；CSP 历史见 [0010](0010-security-headers-middleware.md)。

### 5. 视频：一个源帧时间基准，一个呈现 owner

- [0018](0018-video-frame-service-segments-and-tracker-boundary.md)、[0041](0041-video-canvas-unify-to-konva.md)、[0066](0066-video-segment-fragments-and-canonical-tracks.md) 分别约束分段媒体、单 Konva transform 和 canonical track 的读写真相。`stage/useVideoPlaybackController.ts` 持有播放/定位；`stage/VideoKonvaStage.tsx` 在帧确定后由 `stage/videoFrameViews.ts` 解析当前 entry，再计算该帧 QC（极小框、同类高重叠，另计关键帧间隔），不存在“Konva 没有重叠检查”的限缩。`stage/VideoKonvaMediaLayer.tsx` 隐藏 `<video>` 只喂底图，播放只重画媒体层；暂停优先同帧精准位图，resize 也重新绘制静止帧。Konva、minimap 与截图共用所选画面来源；降级原生画面可显示/截图，不宣称精确帧已经上屏。
- `stage/useFrameClock.ts`、`useVideoPlaybackController.ts` 区分浏览器媒体时钟、受控 frameIndex、raw seek 目标以及已绘制帧，frameIndex 是源帧而非 React 绘制次数或时间轴格。主动跳帧先登记意图，迟到的 seek/播放/preview 回调不能把暂停后的键盘步进、主机跳转或循环区间倒回去。`stage/videoStageControls.ts` 的 checked seek 要等实际媒体层绘制所请求像素；restore 还等布局、选中与 fit，内部恢复不冒充用户命令。设置可暂停不吸附到别格，调查黑屏需区分源租约、metadata readiness、精确解码与 paint acknowledgement；原生 `HAVE_NOTHING` 静默停滞由有界 `load` 恢复后报告失败，不能无限重试。
- `stage/videoSamplingGrid.ts` 只改变展示导航，以源帧 0 为网格起点，末端不是格点时普通步进不会悄悄跳至末帧；Shift 微调仍逐源帧。密度与预测、marker、剪辑范围共用可见窗口；同长度换媒体也重置窗口。`stage/VideoPlaybackOverlay.tsx` 展开和收起都把 wheel 监听绑给当前 root，Ctrl/⌘ 缩放不把普通滚轮都吞掉。普通拖曳仅在章节布置时成为 range 选择，否则 seek；循环与传播刷选用 Shift 拖曳，章节 resize 只在松手保存。界面“几格”在提交时换算为源帧 span。人工/AI 密度用相同计数尺度，轨迹贡献按实际份额着色，未归属框不伪造 track。
- `stage/useVideoPreciseFrame.ts` 使用原 manifest 代际下的不可变 compact 输出身份持有采样表和字节；URL 只是有期限的取数能力。续签同输出可复用 offsets/bytes，输出换代须淘汰原数据；相同目标的授权恢复有界。原始字节 TTL/LRU 有字节预算且活动读取持有引用，不靠 inactive query 无限保留。静止缓存命中可先于 raw seek 结束展示，但 GOP/PTS 诊断、paint 回执必须是同一帧；未命中在请求仍有效且 seek 稳定后启动，预取只在可见暂停状态按方向在同 chunk/GOP 有界工作，不为预取打断前台 decoder。
- `stage/videoChunkDemux.ts` 保持 packet decode order，以 presentation PTS 认目标；`stage/videoGopDecoderSession.ts` 每会话按 decoder 配置/GOP/代际串行，I/P 前进只提交增量，B 帧有界 lookahead；已越过而无缓存则从 key sample 重新解，不能靠每帧 `flush()` 消费 delta。仅 GOP 尾缺目标输出才 drain，之后下次输入需要 key；flush 阻塞缺口见 [#168](https://github.com/YOAO-Labs/ai-annotation-platform/issues/168)。`stage/useVideoChunkDecoder.ts` 管缓存与资源转交而不负责 fetch/demux；decode 回执不等于 `showFrame`。当前显示位图 pin 在 LRU 外直至替代帧提交，以免预算收缩释放正在画的 bitmap；过大的待显示前台项最多一个，预取不走例外。退出/换代时关闭晚到 `VideoFrame`/bitmap；共享 query 用其自身取消信号，以免一个消费者退出断开其他读者。浏览器依据运行能力与设置走 WebCodecs 或本机视频/位图 fallback（[0060](0060-default-enable-capability-gated-client-acceleration.md)），不拿用户代理、UI 时间戳或缓存标签证明实际解码或硬件加速。
- `stage/videoStageGeometry.ts`、`state/videoTrackCommands.ts` 分清跨帧对象与某帧形状（持久 track ID 见 [0045](0045-track-id-as-annotation-column.md)）；显示号由首关键帧和 ID 稳定排序，覆盖不同几何的共同序列，不作持久主键。显式 outside 隐藏形状，插值帧人工编辑物化一帧并从 outside 区间仅移除该帧；开放折线保持端点顺序，闭合多边形对齐环，不能用外接框替代实形。bbox 的帧属性只覆盖 schema 标为 mutable 的键，其余值继承轨迹默认；outside 帧不能设属性。普通复制传播只复制方向范围内合法目标、不含源帧，默认不盖已有关键帧，以一条整体命令撤销；AI tracker 与此手工命令不同。参考 ghost 用于定位/续写，不是该帧持久几何；锁定轨迹不供续写，已有关键帧的编辑不自动跳下一个。跨帧选择 roster 持有批量、画布当前帧选中独立；同类两轨迹才合并，gap join 另要求可见范围不重叠。`stage/videoKonvaInteraction.ts` 的整体平移须移动同坐标系所有环，而非只写外环（未修缺口 [#167](https://github.com/YOAO-Labs/ai-annotation-platform/issues/167)）。
- `state/useTrackerSeedCollection.ts` 持有传播对话框、种子与章节刷选生命周期，`state/useVideoToolCommands.ts` 管临时工具准入，恢复和订阅归 `apps/web/src/hooks/useVideoTrackerJobs.ts`。停放对话框不是归还草稿，但不占画布；重开清掉旧刷选，刷选范围覆盖预设直到方向/预设改变。启动 job 后用原 dialog 呈现进度，结果就绪才转 review；迟到的旧 job 预览不能重开对话框、刷图或发当前题通知。认证尚未 hydrate 时恢复可在凭据就绪后重试，不能创建重复订阅。素材和执行权仍服从 [0110](../0110-retained-tracker-video-inputs.md)、[0157](../0157-video-execution-claims-and-route-binding.md) 与 [0158](../0158-video-window-journals-and-replay.md)；预览、接受和写入不是同一步。Polyline 支持人工关键帧与弧长插值，但 AI 传播明确禁用、接口返回 400，不降级成其他几何；[项目决策](https://github.com/YOAO-Labs/ai-annotation-platform/blob/main/docs/plans/archive/2026-07-12-v0.21.32-polyline-track-ai-propagation.md) 和 [#56 的状态说明](https://github.com/YOAO-Labs/ai-annotation-platform/issues/56#issuecomment-5564738978) 明确这是能力边界，不是下一版承诺。

### 6. AI 与 Mask：候选、像素、操作历史各有归属

- `state/useBackendRouting.ts` 以可达且匹配同一 model row 的能力决定工具与合法候选，依首选、项目默认、注册顺序选择；不能合并两模型拼出 capability。`state/useBatchBackendSelection.ts` 单独管理批量选择，同项目的手选不被跨 tab 默认值或列表重排抢占。`state/useInteractiveAI.ts` 以请求开始时的 task/frame/媒体、提示和模型参数持有结果；新请求或换题使旧结果退役，从缓存快速命中不让旧慢请求盖回来。候选与 annotation 分层：SAM 文本草稿、Magic Box 外接矩形、图片 polygon 和原生栅格不能串用写入；视频推理固定提交拍下来的源帧 JPEG，不拿 MP4 URL 或手速更新后的帧重试。能力缺失须明确降级或禁用，不自行补默认模型；类别归候选对应 tool unit，无自己的类别时用 `__unknown`，不借另一工具类别。依据 [0019](0019-prompt-first-tooldock-1n-arch.md)、[0020](0020-ml-backend-capability-negotiation.md)、[0026](0026-tool-unit-class-and-attribute-binding.md)、[0043](0043-staged-preannotation-pipeline.md) 与 [0050](0050-ml-backend-service-pools-and-request-routing.md)。
- `state/sessionVariantCache.ts` 只是 session 级冷启动显示提示：键为 backend 与排序过的 variant 轴；异步成功可推测热，同步 `useInteractiveAI.ts` 收到 `cache_hit` 明确值才更新，false 清掉热提示、缺省不写。当前正使用真信号，不能拿此 UX hint 当调度/驻留保证，也不能按改配置后的值给进行中的请求标冷/热。`state/transforms.ts` 保留原预测多组件、孔和来源；兼容编辑主环不代表全部像素，接受使用快照而非 UI 当前选择。
- [0022](0022-mask-editor-tool-architecture.md)、[0048](0048-video-raster-mask-content-addressed-rle.md)、[0052](0052-shared-raster-mask-and-image-geometry.md)、[0053](0053-native-mask-ai-candidate-lifecycle-and-video-correction.md) 定义编辑工具、内容寻址 RLE、候选接受与局部纠错。编辑器的笔触松手只形成草稿，选中已保存 Mask 上的智能 scribble 是提示而非直接改像素；`state/canEditMask.ts` 在 pointer、热键、工具栏和提交执行同一 task/对象/轨迹/segment 锁与 phase gate，Enter 非脏时不能物化 held frame。`state/useMaskEditorSession.ts` 管草稿与资源，`state/useMaskMutationWorkflows.ts`、`state/maskMutationPolicy.ts` 保留预览时版本和范围，拒绝把旧预览 fall through 成普通像素更新。`state/useVideoMaskCorrection.ts` 快照原 annotation/帧/session/segment，save 单飞，异步边界重查归属；冲突/保存失败保留可以重试的原草稿，过期结果不能重绘换题后的画布。不知道服务端是否支持原生 Mask 就 fail closed，不能把洞、断开组件或 lossy 转换静默存成外环。格式/qc 以 [0055](0055-mask-quality-and-format-contracts.md) 和 [0111](../0111-independent-mask-content.md) 为准。
- `stage/shared/useRasterMaskRecords.ts` 划分已提交、候选、对照及编辑租约；内容恒等时可共享**成功**读取，权限/存在性失败由各端点判定，不因 sha 相同扩展授权或负缓存。过滤仅放低优先级、解除 pin；换 owner 清除资源，revision 更新才失效对应缓存键。真正命中用原像素，不用 max-pooling 概览；z-order 相同由后者在上。`stage/shared/geometry/maskBuffer.ts` 是与 DOM 无关的二值像素与半开脏矩形；`stage/overlays/MaskOverlayLayer.tsx` 遇新 buffer 或 opacity 字节变化全画，普通笔触只 blit dirty ROI。采样插值保留完整笔迹，不能把像素写入也按 RAF 丢弃。形态学填洞保持自己的 4 邻域合同，矢量导出必须分别检查前景与背景连通性、掉落组件和变换损失；比较以有 halo 的两位 tile 比全图展开安全。
- [0054](0054-raster-mask-large-canvas-memory-and-tiles.md)、[0056](0056-raster-mask-persistent-client-compute-session.md)、[0057](0057-raster-mask-webgpu-packed-xor-contract.md)、[0058](0058-raster-mask-immutable-packed-base-cache.md)、[0059](0059-raster-mask-dense-word-scatter-over-sparse-compaction.md)、[0061](0061-raster-mask-packed-cpu-fallback-and-webgpu-circuit.md)、[0065](0065-keep-one-pass-raster-mask-webgpu-kernel.md) 是稀疏 tile、固定 Worker 池、packed/XOR 与 CPU/WebGPU 选择的权威。`stage/shared/rasterMaskWorkerRuntime.ts`、`rasterMaskWorkerPool.ts`、`rasterResourceCoordinator.ts` 和 `maskHistory.ts` 共同限定任务资源预算：编辑/可见请求优先；撤销只有预算准入才入栈，tile 的初始状态仅首次触及保存，空变更无需条目。字节有限、未对齐 word 尾部、非零偏移和 tile seam 保证 CPU/GPU parity；大型 ROI 也不能把兼容性实验替换成生产路径。设备丢失或不支持能力时回到可用 CPU，有界释放 worker/session/GPU 引用，不能只验 shader 写出而遗漏保存、撤销、重载。

### 7. 3D 与共享 surface：几何权威不在浏览器 renderer

- [0031](0031-dual-canvas-konva-three.md)、[0032](0032-3d-box-editing-main-view-gizmo.md)、[0033](0033-3d-to-2d-projection-overlay.md) 与 [0071](0071-persistent-multicamera-annotation-members.md) 分开世界 box 形状、实时相机投影与人工相机成员；投影不存回为 2D 标注。`stages/three-d/geometry/axisConvention.ts` 在入口把已知轴系转 ISO 8855（前 +X、左 +Y、上 +Z，米），点位置和标定外参配套换基，raw 保持源约定，不在下游重复旋转。`box3d.ts` 的 PSR 尺寸是盒局部全边长，Euler 用 Three.js `XYZ`，角点索引底环/顶环同序，`worldToBox` 归一边界为 ±0.5，自动拟合另用局部米。
- `stages/three-d/geometry/projection.ts` 用行主序外参、可选矫正、内参、透视除法；`w > 0` 只指相机前方，不指落在画面中。着色、深度栅格与种框共享投影合同；反投影使用同一 `w` 深度，奇异矩阵、无效值、非正深度不给结果。`CameraProjectionView.tsx` 用原图像素画两端都在相机前方的投影边，不主动做近裁面求交；缺标定只保留相机图，反选用可见角点包围盒、重叠优先小框。
- 相机矩形种框以原图像素复用前向投影取点，只写 `box_3d`；没有可用簇时沿矩形中心射线估计放置并提示，不保存种子矩形。当前最近点加固定带宽可能丢掉有效目标或混入背景（[#166](https://github.com/YOAO-Labs/ai-annotation-platform/issues/166)）。主视图拖矩形用实际屏幕投影选点，不取地面 footprint；严格点选无命中不造点，只有点击放置可用地面估计。`autofit.ts` 尺寸拟合重新居中，底面按世界最低点（竖直盒假设），实验 yaw 只拟合轴、不判前后；稀疏点保持原值，拟合不充当对象检测。
- [0035](0035-scene-and-frame-foundation.md)、[0069](0069-scene-track-domain-and-lifecycle.md) 定义场景帧和 Scene Track。`stages/three-d/SceneTimeline.tsx` 与 `useScenePlayback.ts` 只自动推进可达帧，等目标 task/点云就绪；短暂未知身份不作无 scene，超时／端部缺帧须退出。相邻帧点云和框使用 `inverse(T_current) × T_neighbor`；缺 pose 不将邻帧点冒充已对齐。`geometry/cullDynamicPoints.ts` 测试已对齐 OBB，却输出邻帧坐标交 renderer 变换；`geometry/perObjectAlign.ts` 以 track 配对后已输出当前坐标，必须用恒等渲染。邻帧引用不纳入当前对象拾取，取数窗口与可见范围分别确定。点索引/抽样 stride 绑定实际帧，显示数据不定义训练料的原始通道或 sweep。
- `stages/three-d/ThreeDWorkbench.tsx` 与 `usePsrPatchPipeline.ts` 持有编辑事务。播放时第一个编辑意图只暂停；gizmo、相机、三正交视图、PSR 数值和快捷键使用同一写入守卫。拖动期间只同步草稿和投影，松手一次提交；主 gizmo 的 React 草稿不能回馈并重置正在拖的控件，取消不能把旧几何恢复到新选择。无效 PSR 输入要求撤销旧防抖；有效待写可归原目标 flush，普通回显不盖正在打字的值。`TriOrthoView.tsx` 拖动冻结取景、不冻结裁剪；命中优先旋转、角、边，并按局部轴和视图手性换算，单边拉伸保持对边固定。窗口监听允许拖出视口；锁定仍允许浏览缩放。标记文字不挂在非等比缩放盒中，拖后合成 click 不重复选中。
- [0068](0068-experimental-webgpu-pointcloud-renderer.md)、[0070](0070-single-context-pointcloud-four-view-rendering.md)、[0073](0073-shared-surface-for-3d-docking.md) 规定 `PointCloudScene.ts` / `usePointCloudScene.ts` 持有唯一 renderer、canvas 与点几何，`PointCloudTriViewPass.ts` 只是附加 pass。停靠只迁移 surface，不重建 renderer 或输入宿主；事件刷新合并在同一 RAF。WebGPU shared surface 清除非 scissor 局部，只变化正交视图也先画主视图；`layout/workbenchViewportRegions.ts` 的可见区域供 DOM/GPU 同用，重叠遮挡不能重新开洞。WebGPU viewport 在界内，借 view offset 保持裁剪前的取景，不放大已裁剪内容。CSS 客户端与 GPU 原点/DPR 分开，位置变化也重测，不因几何刷新清选择。
- 按 geometry generation 使用真实点几何和六裁剪面预热，编译未完不画三视图，结束按当前代际 invalidate。`ClippingGroup`、counted sprites 和画布尺寸补偿不能退回固定一像素原语；旧 WebGL 的 Worker 着色与 WebGPU 的纹理采样共守投影、可见性与深度，切模式不能把临时高度色当 RGB 已提交。`rendering/pointCloudRenderer.ts` 区分请求模式与真正 `webgpu` / Three.js `webgl2-fallback` / legacy；初始化失败与 device loss 由 owner 回退并释放资源。
- `pointCloudAssetCache.ts`、`useOriginalAssetOwner.ts` 与 `apps/web/src/api/originals.ts` 按认证会话加内容身份共享取数和解码。即使同账号，新登录也换代，续签却不打散同内容缓存。仅核验过的站内 original URL 能带凭据，不能发给供应商/测试资源；取消投机读不能取消已被前台加入的共同请求，晚到结果不能记入新 scene 的像素或指标。

## Consequences

- 正向：切题、恢复对话框、联动画布、播放和提交都有可验证的边界；同一底图源与共享 renderer 不因布局改变被重建。缓存、采样和代理帧只减开销，不改变持久几何和源帧身份。
- 负向：守卫可能拒绝已经过期的确认，或将操作延迟到目标真正就绪。浏览器降级可能没有精确位图、完整投影边或 GPU 能力，不得冒充有真实像素或适配器证据。普通批量历史、离线兼容、相机深度启发式、视频洞平移、GOP 尾部终结和旧偏好写入仍有上述缺口，不因迁移注释变成已修复。

## Alternatives Considered

- 拆出两套审核和标注页面，或把领域状态全部收回 Shell：会复制命令、异步守卫与写入 owner；保留窄装配与各域 owner。
- 以已显示的界面或媒体时钟确认接受：无法识别持久化失败、迟到结果、错帧与权限变化；采用事务回执和像素上屏确认。
- 为每个 3D 面板创建 renderer，或将视觉投影、ghost 和插值视图当作用户标注：增加 context 与同步负担；保留共享渲染和纯派生。

## Notes

- 相邻回归入口包括 `layout/WorkbenchDockWorkspace.test.tsx`、`state/useWorkbenchWorkspaceLayout.test.tsx`、`state/__tests__/discussionDraftLifecycle.test.tsx`、`state/offlineQueue.test.ts`、`state/useAnnotationHistory.hook.test.ts`。Konva mock 能检查节点、坐标和事件布线，不能证明真实像素、合成或浏览器性能。
- 实际浏览器验证须记录硬件、浏览器、真实 backend、viewport/DPR 和 fixture。视频比较源帧解码像素与标注对齐，区分 WebCodecs 与硬解；Mask 比较同 ROI 的 CPU oracle、undo 与重载；点云从安全应用 origin 获取 adapter/fallback，分开 cold、相邻、全 warm 和 tri-view 初开/再开，并在设备失效验证释放旧资源与恢复数据。[WebGPU/视频工作台](https://github.com/YOAO-Labs/ai-annotation-platform/blob/main/docs/research/21-webgpu-video-workbench.md) 和 [macOS 渲染调研](https://github.com/YOAO-Labs/ai-annotation-platform/blob/main/docs/research/25-macos-webgpu-performance.md) 是历史证据，不代表当前资格。
