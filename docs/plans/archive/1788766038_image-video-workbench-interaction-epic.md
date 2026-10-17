# 图片与视频工作台交互改进多步计划

> Status: Done
>
> 规划日期：2026-09-07
>
> 代码基线：`cd2521f1`；规划前工作区干净。
>
> 调研输入：[Supervisely 与 CVAT 图片、视频工作台交互专项调研](../../research/23-supervisely-cvat-image-video-workbench.md)。用户指定的主工作区文件与本工作区副本内容一致。
>
> 用户确认范围：完整规划 A–H，优先交付 A–D。最新执行要求：各里程碑先拆独立草案，然后逐步实施，每个里程碑浏览器实测通过后提交；见 §15–16。A–H 的 16 个独立里程碑均已实施并通过浏览器验收，结果与证据见 §16 状态表；不预分配版本或发布日期。

## 1. 推荐路线与边界

按“高频操作 → 状态表达 → 视频上下文 → 审核定位 → 几何提效”推进，保持现有 WorkbenchShell、Mode Hooks、Stage Adapters、双层时间轴、原生 Raster Mask 和 Dockview 布局边界。

最小可交付方案是 **A + B**：修正候选快捷键并保证工具可达。推荐按用户选定的完整范围执行，**A–D 为首批**，因为这四步直接改善反复发生的选类、找工具和提交动作。每个切片都包含功能、相关文档和验证，合并后可独立使用，不依赖后续切片补齐基本功能。

范围包括：图片手工连续创建、图片与视频的工具溢出和 Mask/AI 主动作、视频轨迹与审阅范围、Issue 时间上下文，以及图片几何创建和受限 Slice。整体涉及显著超过 8 个文件，必须拆为多个可评审变更。

本轮明确排除：重写工作台或时间轴、3D 时序对象领域改造、模型训练与新追踪后端、多相机视频、插件工作台、管理员生产预设配置、跨设备同步生产模式、SAM 提示历史、完整视频 ghost 续写预设、Intelligent Scissors。SAM 提示历史与完整 ghost 续写预设不能因报告提及就视为已实现；它们分别有独立的会话历史和保存/跨帧调度要求。

不采用一次性重构成统一编辑器或统一 AI 执行状态机的方案：图片、视频、交互式 SAM、普通预测和追踪候选的提交粒度不同，展示层统一不能改变各自的数据所有权。

## 2. 相对调研的现状修正

调研基于较早提交，以下结论已按当前源码核对；实施使用本表的当前边界。

| 主题          | 当前代码事实                                                                                                                                                  | 本计划实际增量                                                                      |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| A：视频 A/D   | `dispatchKey` 在视频分支先处理 D 为 Smart Box 并返回；后面的候选接受/拒绝逻辑不可达。快捷键表仍将 Alt+3 同时写为 AI 和 Polygon                                | 增加候选上下文优先级，校正展示与生成文档                                            |
| B：工具坞     | 已按单帧、SAM、轨迹分组，但仍纵向全部展开，没有高度溢出分配                                                                                                   | 按真实容器高度分配常驻工具与“更多”                                                  |
| C：快速创建   | 2D 类别面板仍不可点击，`activeClass` 仍仅是预览/默认值；创建入口尚未统一接收属性草稿                                                                          | 会话级创建意图、缺失属性表单和同一保存入口                                          |
| D：Mask       | 已有阶段标签、高级操作、区域/实例预览和冲突恢复，底部仍有通用“取消/确认”                                                                                      | 从已有状态派生唯一主动作，共享按钮与键盘分派                                        |
| E：AI         | `ai-task` 与 `video-tracker` 已接入停靠面板；交互顶栏仍密集展示模型参数                                                                                       | 调整面板内部阶段与主次信息，保留现有布局                                            |
| F：视频轨迹   | 已有轨迹选中卡、sticky 提示与时间轴派生函数；追踪审阅范围仍有组件局部状态                                                                                     | 收敛轨迹条、显式作用范围和追踪审阅选择                                              |
| G：视频 Issue | 已有帧图钉、时间轴标记及按 `anchor_position.frame` 跳帧；普通创建框未写帧，视频落点创建未完整接线                                                             | 先补创建闭环，再扩展对象、视口、时间窗口及范围恢复                                  |
| H：Center Out | `ResizeHandles` 已有 Alt 中心缩放，自定义手柄并非 Konva Transformer                                                                                           | 只补中心向外创建，并沿用 Alt 语义                                                   |
| H：Slice      | Polygon 布尔运算能保留孔洞/多外环，但顶点编辑只支持简单单环；Join 是多次请求，客户端 batch history 不保证原子性；Mask `split_components` 不能切开一个连通分量 | 限定首版几何边界，新增受限事务与原子撤销；不能直接复用 Join 或冒用 split_components |

主要实现入口：

- [快捷键定义](../../../apps/web/src/pages/Workbench/state/hotkeys.ts)、[快捷键调用方](../../../apps/web/src/pages/Workbench/state/useWorkbenchHotkeys.ts)、[ToolDock](../../../apps/web/src/pages/Workbench/shell/ToolDock.tsx)。
- [Shell model](../../../apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx)、[创建动作](../../../apps/web/src/pages/Workbench/state/useWorkbenchAnnotationActions.ts)、[图片动作](../../../apps/web/src/pages/Workbench/stages/image/useImageAnnotationActions.ts)。
- [Issue owner](../../../apps/web/src/pages/Workbench/state/useIssuePins.ts)、[Issue 创建框](../../../apps/web/src/pages/Workbench/shell/IssueCreateModal.tsx)、[反馈 schema](../../../apps/api/app/schemas/annotation_feedback.py)。
- [中心缩放](../../../apps/web/src/pages/Workbench/stage/ResizeHandles.tsx)、[复杂几何保护](../../../apps/web/src/pages/Workbench/stage/shared/geometry/geometryEditPolicy.ts)、[Mask 事务](../../../apps/api/app/services/mask_mutation.py)、[操作账本](../../../apps/api/app/db/models/annotation_operation.py)。

