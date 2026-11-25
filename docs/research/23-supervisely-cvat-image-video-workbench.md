# Supervisely 与 CVAT 图片、视频工作台交互专项调研

> - 调研日期：2026-08-24
> - 关注范围：图片与视频标注的选类、建标、精修、AI 辅助、时间导航、连续作业和审核交互
> - 对照对象：本仓库当前 `feat/26-08-16` 分支，提交 `3e02478d`
> - 前置报告：[`22-supervisely-cvat-workbench.md`](./22-supervisely-cvat-workbench.md)
> - 3D 时序对象补充：[`24-3d-temporal-object-lifecycle.md`](./24-3d-temporal-object-lifecycle.md)
> - 结论用途：产品设计、交互验收和实施排期依据，不是照搬竞品界面的功能清单

本文专门补足前一份报告中图片与视频部分的深度。结论来自 Supervisely、CVAT 的官方文档、教程、图片、GIF、视频和可取得的源码，并逐项对照本项目当前代码与用户文档。

---

## 0. 结论先行

本项目的图片与视频工作台已经不是“缺少基础工具”的阶段：

- 图片侧已有 bbox、OBB、polygon、polyline、关键点、原生 Raster Mask、SAM、多候选审阅、超大图切片、吸附、布尔操作和离线队列。
- 视频侧已有精确逐帧导航、采样网格、J/K/L 播放、书签、章节、循环区间、双层时间轴、轨迹关键帧、outside、occluded、插值、Mask 保持、跨帧 ghost、AI 追踪、局部接受和人工关键帧保护。
- 尤其是 Mask 编辑和视频时间轴，按本次公开资料对照，本项目当前能力比 CVAT 更完整，也覆盖了 Supervisely 的大部分高价值时序交互。

需要打磨的核心不是继续堆工具，而是降低三类摩擦：

1. **高频生产路径仍有重复确认**。图片和视频完成一次绘制后都要再经过类别浮层；左侧类别面板在 2D 工作台主要是展示，不是直接的“类别加工具”入口。对于同类密集标注，这个安全设计会变成每个对象一次的固定成本。
2. **高级能力的状态表达跟不上能力密度**。AI 顶栏、AI Inspector、Mask 工具栏和视频浮层同时承载过多设置、运行状态和提交动作，用户需要自己推断当前到底处于提示、预览、编辑还是提交阶段。
3. **视频对象上下文还不够稳定**。时间轴本身很强，但“当前轨迹是谁、当前帧是什么状态、下一步该补关键帧还是标 outside、问题从哪里跳来”分散在画布、右栏、浮层和时间轴之间。

### 0.1 最值得先做的七项改进

| 优先级 | 改进                                             | 直接价值                                                                  | 主要借鉴                                              |
| ------ | ------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------------- |
| P0     | 增加“快速生产”预设：粘性类别、粘性工具、连续创建 | 消除同类批量标注每个对象一次的选类确认                                    | Supervisely Definitions、CVAT Single Shape 与重复绘制 |
| P0     | 修正视频 AI 候选的 A/D 快捷键契约                | 当前说明写视频通用，但视频路由会把 D 解释为 Smart Box，A 不会进入候选决策 | 本项目源码核验                                        |
| P0     | 工具坞按可用高度自动溢出到“更多”                 | 短屏、浏览器缩放和浮窗布局下不丢工具                                      | CVAT `ControlVisibilityObserver`                      |
| P0     | Mask 和 AI 使用“阶段化主动作”                    | 用户始终看见本次 Enter 会做什么，减少误提交和状态猜测                     | CVAT 明确的 Save / Save and continue                  |
| P0     | 视频增加常驻的“当前轨迹条”                       | 把轨迹身份、帧状态、关键帧来源和下一动作放在一个稳定位置                  | CVAT 对象状态按钮，加上本项目时间轴优势               |
| P0     | 视频 Issue 保存并恢复帧、轨迹、视口和时间窗口    | 审核意见点击后直接回到错误现场，不再依赖自由文本手写 `track_id + frame`   | CVAT 画布 Issue 定位                                  |
| P1     | Polygon 增加沿光标自动落点、边界追踪和 Slice     | 降低复杂轮廓的点击数，补齐当前明确记录的缺口                              | Supervisely 自动点与边界连接、CVAT Snap / Slice       |

### 0.2 不建议重做的部分

- 不要把当前视频时间轴替换成 CVAT 的单滑杆。现有全局导航器、详细轨道、密度、书签、章节、关键帧和传播范围已经明显更强。
- 不要复制 Supervisely Video 3.0 的面板数量。它的信息覆盖全面，但完整界面同时出现工具、Definitions、对象、标签、数据集、Apps 和两级时间轴，直接照搬会压缩画布。
- 不要把 Shape / Track 选择重新塞回每次绘制的弹窗。本项目已经把单帧和轨迹工具分开，问题是入口过多与状态不显眼，不是需要再加一层选择。
- 不要把所有模型和后端参数长期铺在画布上。标注员的首要任务是提示、看结果、决定和修正；模型诊断应是可展开的第二层。

一句话判断：**图片侧先减少重复确认，视频侧先稳定对象上下文，两侧共同补上清晰的阶段和主动作。**

---

## 1. 调研范围与证据

### 1.1 固定快照与媒体覆盖

本次沿用前置报告固定的官方快照：

| 来源                 | 快照                                                                                                            | 覆盖方式                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Supervisely 官方文档 | [`supervisely/docs@846b10b`](https://github.com/supervisely/docs/tree/846b10b4a903300dc4cffc7cf785ad95723a0cb4) | 全库 280 篇 Markdown；`labeling/` 的 115 个图片、GIF、MP4 全部检查，25 个 GIF 按四个时点展开   |
| CVAT 官方仓库与文档  | [`cvat-ai/cvat@cd392352`](https://github.com/cvat-ai/cvat/tree/cd392352e76fc314a4cb8c271ad18097224afb77)        | annotation 与 QA 共 56 篇文档；358 个去重媒体引用中 357 个解析并检查，43 个 GIF 按四个时点展开 |
| 本项目               | 本地提交 `3e02478d`                                                                                             | 用户文档、工作台截图、状态管理、工具坞、AI、Mask、时间轴、轨迹、Issue 和快捷键源码交叉核验     |

前置报告还检查了 Supervisely 的 15 个 3D YouTube 演示、4 个性能短片，以及 CVAT 关联的 4 个长视频。本报告没有重复罗列媒体总表，而是对图片和视频的关键操作段做第二轮逐帧观察。

“全部检查”在这里指固定快照内与 annotation、labeling、video、QA 直接相关的公开材料，不包括厂商网站的招聘页、营销装饰图、私有企业版界面或登录后才能访问的内容。

### 1.2 本轮重点深读的交互证据

#### Supervisely

- [Bounding Box](https://docs.supervisely.com/labeling/labeling-tools/bounding-box-rectangle-tool)：Corner Drag、Center Out、连续创建。
- [Polygon Tool](https://docs.supervisely.com/labeling/labeling-tools/polygon-tool)：Shift 拖动自动落点、沿已有边界连接、吸附、孔洞和增减区域。
- [Brush Tool](https://docs.supervisely.com/labeling/labeling-tools/brush-tool)：Overlay、Overwrite、Preserve 三种遮挡策略，以及缩放无关笔刷。
- [Smart Tool](https://docs.supervisely.com/labeling/labeling-tools/smart-tool)：正负点、框提示、模型切换和 SAM 3 exemplar 多实例结果。
- [Video 3.0](https://docs.supervisely.com/labeling/labeling-toolbox/videos-3.0)：Definitions、Auto Tracking、对象与标签时间轴、精细时间轴、对象合并和数据集导航。
- [Live Training 固定快照](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/labeling-with-AI/live-training/live-training.md)：边标边训、刷新预测与训练状态反馈。

#### CVAT

- [Single Shape Mode](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/single-shape/)：任务提示、Skip、自动下一帧、自动保存、只看空帧和固定顶点数。
- [Track Mode](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/track-mode-basics/)：关键帧、插值、outside、merge、split 和关键帧导航。
- [Controls Sidebar](https://docs.cvat.ai/docs/annotation/annotation-editor/controls-sidebar/) 与[对应源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/control-visibility-observer.tsx)：工具分组与高度不足时的自动溢出。
- [Objects Sidebar](https://docs.cvat.ai/docs/annotation/annotation-editor/objects-sidebar/) 与[对应源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/objects-side-bar/objects-side-bar.tsx)：Objects、Labels、Issues 三个视角，默认类别与快捷改类。
- [Brush Tool](https://docs.cvat.ai/docs/annotation/manual-annotation/shapes/annotation-with-brush-tool/)：Save mask、Save mask and continue、清除底层像素和临时隐藏 Mask。
- [Join and Slice](https://docs.cvat.ai/docs/annotation/manual-annotation/utilities/slice-and-join/)：Polygon 与 Mask 的合并、切割和自动路径点。
- [AI Tools](https://docs.cvat.ai/docs/annotation/auto-annotation/ai-tools/)：Interactor、Detector、Tracker 与标签映射。

#### 图片、GIF 与视频中实际看见的动作

| 媒体证据                                    | 画面中的关键动作                                                                 | 对本项目判断的作用                                     |
| ------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Supervisely bbox 图片与短视频               | Corner Drag、Center Out、Alt 移动、完成后继续建框                                | Center Out 和连续创建是低风险效率项                    |
| Supervisely polygon GIF                     | Shift 拖动自动落点、沿已标轮廓连接、吸附、增减区域和孔洞                         | 当前 Polygon 不必重做编辑内核，应优先减少创建点击数    |
| Supervisely brush GIF                       | Overlay、Overwrite、Preserve、Cut、Fill 的不同影响范围                           | Mask 需要把“会覆盖谁”说清楚                            |
| Supervisely exemplar 视频                   | 一个参考框返回多个同类实例，结果可逐个处理                                       | 本项目应打磨现有 Exemplar 的多实例会话，不新增同义工具 |
| Supervisely Video 3.0 全屏图与 tracking GIF | 左工具、右 Definitions、底部对象时间轴、小型精细时间轴、数据集列表与自动追踪同屏 | 借鉴完整时序信息，不照搬面板密度                       |
| CVAT Single Shape 截图与教程                | 一句任务提示、Skip、自动保存、自动下一帧、只看空帧                               | 任务预设可以显著减少生产路径选择                       |
| CVAT brush GIF                              | Save mask、Save and continue、清除底层像素、临时隐藏当前 Mask                    | 主动作应直接说明保存结果                               |
| CVAT polygon 与 Slice GIF                   | Shift 自动点、Snap、切割路径、预览后分成两个对象                                 | Slice 是当前明确缺口，可复用已有预览和撤销             |
| CVAT Track GIF                              | K 关键帧、插值、O outside、merge、split、关键帧跳转                              | 轨迹状态集中展示比继续增加时间轴功能更重要             |
| CVAT Review 截图                            | 画布位置、对象与 Issue 列表联动                                                  | 视频 Issue 应扩展成可恢复的时域上下文                  |

### 1.3 证据边界

Supervisely 的完整前端不是公开源码，所以其行为判断来自当前文档和媒体；CVAT 可以进一步用源码确认状态与布局。本项目的结论以当前代码为准。文中“建议”是基于操作成本的产品推断，不冒充竞品事实。

---

## 2. 三个工作台的真实能力位置

### 2.1 图片能力

| 维度         | 本项目当前                                                           | Supervisely                                          | CVAT                                                                       | 判断                                             |
| ------------ | -------------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------ |
| 类别与工具   | 工具独立选择，完成绘制后弹类别浮层；2D 类别面板主要展示              | 点击类别自动选择绑定工具，再点当前类别即可新建       | 绘制弹层选 Label / Method / Shape 或 Track；可设默认 Label，N 重复上次绘制 | 本项目安全，但同类密集标注重复确认最多           |
| Bbox         | 角点拖框、编辑、吸附与批量操作                                       | Corner Drag、Center Out、立即继续新框                | Shape / Track、连续重复绘制                                                | 只缺 Center Out 与显式连续模式                   |
| Polygon      | 顶点增删、边界吸附、并集、差集、LOD                                  | Shift 拖动自动点、沿现有边界连接、孔洞               | Shift 自动点、Snap、Intelligent Scissors、Slice / Join                     | 本项目编辑内核强，创建复杂轮廓仍可少点很多次     |
| Mask         | 原生 RLE、像素编辑、形态学、组件、孔洞、实例拆合、转换、QC、区域预览 | Brush 三种覆盖策略、Cut / Fill                       | Brush / Eraser、Polygon to mask、Remove underlying pixels、Slice / Join    | 本次对照中能力最深，主要债务是工具组织和提交语义 |
| 交互式 AI    | Point、Box、Scribble、Magic Box、Exemplar、Mask 精修、多候选         | Smart Tool、模型切换、exemplar 多实例、Live Training | Interactor、Detector、Tracker、OpenCV                                      | 本次对照中能力面领先，界面参数密度偏高           |
| 大图与稳定性 | tile、overview、Minimap、失败重试、离线队列                          | 文档强调工具与平台流程                               | 常规缩放、导航、对象过滤                                                   | 本项目应继续保留现有架构，不因竞品外观重构       |

### 2.2 视频能力

| 维度     | 本项目当前                                                                              | Supervisely                              | CVAT                                    | 判断                                         |
| -------- | --------------------------------------------------------------------------------------- | ---------------------------------------- | --------------------------------------- | -------------------------------------------- |
| 时间导航 | WebCodecs 精确 seek、J/K/L、多速率、采样网格、源帧微调、书签、章节、循环                | 播放控制、完整对象时间轴、小型精细时间轴 | 顶部播放器、逐帧和关键帧导航            | 本次公开材料中，本项目最完整                 |
| 时间轴   | 全局导航器加详细轨道，含密度、Issue、关键帧、outside、插值、传播范围                    | 每对象 / 标签全局时间轴加浮动精细时间轴  | 单滑杆为主，对象状态在侧栏              | 不需要另造“双层时间轴”                       |
| 轨迹编辑 | bbox、polygon、polyline、Mask，关键帧、保持、outside、occluded、拆合、复制与 ghost 续写 | 自动追踪、插值、离屏停止、对象合并       | K 关键帧、O outside、插值、merge、split | 本项目能力更广，但当前轨迹状态分散           |
| AI 追踪  | 单轨、多轨、多目标、文本发现、纠错重传播、按目标与窗口审阅、保护人工帧                  | Auto Tracking 与模型 Apps                | Tracker 和 SAM2 Tracker                 | 本项目决策粒度更细，审阅入口需要更稳定       |
| 视频 QA  | 基础几何警告、HOTA / IDF1、Mask QC、Issue                                               | Objects / Tags 时间轴与作业质量工具      | Review、Issue、GT、自动 QA              | 本项目已有底座，但人工退回仍依赖自由文本定位 |

结论不是“本项目比竞品功能更多”这么简单。它说明下一阶段的投入应该从能力补齐转向：**缩短高频路径、稳定状态、减少浮层竞争、让审核上下文可恢复。**

---

## 3. 从进入任务到提交的摩擦矩阵

| 阶段         | 本项目当前摩擦                                     | Supervisely 可借鉴点                        | CVAT 可借鉴点                              | 建议                                           |
| ------------ | -------------------------------------------------- | ------------------------------------------- | ------------------------------------------ | ---------------------------------------------- |
| 进入任务     | 上次布局可恢复，但工具、类别、AI 面板组合多        | 类别与绑定工具直接可见                      | Single Shape 把任务意图写成一句提示        | 增加任务预设与一句“当前要做什么”               |
| 选类与选工具 | 2D 类别面板不是直接创建入口；绘制后再选类          | 类别行即工具，重复点同类即开始下一对象      | 默认 Label 加 N 重复上次参数               | 快速生产模式下让类别、工具和连续创建保持       |
| 创建几何     | 基础工具完整，复杂轮廓点击数仍高                   | Center Out、Shift 自动点、边界跟随          | Snap、Scissors、Slice、Save and continue   | 先补低风险的 Center Out 和 finish-and-continue |
| 精修         | 功能丰富，入口分散；Mask 尤其密集                  | Overlay / Overwrite / Preserve 直说影响范围 | 明确保存、继续、隐藏 Mask 和清底层像素     | 以意图分组，并把主动作写成结果名称             |
| AI 提示      | 顶栏同时放后端、模型、变体、输出、文本、阈值、状态 | Smart Tool 先交互，模型作为次层             | Interactor / Detector / Tracker 按任务分面 | 顶栏分主层与高级层，Inspector 随阶段切换       |
| 对象流转     | Tab、反引号、J/K、N/U 能力强但不易发现             | 对象与标签面板保持全局可见                  | Objects / Labels / Issues 三视角           | 常驻上下文 HUD，按当前模式只提示 3 到 5 个键   |
| 视频修轨     | 时间轴强，但对象、帧状态、下一动作跨区域           | 每对象时间轴与 Auto Tracking 状态           | 关键帧、outside 和跳转集中在对象项         | 新增当前轨迹条，不再新增大面板                 |
| 候选审阅     | 图片 A/D 顺畅；视频批量候选与交互式 SAM 规则不同   | 追踪结果与时间轴联动                        | Tracker 状态清晰                           | 统一术语和键位，保留不同落库粒度               |
| 审核退回     | 结构化原因已有，视频精确位置靠备注                 | 作业与对象时间轴辅助定位                    | Issue 锚到画布位置和当前帧                 | Issue 持久化完整视频上下文                     |

---

## 4. 图片工作台：应优先打磨的交互

### 4.1 P0：粘性类别、粘性工具与连续创建

#### 当前行为

[`useWorkbenchState.ts`](../../apps/web/src/pages/Workbench/state/useWorkbenchState.ts) 明确把 `activeClass` 定义为预览、默认值和最近使用项；实际类别在绘制完成后由 [`ClassPickerPopover.tsx`](../../apps/web/src/pages/Workbench/shell/ClassPickerPopover.tsx) 确认。图片和视频的 [`ClassPalette.tsx`](../../apps/web/src/pages/Workbench/shell/ClassPalette.tsx) 主要展示类别，当前只有 3D 流程把类别行作为直接可点击的建标入口。

这个设计适合类别经常变化、属性必填或误分类成本很高的任务，但在“同一张图连续标 100 辆车”时，每个对象都需要多一次弹层确认。

#### 两家平台给出的证据

- Supervisely Video 3.0 的 Definitions Panel 允许点击类别后自动选中绑定工具；再次点击当前类别会直接开始新对象。
- CVAT 的 Objects Sidebar 可设置默认 Label，标准工作区用 `N` 重复上一次绘制参数；Single Shape 还能固定类别、几何和顶点数，并自动保存、自动下一帧或只看空帧。

#### 建议设计

保留当前模式作为“安全确认”，新增任务或用户可切换的“快速生产”：

1. 点击类别行，同时设置 `activeClass` 和该类绑定的工具单元。
2. 创建成功后保持当前类别和工具，光标立即回到可绘制态。
3. 类别有必填属性时，只有缺少且无法继承的字段才弹表单；不可变属性不得静默沿用。
4. `Esc` 第一层取消草稿，第二层退出连续创建并回选择工具，规则必须显示在 HUD 中。
5. 顶部用短状态条显示“连续创建 · vehicle · bbox”，点击可退出。
6. 项目管理员可把某个工作流预设为安全或快速，但标注员仍能临时切换。

#### 验收标准

- 选一次 `vehicle` 后连续创建 20 个 bbox，不再出现类别浮层。
- 切到另一个类别时，工具按绑定单元同步切换。
- 必填属性、锁定任务、只读审核和类别不兼容时不能绕过现有校验。
- 连续模式和安全模式使用同一套落库逻辑，不另建平行数据路径。
- 用相同图像标 100 个同类对象，记录中位完成时间、指针点击数和撤销率；目标应由基线实测确定，而不是只验“按钮存在”。

### 4.2 P0：工具坞必须适应短屏

当前 [`ToolDock.tsx`](../../apps/web/src/pages/Workbench/shell/ToolDock.tsx) 是纵向 `flex`，没有按容器高度隐藏或转移工具的逻辑。工具数量继续增加后，在 768px 高度、浏览器放大、OS 缩放或底部面板展开时会出现截断风险。

CVAT 的做法值得直接学习其机制而非样式：每个控制项由 `ControlVisibilityObserver` 观察是否仍在容器内，超出的控制项通过 portal 进入“更多工具”浮层。

建议：

- 选择、当前工具、最近使用和本任务必需工具固定可见。
- 低频编辑操作进入“更多”，展开后仍保留分组和快捷键。
- 图片和视频使用同一溢出框架，但内容按任务能力过滤。
- 视频的“单帧 / 轨迹”不要靠两套相似图标堆叠，改为一个明确的作用范围切换，再展示该范围下的几何工具。
- 更多菜单打开后，键盘焦点、tooltip、禁用原因和埋点必须与主栏一致。

验收：在 768px、900px、1080px 高度和 125%、150% 浏览器缩放下，所有可用工具都可访问，没有按钮落出画面，当前工具永远可见。

### 4.3 P1：Bbox 增加 Center Out 和“完成后继续”

Supervisely 的 Center Out 在拖动边或角时保持中心不动，适合车轮、螺栓、圆形零件和以中心点为锚的重复目标。其媒体还明确展示完成一个框后可以立即创建下一个。

建议只增加两个小能力：

- `Ctrl` 按住时从中心向外创建或缩放，光标旁显示“中心缩放”。
- 在快速生产模式下，完成后继续保持 bbox 工具；安全模式维持当前行为。

不要为它新增长期常驻面板。一个工具设置、临时修饰键和短提示已经足够。

### 4.4 P1：Polygon 创建阶段少点几次，编辑阶段不重做

本项目已有顶点插入、删除、拖动、8px 屏幕距离吸附、并集、差集和未选中态 LOD，编辑内核不弱。明确缺口是 [`polygon.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/polygon) 已记录的 Slice，以及创建复杂边界时仍需逐点单击。

建议按风险从低到高推进：

1. **完成并继续**：当前 Polygon 提交并选类后，快速模式立即开始同类下一对象。
2. **Shift 拖动自动点**：按屏幕距离或曲率采样，松开后仍允许 Backspace 逐点撤销。
3. **沿已有轮廓追踪**：起止点落在已标边界时，预览两条可选路径，用户确认后再加入草稿。
4. **Slice**：一条穿过边界的路径把一个 polygon / mask 切成两部分，先预览实例数与面积变化，再提交。
5. **Intelligent Scissors**：只有真实数据中的边缘对比足够稳定时再引入，不应替代现有确定性的吸附。

Slice 可以复用当前布尔几何、Mask 预览和一次撤销合同，避免再造独立编辑器。

### 4.5 P0：Mask 的主动作必须说出结果

当前 Mask 能力覆盖 Paint、Erase、Lasso、Region、Morphology、Components、Holes、Instance Split / Copy / Merge、Non-overlap、Convert、QC 和 Compare。能力深度是优势，但 [`mask-brush.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/mask-brush) 也说明 `Enter` 会依状态执行三类不同动作：应用区域预览、提交实例预览或提交整个 Mask；`Esc` 也会先取消预览，再退出会话。

快捷键复用本身不是问题，问题是界面没有始终把“这一次会发生什么”放在主位置。

建议：

- 工具按意图分成“绘制”“清理”“实例”“转换”，默认只展开当前组。
- 顶部常驻状态路径，例如“Mask 编辑 > 区域填充 > 预览 12,438 px”。
- 主按钮随状态改名为“应用区域预览”“提交 3 个实例”“保存 Mask”，旁边再显示 `Enter`。
- 次按钮明确写“取消本次预览”或“退出 Mask 编辑”，不要只显示通用叉号。
- 显示未保存像素变化、撤销步数和操作范围；视频 Mask 还要显示“当前帧关键帧”或“从保持帧物化”。
- 借鉴 Supervisely 的 Overlay、Overwrite、Preserve，用自然语言提示对其它 Mask 的影响；现有非重叠和底层像素规则可作为实现基础。

CVAT 的 `Save mask` 与 `Save mask and continue` 很值得借鉴的原因不是按钮样式，而是它把“结束本对象”和“保存后继续下一对象”拆成了两个可预期结果。

### 4.6 P0：AI 顶栏与 Inspector 按阶段组织

当前 [`InteractiveToolBar.tsx`](../../apps/web/src/pages/Workbench/shell/InteractiveToolBar.tsx) 同时承载工具名、重试、后端、模型、variant、正负极性、输出几何、Mask seed、exemplar 形态、文本、阈值、运行状态和警告。图片中的 [`AIInspectorPanel.tsx`](../../apps/web/src/pages/Workbench/shell/AIInspectorPanel.tsx) 又同时承担运行配置、候选筛选、属性审阅、运行统计和诊断。

建议改成两层：

#### 画布主层

- 当前交互工具。
- 正负极性或当前提示类型。
- 输出类型。
- 候选数与当前序号。
- 明确的接受、取消、重试和状态。
- 会直接影响当前候选的阈值。

#### 高级层

- 后端、模型、variant。
- exemplar 文本与稀有参数。
- 能力告警、字段映射和运行诊断。
- 性能与模型元数据。

Inspector 应随阶段切换：

- 尚未运行：显示“运行”与常用预设，例如快速、均衡、精细。
- 正在运行：只显示进度、取消和输入摘要。
- 已有候选：默认进入“审阅”，显示筛选、属性和接受 / 拒绝。
- 出错：显示可恢复动作和诊断详情入口。

不要把四个阶段的控件同时展开。

### 4.7 P1：SAM 增加提示历史，而不是再加一个工具

本项目的 Smart Point、Smart Box、Smart Scribble、Magic Box 和 Exemplar 已覆盖主要交互范式。Supervisely 的 SAM 3 reference box 值得关注，因为一次示例框可以产生多个同类实例，但本项目的 Exemplar 已有相近能力，不需要另造工具。

更有价值的是补齐会话控制：

- 撤销最后一个正点、负点或框提示。
- 提示历史用小 chip 展示，可单独删除。
- “清空提示”与“退出工具”分开。
- 多实例结果显示当前候选、总数和已采纳数。
- 视频切帧会清空候选时，在动作发生前显示原因，不让用户误以为结果丢失。

### 4.8 P2：密集场景增加对象视图预设

CVAT 的 Objects、Labels、Issues 三个页签和按 Label 批量隐藏 / 锁定很实用。本项目已有右侧对象列表、来源分组、类别和 Issue，不必复制三页签，但可增加轻量预设：

- 当前类别。
- 当前选中附近。
- 未审 AI。
- 锁定 / 隐藏。
- 有 Issue。

这些是列表过滤与画布显隐的同一状态，不要再建一套“图层系统”。

---

## 5. 视频工作台：应优先打磨的交互

### 5.1 先承认现有时间轴已经领先

本项目的 [`video-playback.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/video-playback) 和 [`video-track.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/video-track) 已覆盖：

- 播放、反向播放、多速率与逐帧精确定位。
- 采样网格和 `Shift + ← / →` 源帧微调。
- 书签、章节、循环区间与导航历史。
- 全局 navigator 和可缩放详细时间轴。
- prediction / manual 密度、Issue、selected track keyframe、outside、interpolation、propagation 和 loop 轨道。
- Mask 轨迹的保持语义，以及 bbox 的线性插值。

Supervisely 的全局对象时间轴加精细浮动时间轴验证了“两级时域”的价值，但本项目已经实现。下一步不应再造时间轴，而应解决信息优先级：默认只强调当前轨迹、当前问题和当前操作范围，其它密度轨道按需展开。

### 5.2 P0：常驻“当前轨迹条”

CVAT 的 keyframe/outside 与 Supervisely 的 episode object/figure 结构进一步说明：常驻轨迹条不能只展示当前有几个框，还应区分“身份仍在但当前缺席”“人工关键帧”“系统派生帧”和“未物化成员”。这些状态应由统一轨迹领域返回，界面不从当前帧数组猜测；完整商业与开源平台证据见[时序对象补充报告](./24-3d-temporal-object-lifecycle.md)。

建议在画布上缘或详细时间轴上缘增加一条窄而稳定的轨迹上下文，不做成可拖动大浮窗：

| 区域     | 内容                                                            |
| -------- | --------------------------------------------------------------- |
| 身份     | 类别、短 track id、颜色、来源、锁定状态                         |
| 当前帧   | F128、人工关键帧 / AI 关键帧 / 插值 / 保持 / outside / occluded |
| 邻近信息 | 上一关键帧 F110、下一关键帧 F145、间隔警告                      |
| 推荐动作 | 补关键帧、标记 outside、修正并重传播、审阅候选、跳下一个异常    |
| 快捷键   | 只显示当前可用的 3 到 5 个键，例如 `O`、`,`、`.`、`Ctrl+B`      |

这条信息应由现有选中轨迹和时间轴状态派生，不新增持久化数据。无选中时收起为“选择一条轨迹查看状态”。

### 5.3 P0：把“单帧 / 轨迹”变成明确作用范围

当前工具坞同时出现单帧框、单帧 polygon、单帧 Mask、轨迹框、轨迹 polygon、轨迹 Mask 和若干 AI 工具，图标相似时容易让用户画错作用范围。

建议：

1. 工具坞顶部提供“单帧 | 轨迹”分段选择。
2. 下面只显示当前范围可用的几何工具。
3. 当前轨迹已选中时，默认进入轨迹范围；点空白或显式切换才回单帧。
4. 每个新建动作的预览旁显示“只在 F128”或“新建轨迹，从 F128 开始”。
5. 视频 Mask 的“保持帧物化为人工关键帧”要在提交按钮上直接写出。

这不是照搬 CVAT 每次弹 Shape / Track 选择，而是把本项目已有的两套入口变成稳定、可见的模式。

### 5.4 P1：跨采样网格续写做成任务预设

本项目已有 ghost carryover、`Tab` 切换待续轨迹和“续写后自动前进”，但该设置默认关闭。对于固定间隔抽帧后逐对象续写的任务，CVAT Single Shape 的启示是：生产路径应该可以由任务预设一次确定。

建议提供“视频轨迹续写”预设：

- 开启采样网格。
- 显示上一网格帧 ghost。
- 提交后自动选择同帧下一条待续轨迹。
- 当前帧全部完成后再跳下一网格帧。
- 顶部显示 `已续 7/12`，并允许临时跳过。

默认值只对使用该预设的新会话生效，不应突然改变现有用户的全局偏好。

### 5.5 P0：修正视频 AI 候选快捷键契约

这是源码可以确认的问题，不是主观建议：

- [`hotkeys.ts`](../../apps/web/src/pages/Workbench/state/hotkeys.ts) 在视频分支中先把 `D` 路由为 Smart Box，把 `E` 路由为 Exemplar；`A` 在视频分支没有候选接受动作。
- 同一文件的快捷键定义仍把 `A`、`D` 写成接受 / 忽略 AI 候选。
- [`workbenchSettingsFields.ts`](../../apps/web/src/pages/Workbench/state/workbenchSettingsFields.ts) 的“决策后自动前进”说明写明“视频 + 图片工作台通用”。
- 图片候选可以进入 `acceptAi` / `rejectAi`；视频的普通 AI 候选目前主要依赖贴框按钮和侧栏按钮。视频交互式 SAM 另有捕获阶段的 `Enter` / `Esc` / `Tab`，两者不是同一种候选合同。

建议先确定唯一产品语义，再改代码和文档：

- 方案 A：视频选中普通 AI 候选时，A/D 优先于工具切换；未选候选时 D 才切 Smart Box。
- 方案 B：视频候选改用不会与工具冲突的组合键，并只在视频 HUD 显示真实键位。

更推荐方案 A，因为它保持图片和视频的候选审阅肌肉记忆。验收必须增加 `videoMode + selected prediction`、`videoMode + selected user track`、`videoMode + no selection` 三组路由测试，并校正自动生成的快捷键文档。

同一快捷键表还有一个可直接修复的事实错误：`Alt+3` 同时被列为“多边形工具（备用）”和“AI 工具（备用）”，实际路由是 AI 工具，Polygon 是 `Alt+2`。

### 5.6 P0：追踪候选审阅保持范围可见

[`video-propagate.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/video-propagate) 所描述的按目标、按帧窗口接受 / 拒绝，以及人工关键帧保护，是本项目的明显优势。需要优化的是审阅期间的可见性：

- 当前选择的目标数、帧范围、已决 / 未决数量始终显示在时间轴上方。
- 时间轴把本次审阅范围作为高对比底色，窗口外候选降噪。
- 切换帧后不丢失目标选择；切换轨迹时明确询问是改变审阅目标还是只看参考。
- 部分接受后，把剩余未决区间压缩成可点击段，而不是只显示总数。
- 运行配置、候选审阅和诊断不能同时占据多个浮窗；运行完成后面板自动进入审阅态。

### 5.7 P0：视频 Issue 必须是可恢复的时间锚点

当前审核文档仍建议在自由文本中写 `track_id + frame`。这对视频不够可靠：长 track id 易写错，重做人员还需要手动恢复缩放、时间窗口和相关对象。

建议扩展 Issue anchor：

```text
task_id
frame_index
track_id / annotation_id
geometry snapshot or normalized point
viewport { scale, center }
timeline { start, end, zoom }
optional frame_range
optional candidate/job revision
```

创建 Issue 时从当前选中和播放头自动填入；点击 Issue 时按顺序恢复任务、帧、轨迹、视口和时间窗口。找不到旧版本对象时仍跳到帧和几何快照，并标明“对象已变化”。

对于“F120 到 F160 持续漂移”这类问题，要支持范围 Issue，不要强迫审核员建 41 条单帧问题。

### 5.8 P1：减少浮层争抢画布

当前视频可能同时出现播放浮层、选中卡、AI 单题、AI 追踪、候选审阅条、Mask 工具、质量提示和讨论 Issue。已有部分面板互斥，但整体仍需一个层级规则：

1. 阻断性动作：类选择、保存确认、覆盖人工帧确认。
2. 当前编辑工具：Mask、SAM、轨迹修正。
3. 当前对象状态：常驻轨迹条。
4. 后台与诊断：折叠到状态中心或右侧页签。

同一时刻最多允许一个可拖动编辑浮层。候选审阅状态应进入常驻轨迹条和时间轴，不再额外占一块大浮窗。

### 5.9 P2：多视图视频按真实需求再做

Supervisely 的 multi-view video 能同步查看多相机结果，但这需要相机同步、对象身份、时间对齐和导出合同，不是单纯增加多个播放器。如果本项目当前客户确实有多摄像头同一场景需求，应先复用 3D 标定与多视图对象模型；否则它的收益低于前面的高频交互打磨。

---

## 6. 快捷键与状态表达

### 6.1 上下文复用不等于错误，但必须可见

本项目已有许多合理的上下文键位：

| 键        | 上下文一                                                   | 上下文二                            | 风险判断                                        |
| --------- | ---------------------------------------------------------- | ----------------------------------- | ----------------------------------------------- |
| `B`       | 图片 bbox                                                  | Mask 内为 Brush                     | 捕获阶段已有保护，可保留，但 HUD 要显示当前含义 |
| `E`       | 图片普通态提交质检                                         | Mask 内为 Eraser；视频为 Exemplar   | 高风险，主动作必须显式，不应靠记忆              |
| `D`       | 图片 AI 候选拒绝                                           | 视频为 Smart Box                    | 已形成真实契约冲突，需要修复                    |
| `L`       | 图片无选中时 Polyline；有选中时 Lock                       | 视频无选中时向前播放；有轨迹时 Lock | 可用但难学，必须按上下文显示                    |
| `[` / `]` | 无选中时调 AI 阈值                                         | 有选中时调 z-order                  | 结果差异大，状态提示不可省略                    |
| `Enter`   | Polygon 闭合、SAM 接受、Mask 预览应用、实例提交、Mask 保存 | 类别确认、3D point-mask 提交        | 不应取消复用，但按钮文字必须写明当前结果        |

当前 3D 工作台已经通过 owned keys 避免 `E` 被全局“提交质检”抢走，说明项目已有正确的分层路由思路。图片和视频应把同样的严格性扩展到候选、Mask 与轨迹状态。

### 6.2 建议增加上下文 HUD

不要再增加一张更长的全局快捷键表作为主要解决方案。画布角落显示当前模式的 3 到 5 个关键动作：

```text
Mask · 区域填充预览
Enter 应用预览   Esc 取消预览   Ctrl+Z 撤销
```

```text
Track trk_018 · 插值帧 F128
K 设关键帧   O 标记消失   ,/. 跳关键帧   Ctrl+B AI 延展
```

完整快捷键表仍保留，并加搜索与冲突检查。自动生成脚本应在同一任务上下文内检测重复绑定和“展示定义与实际 dispatch 不一致”。

---

## 7. 推荐的界面状态模型

### 7.1 图片

```text
任务意图条
  当前预设 / 类别 / 工具 / 连续创建状态

工具坞                    画布                    右侧对象与 AI
  常用工具                  草稿与候选              当前对象
  当前工具                  上下文 HUD               当前阶段
  更多工具                  命名主动作               高级设置
```

画布上的信息只回答三件事：当前在做什么、下一次输入会发生什么、怎样退出。模型和工程诊断不与这三件事争夺第一层空间。

### 7.2 视频

```text
当前轨迹条
  身份 | 当前帧状态 | 相邻关键帧 | 推荐动作 | 当前快捷键

工具坞                    画布                    轨迹 / Issue
  单帧 / 轨迹范围           当前对象与候选            对象列表
  当前范围工具              唯一编辑浮层              问题与属性

详细时间轴
  当前轨迹优先 | 审阅范围 | Issue | 其它轨道按需展开
```

该结构不要求重写现有 WorkbenchShell。大部分能力已经存在，主要是重排优先级、增加派生状态条和减少重复入口。

---

## 8. 实施优先级与验收切片

| 切片 | 内容                                    | 规模判断 | 依赖                 | 关键验收                                 |
| ---- | --------------------------------------- | -------- | -------------------- | ---------------------------------------- |
| A    | 视频 A/D 路由、`Alt+3` 文档、上下文测试 | S        | 无                   | 快捷键展示与 dispatch 完全一致           |
| B    | 工具坞高度观察与“更多”                  | M        | 无                   | 768px 与 150% 缩放不丢工具               |
| C    | 图片快速生产模式                        | M        | 类别工具绑定现状     | 20 个同类对象无重复选类弹层              |
| D    | Mask 命名主动作和状态路径               | M        | 现有 Mask 状态机     | 每个状态都能回答 Enter / Esc 结果        |
| E    | AI 顶栏两层与 Inspector 阶段化          | M        | 现有 AI 状态         | 同时只显示当前阶段必需控件               |
| F    | 视频当前轨迹条与作用范围切换            | M 到 L   | 现有轨迹派生状态     | 插值、保持、outside、AI / 人工来源均可辨 |
| G    | 视频 Issue 上下文锚点                   | L        | Issue 数据合同与迁移 | 点击问题恢复帧、轨迹、视口和窗口         |
| H    | Center Out、Polygon 自动点与 Slice      | S、M、L  | 几何预览与撤销       | 创建点击数下降，切割可预览可撤销         |

建议先做 A 到 D。它们不需要新的模型能力或后端编排，却直接影响每天重复最多的动作。F 与 G 随后做，因为它们决定长视频修轨和审核是否真正可用。

### 8.1 统一测量方法

不要只用“功能完成”验收。为改动前后各录制同一批任务：

- 图片：100 个同类 bbox、20 个复杂 polygon、10 个多实例 Mask。
- 视频：10 条轨迹跨 5 个采样帧续写、3 段漂移修正、10 个审核问题定位。

记录：

- 完成时间中位数和 P90。
- 指针点击数、键盘次数、工具切换数。
- 撤销、取消、错类和错作用范围次数。
- 首次使用者在不看完整手册时的任务成功率。
- Issue 从点击到恢复完整现场的时间。

这些指标比“少一个弹窗”更能判断借鉴是否成功。

---

## 9. 值得借鉴，但不应排在近期

### 9.1 Supervisely Live Training

边标边训、快速刷新模型结果对同质数据集很有吸引力，但它依赖数据切分、训练作业、模型版本、回滚、资源调度和污染防护。当前更适合先把本项目已有的 `U` 不确定样本导航与 AI 运行反馈做得可见，再单独设计训练合同。

### 9.2 CVAT Single Shape 的独立工作区

它验证了低干扰生产预设的价值，但本项目不必复制一套平行页面。先在同一 WorkbenchShell 内实现“快速检测”“精细分割”“视频轨迹续写”三个预设，共用工具、落库、权限和审核逻辑。

### 9.3 Supervisely Apps Panel

把应用嵌入工作台有扩展价值，也会带来权限、性能、布局和上下文传递复杂度。当前 AI Inspector 已经承担模型入口，先完成阶段化与诊断下沉，再评估插件式工作台。

---

## 10. 不应照搬的竞品模式

| 模式                          | 不照搬的原因                                | 本项目替代方案                       |
| ----------------------------- | ------------------------------------------- | ------------------------------------ |
| Supervisely 全量多面板常驻    | 小屏画布被压缩，认知负担高                  | 预设布局、单一编辑浮层、阶段化面板   |
| CVAT 每次绘制弹 Shape / Track | 多一次选择，且本项目已有明确工具            | 稳定的单帧 / 轨迹作用范围            |
| CVAT 简单视频滑杆             | 无法承载本项目密度、Issue、传播和 Mask 状态 | 保留现有双层时间轴，优化默认层级     |
| 所有模型参数在热路径展示      | 标注员被工程配置打断                        | 主层只留当前决策，高级层再展模型细节 |
| 为每种生产方式建立独立页面    | 状态、快捷键和落库逻辑容易分叉              | 同一壳层的任务预设                   |
| 仅靠全局快捷键表教学          | 无法解释当前同键含义                        | 上下文 HUD 加可搜索完整表            |

---

## 11. 主要官方资料

### Supervisely

- [Image Labeling Toolbox](https://docs.supervisely.com/labeling/labeling-toolbox/images)
- [Bounding Box Tool](https://docs.supervisely.com/labeling/labeling-tools/bounding-box-rectangle-tool)
- [Polygon Tool](https://docs.supervisely.com/labeling/labeling-tools/polygon-tool)
- [Brush Tool](https://docs.supervisely.com/labeling/labeling-tools/brush-tool)
- [Smart Tool](https://docs.supervisely.com/labeling/labeling-tools/smart-tool)
- [Navigation and Selection Tools](https://docs.supervisely.com/labeling/labeling-tools/navigation-and-selection-tools)
- [Video 3.0](https://docs.supervisely.com/labeling/labeling-toolbox/videos-3.0)
- [Video Tracking 固定快照](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/videos/video-tracking.md)
- [Live Training 固定快照](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/labeling-with-AI/live-training/live-training.md)

### CVAT

- [Annotation Editor](https://docs.cvat.ai/docs/annotation/annotation-editor/)
- [Controls Sidebar](https://docs.cvat.ai/docs/annotation/annotation-editor/controls-sidebar/)
- [Objects Sidebar](https://docs.cvat.ai/docs/annotation/annotation-editor/objects-sidebar/)
- [Single Shape Mode](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/single-shape/)
- [Track Mode](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/track-mode-basics/)
- [Brush Tool](https://docs.cvat.ai/docs/annotation/manual-annotation/shapes/annotation-with-brush-tool/)
- [Polygon Snap Tools](https://docs.cvat.ai/docs/annotation/manual-annotation/shapes/annotation-with-polygons/snap-tools/)
- [Join and Slice](https://docs.cvat.ai/docs/annotation/manual-annotation/utilities/slice-and-join/)
- [AI Tools](https://docs.cvat.ai/docs/annotation/auto-annotation/ai-tools/)
- [SAM 2 Tracker](https://docs.cvat.ai/docs/annotation/auto-annotation/segment-anything-2-tracker/)
- [Manual Review](https://docs.cvat.ai/docs/qa-analytics/manual-qa/)
- [工具溢出源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/control-visibility-observer.tsx)
- [绘制参数弹层源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/draw-shape-popover.tsx)
- [对象侧栏源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard-workspace/objects-side-bar/objects-side-bar.tsx)

---

## 12. 最终建议

本项目当前最有价值的路线不是“做得像 Supervisely 或 CVAT”，而是把已有深度变成更低的操作成本：

- 图片用快速生产预设消除重复选类，用 Center Out、自动点和 Slice 缩短几何创建。
- Mask 保留当前强大的像素与实例能力，但每个阶段只展示一个清楚命名的主动作。
- AI 从参数优先改为交互与决策优先，模型细节下沉到高级层。
- 视频保留现有时间轴，把当前轨迹、帧状态、候选范围和下一动作集中到稳定上下文条。
- 审核问题携带完整时间与视口锚点，让点击 Issue 就能回到现场。
- 快捷键继续允许上下文复用，但展示、路由、设置说明和测试必须来自同一真实状态模型。

Supervisely 最值得学习的是“类别、工具、对象、时间和 AI 被组织成连续作业”；CVAT 最值得学习的是“一个动作的结果清楚、可重复、可测试”。本项目已经拥有比两者更深的 Mask 和视频能力，下一步要让这些能力在高频生产中更少打断、更不容易选错，也更容易被新用户发现。