## 3. 跨步骤不变量

1. **保留现有所有者。** 类别/工具/草稿归 Workbench 状态与创建动作；Mask、SAM、追踪作业继续各自管理请求和事务。新增状态条只派生展示，不复制完整运行状态。
2. **业务状态不进入布局快照。** 当前布局 schema 已为 `5`；本轮 A–H 不修改其 grammar，不把帧、选中对象、审阅范围或生产模式写入 Dockview snapshot。
3. **所有入口遵守同一写入限制。** 按钮、快捷键、连续创建、Issue 导航、Slice 撤销均检查任务、权限、锁、草稿和提交状态。Mask 的 `canEdit` 与 `canCommit` 分开，禁止继续编辑不代表禁止保存已有草稿。
4. **异步结果不得越过上下文。** 每次等待后核对原 task/session/generation；切题后的旧结果只能结算原任务，不能选中新任务对象或清理新草稿。
5. **数据与几何语义保持。** 原生 Mask 保留 RLE；视频 bbox 插值与 Mask 保持由现有领域函数决定。Mask 当前最近可见关键帧的选择规则（等距选较早帧）不在此计划中修改。
6. **使用现有主题与组件。** React/Tailwind 语义 token、`data-theme`、Lucide 和本地 UI adapter；新增展示位置不创建第二个 Stage，不因隐藏面板取消后台作业。

```text
WorkbenchShell / existing state owners
  +-- Mode / task lock / navigation scheduler
  +-- Creation intent --> existing annotation actions --> history / offline / API
  +-- Mask / SAM / tracker owners --> derived actions --> buttons / HUD / hotkeys
  +-- Issue focus request --> task navigation --> VideoStageControls
  |                                               +-- frame / viewport owner
  |                                               +-- timeline window owner
  +-- Dock workspace --> panel positions and visibility only

Slice UI preview --> restricted slice transaction --> operation / lineage ledger
                                                       |
Slice history command <----- atomic result / restore ---+
```

## 4. 交付顺序

规模用于比较工作量，包含实现、回归和文档，不代表已承诺工期。S 为局部修正，M 为数个前端所有者协作，L 为跨所有者或 API 合同，XL 必须继续拆分。

| 步骤 | 独立用户结果                                 | 规模 | 硬依赖                                      | 建议顺序                 |
| ---- | -------------------------------------------- | ---- | ------------------------------------------- | ------------------------ |
| A    | 视频候选 A/D 与界面说明一致                  | M    | 无                                          | 1，首批                  |
| B    | 短屏与缩放时所有可用工具仍能访问             | M    | 无                                          | 2，首批                  |
| C    | 选一次类别即可连续创建同类图片对象           | M    | 无；复用现有工具绑定和创建入口              | 3，首批                  |
| D    | 每个 Mask 阶段的 Enter/Esc 结果清晰          | M    | 无；与 A 共用键盘边界                       | 4，首批                  |
| E    | AI 主层只展示当前交互和决策所需控件          | M    | A；复用 D 的动作展示方式                    | 5                        |
| F    | 当前轨迹、作用范围和审阅范围稳定可见         | L    | F1 无；F2 依赖 B；F3 使用现有 tracker owner | 6，可拆 F1/F2/F3         |
| G    | 点击视频 Issue 能恢复错误现场                | L    | G1 无；G2 依赖 G1；不依赖 F 的 UI           | 7，可拆 G1/G2            |
| H    | 减少几何创建操作，并提供可预览、可撤销的切割 | XL   | H1/H2/H3 可独立；H4 的 Mask 入口复用 D      | 8，可拆 H1/H2/H3/H4a/H4b |

A/B 可并行核验；C/D 可在所有者边界明确后并行实现。若使用修改型代理，按仓库并行工作约定各自创建隔离 worktree，交付时合并当前 HEAD 上的变更。不得并发修改本工作区同一个 Shell model。

## 5. A：统一视频候选快捷键契约

**产品决策**

- 视频选中当前帧的普通待决 AI 候选时，A 接受、D 忽略；未选此类候选时，D 仍切 Smart Box，A 不执行候选决策。
- 人工轨迹、人工单帧对象、其它帧候选、已处理候选不触发 A/D 接受或拒绝。交互式 SAM 保留其 Enter/Esc/Tab；追踪作业候选仍按作业、目标和窗口审阅。
- 输入框、菜单、停靠标签、类别弹层和活跃工具自有按键先消费事件。按住按键不得重复提交同一候选。
- 自动前进只在当前帧未决集合内发生；失败保留当前候选，不能把未完成决策当作已决。

**实施落点**

在 `hotkeys.ts` 的 `DispatchCtx` 增加明确的当前候选上下文，由 `useWorkbenchHotkeys.ts` 基于过滤后的 `aiBoxes`、当前源帧和选中 ID 派生，不能用 `hasSelection` 代替。按钮与快捷键调用同一个候选决策入口，保持现有写入门控。修正图片 Polygon 备用键为 Alt+2；视频 Alt+2 仍为轨迹、Alt+3 仍为选择。

`useImageAnnotationActions.ts` 的接受/拒绝目前返回 `void`，因此本步还要让实际决策所有者返回“成功 / 失败 / 待补选类别”的可等待结果，统一处理 422 类别补选、在途 task/candidate 去重和拒绝失败后的候选恢复。只有同一上下文的成功结果才能推进选中；不能只修 dispatch 后继续使用当前的立即前进逻辑。

为受本轮影响的快捷键定义补上下文和目标信息，供提示与路由契约测试比较；只比较 `actionType=setTool` 不足以发现目标工具错误。保留现有帮助页搜索，更新 `docs-site/scripts/generate-hotkeys.mjs` 的解析与生成说明，不为所有历史手势引入新快捷键框架。

**验收**

- 路由矩阵包含图片/视频、待决候选/人工轨迹/无选中、当前帧/其它帧、输入焦点/类别弹层/SAM/Mask。
- A/D 每次只决策一个当前帧候选；开启自动前进后不会跳到其它帧；失败、连按和切题不会重复决策。
- tooltip、帮助页、设置说明、自动生成文档与实际 dispatch 一致；保留 J/K/L 播放及 O/Q/L/H 轨迹键位。

文件集中于 `state/hotkeys.ts`、`state/useWorkbenchHotkeys.ts`、`stages/image/useImageAnnotationActions.ts`、`state/workbenchSettingsFields.ts`、`shell/ToolDock.tsx` 和快捷键生成器。回归扩展现有热键及候选决策测试。

## 6. B：工具坞按容器高度溢出

使用浏览器原生 `ResizeObserver` 观察画布面板内的工具坞容器，按实际按钮、分隔和分组标题尺寸计算容量。仅在发生溢出时预留“更多”按钮空间；高度恢复时恢复原分组顺序。[ResizeObserver 文档](https://developer.mozilla.org/en-US/docs/Web/API/ResizeObserver)说明了按元素尺寸观察的能力；[CVAT 固定快照源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/control-visibility-observer.tsx)提供了溢出入口预留与移入弹层的参考机制。

**固定规则**

- 常驻优先级为选择工具、当前工具、其余工具的现有分组顺序。当前工具不重复渲染；低高度时选择与当前工具优先，其余进入可滚动菜单。
- 工具是否可用仍由任务能力、tool unit、AI 开关和禁用原因决定；溢出计算只处理已过滤的描述项。
- 主栏与菜单复用同一个工具描述和动作，不复制几何或工具状态。菜单复用本地 `DropdownMenu`，保持图标、当前态、快捷键与禁用原因可访问。
- Esc 关闭菜单并还焦点；在菜单中按字母不触发画布操作。缩放或 resize 导致聚焦项移位时，将焦点归还“更多”入口。
- 图片和视频共用溢出算法；3D 保持原入口和语义。极小容器采用可滚动工具区域，不允许静默丢工具。

**验收**

在高度 768/900/1080px、浏览器 100%/125%/150% 缩放、停靠面板 resize 和时间轴展开/收起后，全部可用工具都能访问，当前工具可见，菜单没有画布点击穿透。覆盖现有窄屏工作台门控；不能通过缩小截图代替浏览器缩放验收。

实现集中于 `shell/ToolDock.tsx`，增加相邻纯容量计算模块，复用现有 UI adapter。扩展 `ToolDock.test.tsx` 与 `e2e/tests/workbench-layout.spec.ts`，验证 Stage DOM 身份和草稿未因溢出改变。

## 7. C：图片会话级快速生产

**首版范围**：图片手工 bbox、OBB、polygon、polyline 和按模板完成的 keypoints。Mask 保存与 AI 接受继续使用各自会话，分别由 D/E 改善；不自动套入普通几何连续提交。

**类别和工具合同**

1. 默认保留安全确认，标注员在工作台显式开启“连续创建”。会话态由现有 Workbench owner 管理，不新增服务端 preferences 字段或管理员配置。
2. 创建意图按 `(tool_unit_id, class_name)` 标识，类别面板按工具单元提供入口。同名单元不能串类；只恢复 C 首版允许集合中的最近手工工具，`region` 因而使用 Polygon，不恢复 Mask 或启动 AI。目标工具不可用时停止连续创建并给出原因。
3. 同项目切题可保留模式和合法类别/工具意图，但清除几何、属性草稿和原题提交状态；每题重新校验绑定。切项目、媒体类型、进入只读审核或权限失效时退出连续模式。
4. 切换类别只改变下一对象的创建意图，不修改已保存的选中对象。已有草稿时先通过现有取消/保存流程，不能把未完成几何静默归给新类别。

**创建与退出合同**

- 所有几何仍走 `useWorkbenchAnnotationActions` 的共同创建入口。补齐创建草稿的属性参数，复用属性默认值与 `AttributeForm` 的 `getMissingRequired`；首版不继承上一对象属性。
- 类别合法且必填属性齐全时直接创建；缺必填项时只展示缺失字段并保留几何，不重新要求选择已确定的类别。不可变属性不能从上一个对象静默复制。
- 下一对象只能在现有保存管线成功接收后开始：在线创建成功，或现有离线队列已持久化接收。请求失败保留草稿并允许重试，避免连续输入绕过提交状态。
- 顶部短状态显示“连续创建 · 类别 · 工具”，有明确退出按钮。Esc 先交给弹层/工具取消当前草稿；没有草稿时 Esc 退出连续模式并回选择工具。

**验收**

一次选类后完成 20 个同类 bbox，额外选类浮层次数为 0；Polygon 完成后可继续同类下一对象。覆盖跨单元同名类别、工具被禁用、必填/不可变属性、任务锁、只读审核、网络失败、离线队列及快速切题。生成的 geometry、class、tool unit 和 attributes 与安全确认路径一致。

改动入口为 `state/useWorkbenchState.ts`、`state/useWorkbenchAnnotationActions.ts`、`state/useWorkbenchShellModel.tsx`、`shell/ClassPalette.tsx`、`shell/ClassPickerPopover.tsx` 和相应 Stage 调用方。文档解释会话边界，避免让用户误以为模式会跨设备同步。

## 8. D：Mask 阶段化主动作

保留现有 Mask phase、预览、高级操作和冲突恢复。从它们派生一个动作描述，统一主按钮、Enter、Esc 和 HUD；不新增平行状态机。

| 当前状态                 | 主动作                             | Esc / 次动作                                   |
| ------------------------ | ---------------------------------- | ---------------------------------------------- |
| 计算、提交中或条件不满足 | 展示进度/禁用原因，不重复提交      | 仅执行当前所有者支持的取消，不销毁不可取消事务 |
| 实例预览有效             | 提交 N 个实例                      | 取消实例预览，保留编辑会话                     |
| 区域预览有效             | 应用区域预览                       | 取消区域预览，保留此前像素草稿                 |
| 普通像素脏稿             | 保存 Mask                          | 通过既有未保存修改保护退出                     |
| 视频单帧或轨迹脏稿       | 保存当前帧 Mask / 保存当前帧关键帧 | 同一草稿保护；保持帧物化需明确写出             |
| 无脏稿                   | 已保存；无提交动作                 | 退出 Mask 编辑                                 |

实例与区域预览按现有互斥约束处理；若出现失效或冲突，主动作转为恢复动作，不能选择另一个提交路径。空结果的确认必须由按钮和键盘共享。工具仍按现有高级菜单组织，只补当前阶段路径、结果名称和对其它 Mask 的影响提示。

**验收**：区域“应用”只改变草稿，随后“保存”才持久化；实例提交次数为 1；取消预览不丢已有编辑；内存限制下 `canEdit=false`、`canCommit=true` 仍能保存；任务/帧/对象切换和错误恢复不会使用过期预览。既有实例操作的原子撤销缺口不在 D 暗中扩大；H 中新增 Slice 必须自带原子撤销。

修改 `MaskToolbar`、其调用方与 `useWorkbenchHotkeys.ts`，把已有动作解析与分派收在当前 Mask owner。扩展现有 MaskToolbar/快捷键测试和 `mask-session-guard` 交互回归。

## 9. E：AI 顶栏与 Inspector 按阶段组织

拆为两个可独立合并的小步：E1 调整交互顶栏主次层；E2 调整 Inspector 当前阶段。

- **主层**：提示方式、正负极性、输出类型、当前必要输入、候选序号/数量、接受/取消/重试和短状态。文本是当前工具必需输入时必须留在主层；不能因归为“参数”而隐藏。
- **高级层**：后端、模型、variant、非必需阈值、字段映射、诊断和性能信息。未配置或能力不兼容时，主层保留可操作的错误提示与配置入口。
- **阶段**：idle 展示运行入口和输入；running 展示进度、输入摘要与支持的取消；review 展示候选、属性和决定；error 展示重试及诊断入口。高级区仍可手动展开。
- 能力协商 loading/error 与实际推理 loading/error 分开，分别从 routing 与本次 SAM/预测所有者读取。当前请求的输入摘要保持对应版本，不能显示后来选择的模型作为已运行模型。
- 普通预测、SAM 和追踪作业共享展示词汇，仍由原执行所有者处理取消、重试和落库。面板隐藏、标签切换和布局恢复不取消 job，也不丢属性草稿。

**验收**：四阶段各有唯一主动作，加载/失败来源准确；取消或任务切换后旧结果不重新打开新题审阅；同一 panel 只有一个业务实例；候选接受和原生 Mask lineage 保持；图片与视频两侧通过。

主要目标为 `shell/InteractiveToolBar.tsx`、`shell/AIInspectorPanel.tsx`（其中导出 `AIPredictionPopover`）与 Shell 装配。复用既有 AI 测试及 `native-mask-ai`、`video-tracker-local-review` 场景，不增加后台执行流程或模型依赖。

## 10. F：视频轨迹上下文与作用范围

### F1：常驻当前轨迹条

放在画布面板内、视频画布上缘，不依赖可隐藏的详细时间轴。收敛现有选中卡与 sticky 信息，复用 `videoStageGeometry.ts`、`videoTrackOutside.ts`、`videoTrackTimeline.ts` 派生：

- 类别、短 track id、颜色、锁定状态；无选中时显示选择提示。
- 从 0 起的当前源帧、关键帧/插值/保持/outside、遮挡状态与相邻关键帧。
- 来源只使用实际存在的关键帧 provenance；缺字段显示“来源未知”，不能从整条 Annotation 来源推断人工或 AI。
- 补关键帧、outside、重传播或审阅入口仅在相应写入条件满足时出现；最多显示 3–5 个当前可用快捷键。

当前不可见或 outside 不等于轨迹不存在。查询轨迹身份与当前帧状态分离，不从当前帧可见几何数组推断生命周期；不依赖 3D 研究中的未实现状态。K 继续暂停播放，不照搬竞品的关键帧键位。

### F2：显式“单帧 / 轨迹”作用范围

工具坞顶部提供作用范围选择，下面投影当前范围可用工具，复用 `VideoTool`、`videoToolUnits` 与现有提交路径。

选择轨迹且无草稿时进入轨迹范围；点空白只清除选择，保留显式作用范围，避免下一笔因取消选中而意外变为单帧。切换范围必须经过草稿保护。原有快捷键保留明确目标：B/P/M 等进入其单帧工具，T 进入轨迹，范围显示同步更新。预览旁显示“仅当前源帧”或“新建轨迹，从当前源帧开始”。

范围和工具通过同一事件原子更新：只有新的轨迹选中事件会触发自动范围变化，不能用“当前仍选着轨迹”的 effect 反复覆盖显式工具命令。bbox/polygon/polyline/Mask 轨迹分别映射到 `track`/`polygon-track`/`polyline-track`/`mask-track`；`select` 为中性工具，保留最近显式范围。目标工具不可用时保留选择工具并解释原因，不隐式切到其它几何或 AI。

### F3：追踪候选范围共用一个所有者

在现有 `useVideoTrackerJobs` 审阅生命周期内管理当前 job、目标集和帧窗口，供 `VideoTrackerReviewBar`、轨迹条与时间轴消费。停止在每次渲染时默认取首个待审 job；也不在多个组件存独立的目标集。

切帧保留审阅目标与窗口；revision 更新后与剩余合法候选求交。换看参考轨迹不更换审阅目标，只有显式“加入/替换审阅目标”才改变集合。始终显示目标数、帧范围与未决数；部分接受后提供剩余区间的跳转入口。继续沿用局部决定与人工关键帧保护。

**验收**：bbox、polygon、polyline、Mask 的关键帧/派生帧/outside 状态准确；锁定和只读时无可执行写动作；选中/取消/切工具不误改范围；切帧、切 job、刷新 revision、部分接受、旧请求返回均不串目标。F1/F2/F3 各自合并后都可使用。

## 11. G：视频 Issue 保存并恢复现场

### G1：补齐视频落点创建和帧锚

接通 `WorkbenchStageHost` 与视频画布的 drop-arm/onIssuePinDrop，复用现有创建框。创建时暂停播放且不吸附到采样网格，冻结打开时的源帧和归一化落点；提交时不能重新读取后来变化的播放头。已有帧图钉、时间轴标记与点击跳转共用该锚点。

无像素位置的任务级 Issue 继续可用。新视频像素 Issue 自动写 `anchor_position.frame`；该帧必须对应实际展示帧，尚未完成的 seek 不能用于冻结锚点。当前 `seekToFrameReady` 会丢失底层结果，且底层有禁用直接返回和超时被标为接受的路径；G1 同时补齐明确的 ready/cancelled/timeout/unavailable 结果，只有实际展示帧等于目标且 task/generation 仍有效才为 ready。Issue 调用方检查结果，失败保持原锚点和可重试状态，不把 Promise 结束等同于定位完成。现有调用方逐一适配与回归。这一步可独立完成创建和跳帧闭环。

### G2：增量上下文合同

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

## 12. H：图片几何提效与可恢复 Slice

H 必须拆为 H1/H2/H3/H4a/H4b，前面三步不会等待 Slice 的后端事务。

### H1：中心向外创建 bbox

仅增加普通 bbox 创建，保持现有 Alt 中心缩放。空白画布 Alt 拖动以起点为中心；另提供当前 bbox 工具的“角点 / 中心”会话选项，适用于系统截获 Alt 的环境。Ctrl/Cmd 保留当前 SAM 候选选择用途。临时修饰键在 pointerdown 锁存，边界处对称限制半径，不能裁一边后移动中心。

[Konva 官方](https://konvajs.org/docs/select_and_transform/Centered_Scaling.html)已有 Transformer 中心缩放能力，但当前实现使用自定义 `ResizeHandles` 且已支持同类交互，因此复用现有手柄与创建路径，不迁移编辑内核。[Supervisely 固定文档](https://raw.githubusercontent.com/supervisely/docs/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/labeling-tools/bounding-box-rectangle-tool.md)的中心保持机制作为交互参考。

验收中心不漂移、反向拖动与图像四边一致，零面积拒绝沿用现有漏斗；C 连续创建可直接使用。测试扩展 `ResizeHandles.test.ts` 及 ImageStage 创建交互。

### H2：Polygon 沿光标自动落点

在现有 `PolygonTool` 草稿内增加 Shift 拖动，按累计 8 CSS px 屏幕距离重采样，相邻输入样本间可生成多个点，按动画帧合并草稿更新并保留终点；不让鼠标事件频率决定采样密度。松开后继续同一草稿，Backspace 删除最后一点，Esc 取消草稿，Enter 保持现有闭合语义。普通单击与既有吸附优先级保持，首版不引入曲率算法。

阈值按视口尺度换算，保持屏幕上的点间距；保留现有几何校验。自动采样在草稿达到 20,000 点时暂停并提示，保留已有点且不自动提交或截断；这是固定交互预算，不新增设置。验证缩放下屏幕采样密度一致、零移动/重复点不膨胀、快捷键与图片平移不冲突，并与 C 连续创建协同。

### H3：沿已标边界追踪

首版仅支持当前图片中已保存、可用的简单单外环 Polygon。起止点吸附到同一边界，预览顺/逆两条路径，默认较短弧，并提供两项明确选择；确认后一次追加到现有草稿，随后仍可逐点撤销。

冻结来源 ID/version；来源发生修改或消失时使预览失效。带孔或多外环对象使用现有复杂几何门控，明确显示不支持原因，不降级成 points-only 几何。测试凹多边形、跨首尾顶点、同点、近顶点与来源更新。

### H4a：Polygon Slice 连同原子撤销交付

**几何范围**：图片内已保存、未锁定、无活动子对象的简单单外环 Polygon。切线可以是折线，但不能自交，必须恰有两次有效穿越；相切、沿边重合、多次进出、零面积、孔洞和 multi_polygon 明确拒绝。折线最多 256 个点，作为固定解析边界，不新增环境变量。

**算法与预览**：截取切线在对象内的路径，与原边界的两条弧分别组成结果环。复用已有 polygon 操作做前端有效性检查；服务端根据原几何和切线独立重算、校验两个结果非空、内部不重叠、并集等于原形状，面积误差阈值为 `max(1e-10, source_area * 1e-8)`（归一化坐标）。[polygon-clipping 官方接口](https://github.com/mfogel/polygon-clipping)提供布尔 Polygon/MultiPolygon 运算，未提供开放切线 Slice，因此不能把 difference 的删面积语义直接当作切割。

**受限 API**：新增 `POST /tasks/{task_id}/annotations/polygon-slices:commit`，输入为 `annotation_id`、`expected_version`、`idempotency_key` 和归一化 `cut_path`。服务端决定结果，不能信任客户端提交两块几何。按面积大者保留源 ID；相等时按质心 x/y 排序确定，另一块生成新 ID。源对象身份与属性保持；新对象复制类别、工具单元、业务 attributes、对应 attributes_meta、z_order 和原 parent_annotation_id，保持同一父对象关系，不把被切来源设为新对象的 parent。新对象 source 为 manual、user_id 为操作者、confidence 为空，不复用 external_id 或 parent_prediction_id；来源追溯通过 lineage 保存。

一次数据库事务完成校验、更新/创建、操作账本和 lineage。沿用 Task → Annotation 锁顺序、权限、版本与幂等约定；相同幂等键不同内容冲突。新增应用内 `annotation_slice` 模块，不建通用 mutation 框架，不引入生产几何库或新部署服务。

新增文件集中在 `apps/api/app/schemas/annotation_slice.py`、`apps/api/app/services/annotation_slice.py`、`apps/api/app/api/v1/tasks/annotation_slices.py` 和 `apps/web/src/api/annotationSlices.ts`；原有任务路由注册、账本模型、几何预览及 history 调用方同步接入。前端新增的切线预览会话仍归图片 Stage 所有。

**撤销/重做**：同一切片新增 `POST /tasks/{task_id}/annotations/slices/{operation_id}:restore`，请求带目标 `before/after`、当前完整结果版本集和新的幂等键。服务端从操作账本中的受限前后快照恢复几何与活动状态，保留 ID，版本单调递增；客户端不能传任意回滚几何。仅允许恢复 slice 类操作，继续执行当前权限与任务状态门控。任一结果版本变化或出现活动子对象时整批拒绝，保留历史栈当前位置。提交和恢复响应均带 `restore_expires_at`：固定为原切割提交后 30 天，超期拒绝恢复并说明，重做不延长截止。

完整版本集必须是原 slice 或最后一次成功 restore 返回并由服务端记录的版本集，不能刷新任意最新版本后覆盖人工修改。响应返回两个 ID 的全部版本（含 inactive 对象），供历史命令更新。恢复到当前同一侧返回现有结果，不递增版本或追加历史；同一次 restore 的超时重试复用同一幂等键，新一次 undo/redo 才生成新键。

客户端在 `useAnnotationHistory` 增加受限 slice 命令，一次请求对应一次撤销/重做，不能组合 create/delete 叶命令伪装成原子操作。当前 history 会先移出命令再吞失败，slice 分支必须改为成功后移动栈，失败保留并提示；切题后只结算原任务历史。持久化失败不清空预览，超时重试沿用原提交幂等键；首版不进入不支持复合事务的离线队列。

扩展现有账本 kind CHECK 接受 `slice_polygon`、`restore_slice`，lineage 增加明确恢复关系；需要最小 Alembic 迁移，无新增业务表。新操作写入后不能直接收窄 CHECK 回旧值，回滚时先移除入口、保留兼容 schema 和审计数据。

### H4b：Mask Slice 复用现有原子管线

首版仅图片已保存、未锁定、无活动子对象的 Raster Mask，使用穿过当前 Mask bbox 的直线切割。输入为有序、不同的两个归一化有限端点，零长度拒绝。以像素中心 `((x+0.5)/W,(y+0.5)/H)` 对有向切线求叉积，`>=0` 归左侧，其余归右侧，前后端不使用不同 epsilon。允许来源自身含孔洞或多个连通分量，结果固定为两个非空实例。像素较多的一块保留源 ID，数量相等时左侧保留；新对象属性与 provenance 遵守 H4a 规则。结果逐像素不重叠且并集等于来源，禁止通过擦除切线造成像素损失。

在现有 `mask-mutations:commit` 中新增 `slice_mask`，通过恰含两个点的 `cut_path` 提交切线，并提交预览结果，服务端独立验证分区；其它 operation 不接受该字段。不能冒用要求“完整原连通分量”的 `split_components`。保持 D 的实例预览与影响范围展示，沿用现有尺寸/内存预算和 RLE 运算。

复用 H4a 的受限 restore 合同和历史命令，并为 `slice_mask` 增加相应账本 CHECK。H4a 不等待 Mask 上线；H4b 在该原子恢复合同上扩展，不改变既有 split_components 的含义。

Mask 恢复保存版本引用，不把 RLE 正文复制进操作账本。复用 `MaskAnnotationRevision` 与现有捕获触发器：更新/停用时保存旧几何，现有 GC 已保护未过期 revision。切割/恢复事务保证所需版本可由当前对象或 revision 定位，并把引用 revision 的 `expires_at` 非缩短地保护到原切割的 30 天截止；现有无限保留项不改短。缺少版本、过期或内容校验失败时整体拒绝且零写入。验收包含 undo 后新实例停用、GC 运行、再 redo，避免回滚指向已回收的 mask asset；无需新增 GC 扫描来源或保留配置。

**H4 验收**：预览数量、面积/像素与落库结果一致；取消零写入；成功后一次撤销完全恢复、一次重做恢复切割结果。覆盖权限/锁、过期版本、同键重放、同键异参、任一步异常回滚、提交成功但响应丢失、重试、任务切换、输出被修改后撤销冲突。Polygon 面积守恒、Mask 像素守恒均由服务端测试验证。

## 13. 验证、文档与结束条件

### 每个切片的验证入口

只运行本切片涉及的测试；全部通过后不无理由重复扩大。以下命令来自当前 package scripts，新行为用例随对应切片补入现有套件或相邻新 spec。

```bash
# A
pnpm --filter @anno/web test src/pages/Workbench/state/hotkeys.test.ts src/pages/Workbench/state/useWorkbenchHotkeys.test.ts
pnpm docs:hotkeys
pnpm docs:settings

# B
pnpm --filter @anno/web test src/pages/Workbench/shell/ToolDock.test.tsx
pnpm --filter @anno/web test:e2e e2e/tests/workbench-layout.spec.ts --project=chromium

# D / E：按改变的 Mask / AI 行为选择
pnpm --filter @anno/web test:e2e:mask-native
pnpm --filter @anno/web test:e2e:mask-ai-native

# F
pnpm --filter @anno/web test src/pages/Workbench/stage/videoStageGeometry.test.ts src/pages/Workbench/stage/videoTrackTimeline.test.ts src/pages/Workbench/stage/VideoTrackerReviewBar.test.tsx src/hooks/useVideoTrackerJobs.test.ts
pnpm --filter @anno/web test:e2e e2e/tests/video-tracker-local-review.spec.ts --project=chromium

# G：补齐新恢复用例后运行
pnpm --filter @anno/web test src/pages/Workbench/state/useIssuePins.test.tsx src/pages/Workbench/stage/useFrameClock.test.ts src/pages/Workbench/stage/useVideoPlaybackController.test.ts

# 所有前端切片
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint

# G / H：先验证一次性测试库，再在 apps/api 中执行
uv run pytest tests/test_annotation_feedbacks.py tests/test_mask_mutations.py

# G / H：API 合同变化后，从仓库根执行
pnpm openapi:export
pnpm codegen
pnpm openapi:check

# 每个切片结束
git diff --check
```

C 另覆盖创建动作、类别/属性表单和图片连续创建浏览器路径；G 增加 Issue capture/restore 的 hook、API 和浏览器用例；H4 增加切割事务与 slice history 的行为测试。不能只运行上面的旧测试宣称新增能力已通过。

Python 优先使用本工作区 `apps/api/.venv`。本地 Playwright 配置使用隔离 API/Web 端口与 `annotation_e2e`，pytest 使用测试配置中的一次性库；运行前验证实际目标，不能仅凭库名认定可清空。Node 依赖和 `.env` 当前指向主工作区，测试不修改这些共享目标。每次测试结束清理本次生成的缓存、截图、trace、HTML 报告及临时 seed；保留明确交付的基线证据，不能删除预存文件。

初始规划阶段只做源码和合同核验。已完成切片的应用单测与真实浏览器验收见各自文档 Outcome；尚未实施的里程碑、数据库迁移演练与远端 CI 不因相邻切片通过而视为完成。

### 用户路径测量

在每个相关切片实现前，用相同数据、视口和操作脚本保留基线；这属于切片验收准备，不另设“先调查再规划”的交付阶段。

- 图片：100 个同类 bbox、20 个复杂轮廓、10 个多实例 Mask。
- 视频：10 条轨迹的帧/对象切换、3 段漂移审阅、10 个 Issue 定位。
- 记录中位时间、P90、点击/按键/工具切换数、撤销/取消/错类/错范围次数；Issue 单独记录点击至完整恢复用时。

硬门为数据与作用范围错误 0、工具不可达 0、C 的 20 个对象重复选类 0、D/E 当前主动作可解释、G 锚点恢复准确、H 一次原子撤销成立。效率验收比较基线，要求目标路径操作数下降且中位时间/P90 无明显退化；不把未经实测的提效百分比写成现状。缺少新手参与时，不报告新手成功率已验证。

### 同步文档

| 步骤 | 同提交更新                                                                         |
| ---- | ---------------------------------------------------------------------------------- |
| A/B  | 快捷键生成页、工作台工具入口与布局说明                                             |
| C    | 图片创建、类别/属性、连续模式及会话边界说明                                        |
| D    | `docs-site/user-guide/workbench/mask-brush.md`                                     |
| E    | 交互式 AI、预测审阅和运行错误说明                                                  |
| F    | `video-track.md`、`video-playback.md`、`video-propagate.md`                        |
| G    | Issue/审核指南、`docs-site/api/`、`README.md`、OpenAPI 与受影响 SDK 合同           |
| H    | `bbox.md`、`polygon.md`、Mask 说明；H4 另更新 API、OpenAPI、SDK 和原子操作架构文档 |

各用户可见改动写入 `CHANGELOG.md` 的 Unreleased 对应分组；架构长期约定进入 `docs-site/dev/concepts/`，H4 的原子恢复合同在实现时新增 ADR。现有版本号不变，只有维护者请求发布时再执行 release 流程。每个已实施切片记录真实提交和验证结果；完整交付后追加 `## Outcome`，不在规划阶段预填完成。

## 14. 风险、降级与回滚

- **最脆弱假设：** 高频同类对象的类别/工具和默认属性足以完成创建。若必填属性经常变化，C 仍会显示属性表单，提效小于纯 bbox 任务；默认安全确认与显式连续模式使流程仍然可用。
- **外部依赖失败：** AI 后端离线只影响现有 AI 路径，手工创建与编辑可用。E 显示能力发现/推理各自的错误，不自动换模型重跑。新增外部账号、API key、MCP、CLI、部署服务、环境变量和生产依赖均为 0。
- **规模增加：** 十倍候选量时，F 优先复用现有查询和聚合，禁止为状态条每帧全量复制候选几何；H 沿用 Mask 预算并限制切线路径复杂度。超过既有编辑预算时明确禁止该操作，保留原标注，不能截断数据后保存。
- **前端回滚：** A–F 和 H1–H3 可按切片回退代码；C 为会话态，E/F 不增加布局持久化合同，不需要数据回填。
- **Issue 回滚：** G 的旧锚字段始终写入，新上下文作为可选 JSON 保留；旧读端可退回帧/像素定位。回退不清理历史 Issue 内容。
- **Slice 回滚：** H4 保留新增 CHECK 值、快照与审计账本，停止新入口即可；已有切割结果仍是标准 Polygon/Raster Mask。不能为了降级删除新操作记录或把部分结果当作成功撤销。

**交付完成定义**：A–H 及其列出的子切片均完成对应行为、失败路径、文档和验证，最后复核 diff 并清理测试中间产物。若执行范围只到首批，则明确记录 A–D 完成、E–H 未实施，不能把完整计划标为完成。

## 15. 独立草案与执行顺序

已拆为 16 份独立草案，优先 A–D。用户于 2026-09-07 将执行要求更新为“独立草案 → 逐步实施 → 浏览器实测 → 提交 → 下一里程碑”，该授权覆盖全部草案，无需逐项再次确认。全部 16 个里程碑均已按依赖完成，逐步验收后提交；各草案 Outcome 保存实现、验证和清理证据。

下表为执行状态索引；各草案记录本步设计与验收，Epic 保留整体目标、共同边界和历史依据。实现前如发现与草案合同有实质冲突，先更新对应草案并说明差异，不静默改变交付范围。

| 里程碑 | 独立草案                                                                          | 硬依赖 | 确认   | 实施   | 浏览器实测     |
| ------ | --------------------------------------------------------------------------------- | ------ | ------ | ------ | -------------- |
| A      | [视频候选决策与快捷键](1788769060_workbench-a-video-candidate-decisions.md)       | 无     | 已确认 | 已完成 | A-1–A-5 通过   |
| B      | [工具坞高度溢出](1788769060_workbench-b-tool-dock-overflow.md)                    | 无     | 已授权 | 完成   | B-1–B-4 通过   |
| C      | [图片连续创建](1788769060_workbench-c-image-continuous-creation.md)               | 无     | 已授权 | 已完成 | C-1–C-5 通过   |
| D      | [Mask 阶段主动作](1788769060_workbench-d-mask-primary-actions.md)                 | 无     | 已授权 | 完成   | D1–D5 通过     |
| E1     | [AI 交互顶栏主次层](1788769060_workbench-e1-ai-toolbar-layers.md)                 | 无     | 已授权 | 已完成 | E1-1–E1-4 通过 |
| E2     | [AI Inspector 阶段展示](1788769060_workbench-e2-ai-inspector-phases.md)           | E1     | 已授权 | 已完成 | E2-1–E2-5 通过 |
| F1     | [当前视频轨迹条](1788769060_workbench-f1-video-track-context.md)                  | 无     | 已授权 | 已完成 | F1-1–F1-4 通过 |
| F2     | [视频单帧与轨迹范围](1788769060_workbench-f2-video-tool-scope.md)                 | B      | 已授权 | 已完成 | F2-1–F2-5 通过 |
| F3     | [追踪候选审阅范围](1788769060_workbench-f3-video-tracker-review-scope.md)         | 无     | 已授权 | 已完成 | F3-1–F3-4 通过 |
| G1     | [视频 Issue 创建与真实落帧](1788769060_workbench-g1-video-issue-frame.md)         | 无     | 已授权 | 已完成 | G1-1–G1-4 通过 |
| G2     | [视频 Issue 完整上下文恢复](1788769060_workbench-g2-video-issue-context.md)       | G1     | 已授权 | 已完成 | 通过           |
| H1     | [中心向外创建 bbox](1788769060_workbench-h1-bbox-center-out.md)                   | 无     | 已授权 | 已完成 | 通过           |
| H2     | [Polygon 自动落点](1788769060_workbench-h2-polygon-auto-points.md)                | 无     | 已授权 | 已完成 | 通过           |
| H3     | [沿已有 Polygon 边界追踪](1788769060_workbench-h3-polygon-boundary-trace.md)      | 无     | 已授权 | 已完成 | 通过           |
| H4a    | [Polygon Slice 与原子恢复](1788769060_workbench-h4a-polygon-slice-transaction.md) | 无     | 已授权 | 完成   | 通过           |
| H4b    | [Mask Slice 与可回收数据保护](1788769060_workbench-h4b-mask-slice-transaction.md) | H4a、D | 已授权 | 完成   | 通过           |

E1 不依赖 A/D；E2 只依赖 E1。F2 依赖 B，F1/F3 可各自使用现有 UI 独立交付。H1/H2 与 C 的联合检查是集成验收，不是硬依赖；H4a 不依赖 H3；H4b 依赖 H4a 的原子恢复和 D 的主动作。此表细化并取代 §4 聚合步骤中的粗粒度依赖描述。

## 16. 浏览器实测环境

2026-09-07 只读预检确认：主工作区的 Vite/API 使用 3000/8000；3001 为 Grafana；当前工作区 Node 依赖和 .env 指向主工作区，API 虚拟环境独立。A 已完成可见 Chrome 与 Chromium/API 验收，具体结果见 A 的 Outcome。

本 Epic 使用当前工作区启动 Web/API，A 实际端口为 3010/8011（8010 被另一工作区占用；后续启动前复核空闲）。不得复用主工作区运行进程来证明本分支修改，也不停止 Grafana 来腾出端口。测试数据库使用本任务独占的 `annotation_workbench_0dc6_e2e`，通过现有 `PLAYWRIGHT_E2E_DATABASE_URL` 指定并核验实际数据库身份；连接信息复用现有测试配置，不能输出密码或修改共享 .env。

自动化执行时，创建本地临时 `apps/web/playwright.epic.local.config.ts`，导入原 `playwright.config.ts`，覆写 Web webServer 的 PORT/url、use.baseURL 与导入后的 PLAYWRIGHT_BASE_URL 为 3010，并将 API command/url、PLAYWRIGHT_API_BASE、代理与 WebSocket 目标统一为 8011；testDir/fixtures/teardown/项目/数据库保护全部继承原配置。运行命令附加 `--config=playwright.epic.local.config.ts`。此临时文件在测试后删除，不引入永久配置、命令或环境变量，也不伪装 CI 来绕过隔离。

可见浏览器巡检使用同一当前工作区及相同一次性测试库；通过本工作区虚拟环境启动 API 8011，通过已有 API_PROXY_TARGET、PORT 启动 Vite 3010，核对实际 cwd 和页面资源。用现有 seed/API 建立确定性样本，用真实 UI 完成行为与刷新读回；不在页面 evaluate 中读取 token、store 或改内部状态。每份草案记录实际运行 URL、代码基线、视口/缩放、逐项结果、相关控制台/API 错误和必要截图证据。

每个里程碑的功能实测与定向回归都通过后才标为完成；测试结束清理本次 seed、临时配置、trace、HTML 报告和其它中间产物，保留明确交付的最小证据，停止本任务启动的进程并恢复浏览器缩放。真实 ML 后端测试与确定性响应夹具分别标注；客户端交互通过不能冒充模型质量或渲染性能资格通过。

A 实施时发现共享依赖中缺少必要的 dockview-react；已仅在本工作区将 root/web/docs 的依赖链接改为本地安装，使用 frozen lockfile，包清单与锁文件未变。后续不再把这三处视作共享链接；`.env` 仍指向主工作区，不应修改。A 的临时运行服务、配置和测试数据已经清理，后续里程碑需重新核验并启动自己的验收环境。

## Outcome

- Landed commits: 16 个里程碑的实现、验证和提交记录见 §15 链接的独立计划。
- Release milestone: `0.25.3`（维护者于 2026-09-09 确认）。
- User documentation: `docs-site/user-guide/ai/`、`docs-site/user-guide/review/`、`docs-site/user-guide/workbench/`。
- Developer documentation: `docs-site/dev/concepts/`、`docs-site/api/`。
- ADR: `docs/adr/archive/0074-annotation-slice-operations.md` 及各独立计划记录的相关决策。
- CHANGELOG: `0.25.3` 已记录 A–H 的用户影响。
- Remaining work: 无；媒体重录的剩余工作由 `1788771743_workbench-media-recapture.md` 独立跟踪。
