# Supervisely 与 CVAT 标注工作台深度调研

> - 调研日期：2026-08-24
> - 关注范围：标注工作台的信息架构、工具交互、3D 点云工作台、传感器融合、时序操作、质量控制与大点云性能
> - 对照对象：本仓库当前 `feat/26-08-16` 分支，提交 `3e02478d`
> - 结论用途：产品与工程决策输入，不是可以直接照抄的迭代清单

本文以 Supervisely 与 CVAT 的当前官方文档、教程、公开源码、图片、GIF 和视频为主证据，并对照本项目现有实现。它补充并更新 [`03-cvat.md`](./03-cvat.md) 的工作台部分，也比 [`14-point-cloud-image-fusion.md`](./14-point-cloud-image-fusion.md) 更关注完整操作过程。图片与视频的逐阶段交互分析、实施优先级和验收标准见 [`23-supervisely-cvat-image-video-workbench.md`](./23-supervisely-cvat-image-video-workbench.md)；更多商业与开源平台的 3D 时序对象、存在区间和轨迹修复合同见 [`24-3d-temporal-object-lifecycle.md`](./24-3d-temporal-object-lifecycle.md)。

---

## 0. 结论先行

两家平台代表了两种不同的 3D 标注产品思路：

- **Supervisely 强在完整工作流**。它把点云、相机图像、AI 辅助、跨帧跟踪、时间轴、对象表和质量流程放进同一工作台，更像面向生产团队的多模态操作系统。
- **CVAT 强在可预期的编辑内核**。它的 3D 功能面较窄，核心仍是 cuboid，但创建、四视图精修、快捷键、复制放置、相机稳定性和对象聚焦的规则更克制，也更容易学习和测试。
- **本项目并不是从零追赶**。当前已经有标定后的 3D 到 2D 投影、相机面板反向选中、点级 3D 掩码、跨帧传播、ego motion 补偿、邻帧点云加密和可持久化三正交视图。这几项比 CVAT 当前公开 3D 工作台更深入，也覆盖了 Supervisely 的部分高价值能力。
- **真正的差距不是再加一个按钮**。当前最缺的是顺畅的高频操作、场景级时间轴、3D 专属质量流程，以及支撑 1,000 万到 5,000 万点数据的空间流式加载。

建议按下面顺序推进：

| 优先级 | 建议                                     | 为什么先做                                                              |
| ------ | ---------------------------------------- | ----------------------------------------------------------------------- |
| P0     | 双击对象聚焦、正交缩放记忆、相机状态稳定 | 小改动直接降低每个 3D 框的精修成本，CVAT 已证明其价值                   |
| P0     | 预设式 3D 布局，而不是无限自由拖拽       | 让框体精修、传感器融合、点级分割各有稳定起点，同时保留现有可拖拽能力    |
| P0     | 持续建框和持续自动拟合循环               | 解决“每标一个对象都要重新选工具”的高频摩擦，借鉴 Supervisely            |
| P0     | 场景时间轴、对象存在区间和异步任务进度   | 把当前逐帧编辑升级为 scene 级工作流                                     |
| P0     | 3D 专属质检模式                          | 形成明显差异化，因为 CVAT 当前公开 Review、Consensus 均没有完整覆盖 3D  |
| P1     | 空间分块、可见性加载和性能基准           | 当前整文件解析后抽样的路线无法支撑真正的大场景                          |
| P1     | 持久化的 2D 与 3D 同一对象               | 将现有“2D 框只作为 3D 建框种子”升级为可追溯的多模态对象                 |
| P1     | 3D AI 辅助、地面分割预览、测距和几何特征 | 补足 Supervisely 已经展示的效率工具，但必须建立在可撤销、可预览的合同上 |

一句话判断：**最值得学习的是 Supervisely 的工作流编排和 CVAT 的交互确定性；最不值得做的是复制它们的界面外形。**

---

## 1. 调研范围、证据和边界

### 1.1 文档与源码快照

本次不是只浏览产品首页，而是先固定官方仓库快照，再对标注相关内容做全文检索和逐页深读。

| 来源                 | 固定快照                                                                                                        | 全库覆盖                                                                           | 深读范围                                                                                 |
| -------------------- | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Supervisely 官方文档 | [`supervisely/docs@846b10b`](https://github.com/supervisely/docs/tree/846b10b4a903300dc4cffc7cf785ad95723a0cb4) | 1,916 个文件，其中 280 个 Markdown、1,231 个 PNG、294 个 JPG、80 个 GIF、13 个 MP4 | `labeling/` 下 53 篇文档，以及 3D、视频、AI 辅助、作业、质检和性能相关页面               |
| CVAT 官方仓库与文档  | [`cvat-ai/cvat@cd392352`](https://github.com/cvat-ai/cvat/tree/cd392352e76fc314a4cb8c271ad18097224afb77)        | 3,026 个文件，其中 207 个 Markdown、234 个 PNG、289 个 JPG、56 个 GIF、6 个 MP4    | annotation 与 QA 共 56 篇文档，并深读 `cvat-canvas3d` 和 `standard3D-workspace` 相关源码 |
| 本项目               | 本地提交 `3e02478d`                                                                                             | 工作台文档、Three.js 实现、性能档位、review 和 AI 预标注文档                       | 以实际代码与当前用户文档交叉验证，不只看功能列表                                         |

### 1.2 图片、GIF 和视频覆盖

“看过文档”不等于“理解交互”。因此本次把媒体作为独立证据处理：

- Supervisely 的 `labeling/` 目录共有 115 个可直接渲染的图片、GIF 和 MP4。全部生成图集逐页查看；25 个 GIF 另外按四个时点展开，避免只看到首帧。
- Supervisely 当前 3D 文档嵌入的 15 个 YouTube 演示全部抽帧检查；性能升级页的 4 个短视频也全部检查。
- CVAT annotation 与 QA 文档共引用 358 个去重媒体路径。357 个能够解析，已全部生成图集查看；其中 43 个 GIF 按四个时点展开检查。
- CVAT Academy 与产品页关联的 4 个长视频均检查了开头、操作段、对象精修段和收尾段。
- CVAT 官方文档有一个失效图片引用：`attribute-annotation-mode-basics.md` 中的 `image026.jpg`。这不影响本次 3D 判断，但说明官方文档本身也需要按快照审阅。

本次没有把第三方评测或厂商宣传数字当成实测结论。Supervisely 的帧率与对象规模数据在文中统一标记为“厂商自测”。

### 1.3 证据分级

| 级别 | 证据                             | 本文如何使用                           |
| ---- | -------------------------------- | -------------------------------------- |
| A    | 当前官方源码、可复现的本项目代码 | 用于判断状态、快捷键、布局与功能边界   |
| B    | 当前官方文档、教程和演示媒体     | 用于判断用户可见流程和产品意图         |
| C    | 产品博客、发布说明和性能宣传     | 用于发现新能力，数字只作为厂商声明     |
| D    | 从 A、B 证据推导的产品建议       | 明确写成“建议”或“推断”，不冒充产品事实 |

边界也要说清楚：Supervisely 的服务端和完整前端不是公开源码，无法像 CVAT 一样验证内部状态机；两家的企业版、云端灰度功能可能比公开材料更新；“当前未见”只表示本次固定快照没有暴露该能力，不表示产品历史上从未支持。

---

## 2. 两家平台的产品哲学

| 维度      | Supervisely                                          | CVAT                                        | 对本项目的启示                                                        |
| --------- | ---------------------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------- |
| 核心单元  | 数据集、episode、对象、应用和作业                    | task、job、frame、shape 和 track            | 本项目应继续以 task 为执行单元，但 3D UI 要提升到 scene 与 track 视角 |
| 3D 主形态 | 点云 episode 与多相机融合工作台                      | 以 cuboid 为中心的四视图工作台              | 不必二选一，可以保留本项目多模态深度，同时学习 CVAT 的精修确定性      |
| AI 角色   | 工具本身的一部分，覆盖自动框、智能框、地面分割、跟踪 | 3D 当前公开工作台以人工编辑为主             | AI 必须有稳定的预览、接受、拒绝和撤销过程，而不是单独的批处理入口     |
| 布局      | 面板多、可移动、可缩放、可重置                       | 固定四视图骨架，加上下文图像后重排          | 用“预设加有限自由度”平衡效率与可恢复性                                |
| 时序      | episode 时间轴、对象存在、密度和跟踪进度             | frame 导航与 track 插值                     | 场景时间轴是本项目下一阶段最有价值的结构性升级                        |
| 质量      | sample QC、Issues、Consensus、绩效                   | GT、自动 QA、即时反馈、Consensus、Analytics | 复用现有 review 壳，但新增 3D 几何规则与跳转定位                      |

Supervisely 更像把很多专业工具装进一张工作台；CVAT 更像把少数动作打磨成稳定的编辑器。前者提醒我们“流程要完整”，后者提醒我们“每个动作都要可预测”。

---

## 3. Supervisely：值得拆解的工作台交互

### 3.1 信息架构不是单一画布，而是一个可编排工作区

当前 [3D Point Cloud and Episodes](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2) 工作台把以下信息同时放在一张桌面上：

- 左侧工具栏：选择、cuboid、AI、测量、点级分割等高频操作。
- 中央点云视口：透视视图与正交视图并存。
- 右侧 Definitions、对象列表和设置：类别、对象实例、显示与工具参数。
- 底部时间轴与对象表：帧存在性、对象出现区间、轨迹和批处理进度。
- 相机图像面板：作为可编辑的传感器视图，而不只是静态参考图。

面板可以移动、停靠、缩放和重置。这个自由度适合多屏和多传感器任务，但也有明显风险：如果没有预设与恢复入口，用户很容易把布局拖成不可复用的个人状态。

本项目已经有可拖拽、缩放、折叠并持久化的三正交浮层。应当保留这项基础，但下一步要增加三种命名预设，而不是继续增加任意面板：

1. **框体精修**：主透视视口加三个紧凑正交视图。
2. **传感器融合**：主点云视口加一到两个高权重相机面板。
3. **点级分割**：主视口最大化，正交视图缩小，掩码工具与图层面板前置。

### 3.2 持续建框循环比单次自动框更重要

Supervisely 的 Auto Cuboid 演示展示了一个非常完整的高频循环：进入 Auto Labeling，保持“click highlight”处于待命状态，点击目标后生成 cuboid，生成对象自动成为当前选中项，但自动工具没有退出。按 `Space` 取消选择后，待命框重新附着到光标，可以继续点下一个目标。[Auto Cuboid 文档与演示](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2#auto-cuboid-tool)体现的关键不是模型多强，而是工具状态跨对象保持。

```text
选择类别并激活自动框
          ↓
工具保持 armed 状态
          ↓
点击目标 → 生成并选中框 → 人工精修
          ↓
Space 完成当前对象
          ↓
回到 armed 状态，继续下一个目标
```

这比“点击 AI 按钮，弹窗，生成一次，退出”少了大量状态切换。对本项目而言，可以先用现有点聚类与自动拟合实现，不必等待新模型。界面必须始终显示“自动拟合待命中”，避免用户不知道下一次点击会发生什么。

### 3.3 一个工具同时支持点选和笔刷，减少模式切换

[Point Cloud Pen](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2#point-cloud-pen) 把两种输入合并在同一个工具里：

- 单击逐点形成多边形边界。
- 按住拖动直接像画笔一样涂选。
- 同一工具可以创建、编辑、添加和移除点。

这种“输入手势决定细分行为”的设计适合点级分割，因为用户经常在大轮廓与局部孔洞之间切换。本项目已有矩形、套索、多边形、添加和移除，能力面不弱。真正值得借鉴的是把最常用的多边形和笔刷放进同一个临时手势层，而不是再增加一个模式按钮。

风险是误触。落地前需要明确区分：短按为落点，超过拖动阈值才进入笔刷；笔刷半径要与屏幕像素关联，而不是随相机缩放变成完全不同的世界尺寸。

### 3.4 地面分割、测距和几何特征都是“先理解场景，再标对象”

Supervisely 的 3D 工具箱提供了三类场景理解能力：

- **Ground Segmentation**：可选 Patchwork++、GndNet、分位数、平面拟合和网格坡度等方法，先预览，再应用。
- **Measure Distance**：以折线方式显示 XYZ、分段长度与累计距离。
- **Geometric Features**：显示 verticality、planarity、linearity 等点几何特征。

它们不是标注类型，却直接降低了“这个点属于地面还是对象”“尺寸是否合理”“哪里是杆状或平面结构”的判断成本。对本项目的正确借鉴方式是把它们设计成可撤销的辅助层：

1. 参数变化只更新预览，不立刻改标注。
2. 应用时记录算法、参数、输入帧和结果摘要。
3. 地面结果既可以作为显示过滤，也可以作为自动拟合的排除条件。
4. 测距结果默认不进入导出，只有显式“转为标注”才持久化。

### 3.5 2D 与 3D 是同一个对象，不是一次性的建框提示

[Sensor Fusion](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2#sensor-fusion) 的关键逻辑是：相机图像里的 bbox、polygon、brush 或 Smart Tool 结果会保留在 Definitions 与时间轴中，并可转换为 3D 对象。官方材料把 2D 与 3D 描述为同一对象实例的双向同步，而不只是“用 2D 框算出一个 3D 框”。

本项目已经具备标定投影和从相机 2D 区域生成 3D 框的能力，但当前用户文档明确写着 2D 手势的唯一产物是 3D 框，2D 成员本身不持久化。两者的差别会直接影响：

- 用户能否回看“这个 3D 框最初来自哪个相机区域”。
- 修改 3D 尺寸后，2D 投影是计算结果还是可独立修正的标注成员。
- 数据导出时能否保留多相机可见性、遮挡和跨模态审计记录。

建议保留当前“快速种子”模式，同时增加可选的“持久化多模态对象”模式。数据层可利用现有 `track_id` 作为共同身份，但 2D 成员必须有自己的几何、相机、帧、来源和修订记录。

### 3.6 episode 时间轴承担的是场景状态，不只是换帧

从数据格式看，Supervisely 还把 episode 级 `objects[]` 与逐帧 `figures[]` 分开，figure 通过 `objectKey` 归属稳定对象。这说明对象侧栏、时间轴和逐帧几何应共享一个 Scene 级身份来源，而不是各自在 Annotation 列表上临时聚合。缺席区间、人工锚点和可逆修复的跨平台深读见[时序对象补充报告](./24-3d-temporal-object-lifecycle.md)。

Supervisely 的底部时间轴同时表达：

- 哪些帧有数据。
- 对象在哪些帧存在。
- 当前帧和附近帧的标注密度。
- 跟踪或批处理的运行区间与进度。
- 全局 tag 与 frame tag。

这比简单的上一帧、下一帧更接近 3D 生产工作流。特别是“对象存在区间”和“异步任务进度”能减少用户在帧间盲找。

本项目已有跨帧传播、插值、邻帧框和邻帧点云加密，但缺少一个能解释这些状态的场景级视图。建议先做只读时间轴，不急着在第一版支持复杂拖拽编辑：对象出现区间、关键帧、待确认候选、缺失帧和异步任务进度已经足够产生价值。

### 3.7 跟踪的“选中对象或全部对象”很高效，也容易歧义

Supervisely 的 tracking 逻辑是：有选中对象时跟踪选中对象，没有选中对象时跟踪全部对象，并可指定方向和帧数。进度同时出现在按钮和时间轴。基于 registration 的跟踪演示强调不需要针对类别训练模型。

这里值得借鉴的是批处理范围与进度反馈，不值得照搬的是隐式范围规则。对本项目应使用明确的两个动作：

- `跟踪选中对象（3）`
- `跟踪当前场景全部对象（127）`

按钮文案必须随选择集变化。任务还要具备取消、重试、失败帧清单和幂等性，不能只显示一个不可恢复的 spinner。

### 3.8 2D 与视频工作台里的几个快赢

Supervisely 的 2D 与 [Video 3.0](https://docs.supervisely.com/labeling/labeling-toolbox/videos-3.0) 也有一些适合本项目的细节：

- 类别行本身就是工具入口，选类别的同时确定几何工具。
- bbox 同时支持 Corner Drag 与按 `Ctrl` 的 Center Out。
- polygon 支持边界吸附、连接、按住 `Shift` 自动布点、孔洞和多多边形。
- Smart Tool 支持正负点、框提示、模型切换和自定义模型。
- 视频同时提供全局时间轴与局部时间轴，轨迹、当前对象和快速动作分层表达。
- 跟踪接近片段末尾时自动延长，人工修正后可从修正点重新跟踪，对象离屏时停止。

本项目在 SAM 点提示、框提示、exemplar、多种 mask 编辑、视频 mask track 和候选审核上已经有较强基础。建议只补能减少高频切换的细节，例如类别行作为工具、双层时间轴和 Center Out，不要为了对齐清单重做已有能力。

### 3.9 质量控制已经不再只是统计看板

当前 Supervisely 文档包含三条互补路径：

- [Labeling Quality Control](https://docs.supervisely.com/labeling/jobs/labeling-quality-control)：对抽样结果检查类别、几何和 tag，缺标时可画 Error Region。
- [Labeling Consensus](https://docs.supervisely.com/labeling/jobs/labeling-consensus)：支持 bbox、mask、polygon 和 tag 的多人一致性比较，提供成对矩阵与明细。
- Issues：问题可直接挂在画布对象上，保留评论、分派和历史。

这意味着旧调研中“Supervisely 没有 consensus”的结论已经过期。需要注意的是，当前公开材料仍未展示面向 3D cuboid、点掩码和跨模态对象的完整 Consensus 合同。

### 3.10 性能声明应当学习架构，不应照抄数字

[Performance Upgrade for Dense Clouds](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2/3d-point-cloud-optimizations) 介绍了 WebGPU、3D tiling、可见与邻近 tile 加载，并给出 5,000 万到 1 亿点、上千对象的厂商自测数据。主 3D 页面则以每帧最多 5,000 万点作为产品表达。

这些数字没有独立复现条件，不应成为本项目的承诺。真正值得借鉴的是数据路径：

```text
整文件加载
    ↓  当前本项目主要路径
完整解析后按步长抽样

空间流式路径
    ↓  推荐方向
离线切片或空间索引 → 视锥判断 → 可见 tile → 邻近预取 → Worker 解码 → GPU 缓存
```

先建立可重复基准，再选择 octree、固定网格或其他索引。没有数据集、机器、相机轨迹、显存与编辑延迟口径的“120 FPS”没有决策价值。

### 3.11 WebGPU 与点云颜色路径的补充核验

前述调研已经记录了 Supervisely 使用 WebGPU，但没有拆开“渲染大点云”和“相机给点云上色”这两个问题。补充核验后的证据边界如下：

| 对象        | 公开实现能确认的事实                                                                                                                                                                                                                                                                                                                                                                                 | 不能确认的部分                                                                                              | 可迁移机制                                                           |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Supervisely | [性能升级文档](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/3D-Point-Clouds/3D-point-cloud-episodes-2/3D-point-cloud-optimizations.md)明确称渲染管线从 WebGL 迁到 WebGPU，颜色模式、点大小和对象重着色直接在 GPU 完成；[3D 总览](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-clouds)说明大场景切成 3D tiles，只加载可见与邻近 tile | 完整前端闭源，无法确认相机 JPEG/PNG 是否在 shader 中逐点直接采样，也无法核对标定、遮挡、fallback 与显存预算 | 显示属性留在 GPU；大场景把可见性加载和渲染后端分开治理               |
| CVAT        | 固定快照的 [`cvat-canvas3d`](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-canvas3d/src/typescript/canvas3dView.ts)使用 Three.js `WebGLRenderer`、`PCDLoader`、`PointsMaterial` 和 PCD vertex color；透视与三个正交视图分别建 renderer                                                                                                                          | 没有 WebGPU 依赖，也没有标定相机给点云逐点上色的公开路径；contextual images 只是参考图                      | WebGL 路线仍可提供确定的 cuboid 编辑；不能拿它回答六相机上色性能问题 |
| Potree      | [公开 point-cloud vertex shader](https://github.com/potree/potree/blob/develop/src/materials/shaders/pointcloud.vs)在点材质中查询 visible-node、classification、gradient 与 shadow-map texture                                                                                                                                                                                                       | 不是标注工作台，也没有本项目的多相机选择和标定合同                                                          | 自定义点材质直接采样 texture 是成熟的 GPU 显示模式                   |

Three.js 当前[官方 WebGPU 迁移说明](https://threejs.org/manual/en/webgpurenderer)明确：`WebGPURenderer` 要求异步初始化，现有自定义 shader 必须迁到 TSL/node material，并可在 WebGPU 不可用时使用 WebGL2 backend；官方也提醒部分场景不一定比 `WebGLRenderer` 更快。[ImageBitmapLoader](https://threejs.org/docs/pages/ImageBitmapLoader.html)提供异步、面向 texture 的图片准备路径。当前版本的 `PointsNodeMaterial` 还限制 WebGPU point primitive 为 1 pixel，因此保留可调点大小需要实例化 sprite/quad，不能把现有 `Points` 原样换 renderer。

基于当前产品对切帧和选框卡顿的容忍度，平台已提供完整 3D WebGPU 实验 Pilot：主视图与三视图共同迁移，TSL 直接采样相机 texture，并保留默认关闭的 Legacy 对照。此处仍是待实测的产品决策，不把 Supervisely 的厂商数据当成本项目收益；只有真实 nuScenes A/B 和跨设备资格通过后，才另行讨论转为正常功能。

---

## 4. CVAT：值得拆解的编辑器确定性

### 4.1 四视图骨架让 cuboid 精修有固定空间模型

CVAT 的 [3D Task Workspace](https://docs.cvat.ai/docs/annotation/annotation-editor/3d-task-workspace/) 以一个大透视视图和 Top、Side、Front 三个正交视图组成稳定骨架。源码中的默认网格是透视视图占 12 x 9，三个正交视图各占 4 x 3；加入 contextual image 后才根据内容重排。[布局配置源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/canvas/grid-layout/canvas-layout.conf.tsx)与[网格实现](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/canvas/grid-layout/canvas-layout.tsx)印证了这一点。

正交视图不是独立浏览器，而是围绕当前选中的 cuboid 展开。这减少了用户在大场景中反复寻找同一对象的成本，也明确了“主视图负责导航，正交视图负责精修”的分工。

本项目的正交浮层自由度更高，但缺少这种稳定的对象中心语义。建议增加“锁定到当前对象”开关，并默认开启。

### 4.2 3D 创建流程窄，但每一步都很明确

[3D Object Annotation](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/3d-object-annotation/) 的主要流程是：

1. 明确选择 Shape 或 Track。
2. 激活 cuboid 后，候选框随光标移动。
3. 放置对象。
4. 在正交视图通过四角控制点、中心旋转点精修。
5. 必要时输入精确 X、Y、Z 尺寸或调整方向。

Track 是显式模式，并以插值和稳定 ID 连接帧间对象。复制后粘贴的 cuboid 会等待用户双击确定新位置，而不是在旧坐标直接生成。方向轴、精确尺寸、复制与放置组合起来，使批量标相似车辆时非常顺手。

当前公开 3D 控制栏只暴露 Cursor、Move、Draw Cuboid、Merge、Group、Split。对应快捷键包括 `N`、`Shift+N`、`G`、`Shift+G`、`M`、`Alt+M` 和 `Ctrl+V`。[控制栏源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard3D-workspace/controls-side-bar/controls-side-bar.tsx)说明它刻意维持了较窄的工具面。

### 4.3 六个小交互比新增六个工具更有价值

CVAT 当前点云更新说明重点强调了六类微交互：

1. 双击 cuboid 聚焦对象。
2. 正交视图采用一致缩放，并按对象记住缩放状态。
3. 控制点尺寸更稳定且可配置。
4. 投影视图变化时，相机位置不跳变。
5. 提高边线、控制点和背景之间的对比度。
6. 扩大可缩放范围，改进触控板缩放算法。

这些行为也能在 [`canvas3dView.ts`](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-canvas3d/src/typescript/canvas3dView.ts) 找到对应实现：双击触发对象 fit、正交视图保存 per-object zoom、控制点按屏幕关系缩放、透视视图使用相机控制器处理 rotate 与 truck。

这是本项目最应该优先复制的部分。当前 `ThreeDWorkbench` 的双击只用于结束点掩码多边形，没有“聚焦选中框”的默认路径；已有 TransformControls 尺寸自适应，因此不必重写 gizmo，只需补齐聚焦、正交缩放记忆和状态保持测试。

### 4.4 contextual image 是参考面板，不是传感器融合

CVAT 的 [Contextual Images](https://docs.cvat.ai/docs/annotation/manual-annotation/utilities/contextual-images/) 最多可挂 12 张图，默认显示 3 张，支持拖动、缩放、全屏、fit 和 reload。它非常适合查看多角度照片或局部细节。

但当前公开流程没有展示相机内外参、3D 到 2D 投影、2D 反向选中或同一对象的双向编辑。因此更准确的描述是“参考图像工作区”，不是标定后的 sensor fusion。

本项目已经有标定相机覆盖层和 2D 命中选择，不能为了界面像 CVAT 而退回非标定图片。可以学习它的面板管理和全屏操作，但必须保留几何关联。

### 4.5 通用 2D 编辑器的成熟点

CVAT 的强项仍然是通用 annotation editor：

- Objects sidebar 提供锁定、隐藏、遮挡、置顶、层级、组、合并、拆分和传播。
- 类别级隐藏与锁定、对象过滤和排序可以快速处理拥挤场景。
- 图层栈支持折叠、编号和对象移动。
- polygon 有吸附，mask 有 brush、Slice 和 Join。
- Quick Issue 可从画布右键直接创建，减少审核上下文切换。

本项目的 mask 工作台、SAM 辅助和视频 track 已经覆盖不少能力，但 Slice 仍是明确缺口。建议将 CVAT 的 layers、可复用过滤器和 Slice 当作独立小课题，不要与 3D 主线绑在同一版本。

### 4.6 QA 很强，但当前公开边界对 3D 不友好

CVAT 的 QA 体系是本次调研里最完整的之一：

- [Quality Control](https://docs.cvat.ai/docs/qa-analytics/quality-control/) 以 ground truth 与冲突为中心。
- [Immediate Feedback](https://docs.cvat.ai/docs/qa-analytics/immediate-feedback/) 可把 honeypot 帧混入普通任务，并限制尝试次数。
- [Automatic QA](https://docs.cvat.ai/docs/qa-analytics/auto-qa/) 支持阈值、冲突类型和指标配置。
- [Consensus](https://docs.cvat.ai/docs/qa-analytics/consensus/) 支持 replicas、投票和合并。
- Analytics 可按 project、task、job 查看工作时间、速度、关键帧、track 和事件。

但当前官方边界非常重要：

- Manual Review 文档明确不适用于 3D。
- Consensus 当前只覆盖 2D，排除了 cuboid；合并会覆盖父任务结果，且不可撤销。
- 在本次审阅的当前公开工作台、文档和源码路径中，没有暴露点级 3D mask、标定后的 2D 与 3D 双向编辑、地面分割或 3D AI 助手。

这为本项目提供了差异化窗口：不要只给 3D 加一个“提交审核”按钮，而要把 3D 几何异常、投影误差和时序异常做成可定位的问题类型。

---

## 5. 3D 工作台状态机对比

### 5.1 Supervisely：工具保持和场景任务并存

```text
场景状态
  ├─ 当前帧、对象集合、相机集合、时间轴
  ├─ 当前选择集
  ├─ 当前持久工具，例如 Auto Cuboid 或 Point Cloud Pen
  └─ 后台任务，例如 tracking、registration、AI

用户动作
  ├─ 选对象 → 正交视图、图像面板、Definitions 同步
  ├─ 完成对象 → 工具仍待命，可继续下一个对象
  └─ 启动后台任务 → 按钮与时间轴同时反馈进度
```

优势是连续生产效率高。风险是状态很多，如果没有醒目的 armed、selected、running 提示，误操作成本也高。

### 5.2 CVAT：选择驱动的 cuboid 精修

```text
选择 Shape 或 Track
       ↓
放置 cuboid
       ↓
选中对象成为四个视图的共同中心
       ↓
控制点、旋转点、精确尺寸完成精修
       ↓
切帧插值，或复制后双击放置
```

优势是每一步确定、可测试。局限是场景理解、点级编辑和多模态对象都不在主状态机里。

### 5.3 本项目：能力较多，但还缺统一的场景状态层

当前本项目实际已经包含：

- `box_3d` 与 `point_mask_3d` 两类 3D 标注。
- 通过屏幕矩形选择真实点并拟合框，也支持点击创建默认框。
- W、E、R gizmo，Position、Size、Rotation 数值编辑。
- 可拖拽、缩放、折叠并持久化的三正交视图。
- 标定后的 3D 到 2D 投影、相机视图命中和选择同步。
- 从相机 2D 区域通过视锥与深度聚类生成 3D 框。
- 点掩码的矩形、套索、多边形、添加和移除，保留原始点索引。
- 自动拟合、尺寸估计、地面过滤和 PCA yaw。
- 传播、插值、ego motion 补偿、邻帧框和邻帧点云加密。
- RGB 着色与深度热力图。

这些能力分布在局部工具、帧导航、浮层和菜单里，但还没有一个明确的 scene 状态层解释“对象在哪些帧存在、哪些结果待确认、哪些异步任务正在运行”。这正是场景时间轴和任务中心的价值。

---

## 6. 本项目能力基线与真实差距

以下判断同时核对了当前文档和源码：

| 领域         | 当前已有                                        | 相比 Supervisely                      | 相比 CVAT                        | 下一步                               |
| ------------ | ----------------------------------------------- | ------------------------------------- | -------------------------------- | ------------------------------------ |
| 3D cuboid    | 创建、gizmo、数值编辑、自动拟合、复制和批量操作 | 缺持续自动框和 3D AI 工具链           | 功能面更宽，缺对象聚焦和缩放记忆 | 先补微交互，不重做编辑器             |
| 三正交视图   | 可拖拽、缩放、折叠和持久化                      | 布局预设与任务上下文较弱              | 自由度更高，稳定骨架较弱         | 加对象锁定和三种预设                 |
| 点级 3D mask | 多种选区、增删、原始点索引                      | 已有基础，缺混合 Pen 手势和语义辅助层 | 当前公开 CVAT 3D 未见同类能力    | 保持优势，补交互与质检               |
| 相机融合     | 标定投影、相机覆盖层、反向命中、2D 建 3D        | 2D 成员没有持久化，尚非同一多模态对象 | 明显强于 contextual image        | 设计同一对象的数据合同               |
| 跨帧         | 传播、插值、ego 补偿、邻帧点云                  | 缺 registration 跟踪任务与完整时间轴  | 比线性插值更深入                 | 补进度、失败恢复和 selected/all 范围 |
| 3D QA        | 可进入通用 review 流程                          | 缺专属几何、时序和投影规则            | 有差异化机会                     | 新建 3D QC 规则与跳转定位            |
| 大点云性能   | 按性能档位渲染 25 万、50 万、100 万点           | 缺 tiling、可见性加载和大场景基准     | CVAT 也不是完整流式架构样板      | 建空间流式加载                       |
| 3D AI        | LiDAR 当前不在 AI 预标注支持范围                | 差距明显                              | CVAT 当前公开 3D 也较弱          | 先做算法型助手，再接模型插件         |

关键本地证据：

- [`3d-box.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/3d-box) 描述框体创建、gizmo、自动拟合与跨帧操作。
- [`pointcloud-view.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/pointcloud-view) 描述点云显示与视图能力。
- [`pointcloud-projection.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/pointcloud-projection) 描述标定投影、相机覆盖层和当前的 2D 建 3D 合同。
- [`pointcloud-crossframe.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/workbench/pointcloud-crossframe) 描述传播、ego 补偿与邻帧点云。
- [`PointCloudScene.ts`](../../apps/web/src/pages/Workbench/stages/three-d/PointCloudScene.ts) 明确将真正的 LOD 与 tiling 留作后续，当前路径是完整 PCD 解析后按步长抽样。
- [`performanceTier.ts`](../../apps/web/src/pages/Workbench/state/performanceTier.ts) 把渲染点数分为 25 万、50 万和 100 万三个档位。
- [`ai-preannotate.md`](https://yoao-labs.github.io/YOAO-Docs/user-guide/projects/ai-preannotate) 当前明确不支持 LiDAR 预标注。

需要特别避免两个误判：

1. 本项目的 TransformControls 已按距离和对象尺寸做屏幕稳定处理，不能笼统写成“控制柄完全不稳定”。真正缺的是跨对象与跨投影的稳定性测试。
2. 本项目的跨帧传播已经包含 ego-aware 路径，不能用 CVAT 的线性插值取代它。线性插值只能作为便宜、可解释的后备方案。

---

## 7. 能力对比矩阵

符号说明：`●` 为当前公开材料中完整可用，`◐` 为部分或受限，`○` 为本次公开快照中未见。

| 能力                    | Supervisely | CVAT             | 本项目                       | 判断                                 |
| ----------------------- | ----------- | ---------------- | ---------------------------- | ------------------------------------ |
| 透视加三正交视图        | ●           | ●                | ●                            | 已是行业稳定形态，重点转向状态保持   |
| 布局拖拽与缩放          | ●           | ●                | ●                            | 本项目无需补能力，需补预设与 reset   |
| 双击对象聚焦            | ●           | ●                | ○                            | P0 快赢                              |
| per-object 正交缩放记忆 | 未确认      | ●                | ○                            | P0 快赢                              |
| cuboid 精确尺寸与方向   | ●           | ●                | ●                            | 保持                                 |
| 持续自动建框            | ●           | ○                | ○                            | P0 高频效率路径                      |
| 点云智能框              | ●           | ○                | ◐ 算法拟合                   | P1，从现有聚类拟合演进               |
| 点级 3D mask            | ●           | ○                | ●                            | 本项目现有优势                       |
| 混合 Point Cloud Pen    | ●           | ○                | ◐                            | P2 交互升级                          |
| 地面分割预览            | ●           | ○                | ◐ 拟合时过滤                 | P1，升级为用户可见辅助层             |
| 测距与几何特征          | ●           | ○                | ○                            | P1 小而实用                          |
| 标定 2D 与 3D 同步      | ●           | ○                | ◐ 3D 到 2D 强，2D 成员不持久 | P1 数据合同                          |
| contextual images       | ●           | ●                | ●                            | 本项目应坚持标定关系，不退化为参考图 |
| 场景时间轴              | ●           | ◐                | ◐                            | P0 结构性升级                        |
| track 与插值            | ●           | ●                | ●                            | 本项目已有更丰富传播路径             |
| registration 跟踪任务   | ●           | ○                | ◐                            | P2，补任务语义而非替换现有传播       |
| 3D 专属 QA              | ◐           | ○                | ○                            | 最有价值的差异化方向                 |
| 2D GT 与即时反馈        | ◐           | ●                | ◐                            | 可复用现有质量体系演进               |
| 2D Consensus            | ●           | ●                | ◐ 数据基础在建设中           | 不与 3D 第一版强绑定                 |
| 空间 tiling             | ●           | 未作为当前主卖点 | ○                            | P1 性能基础设施                      |
| WebGPU 3D               | ●           | ○                | ◐ 实验 Pilot 已实现          | 默认关闭，以真实 A/B 决定是否转正    |

---

## 8. 推荐路线图与验收标准

### 8.1 P0：先把现有 3D 能力变得更快、更稳、更可解释

#### P0-A 对象聚焦与视图稳定

**借鉴来源**：CVAT。

实现范围：

- 在非绘制状态双击 cuboid，主视口 fit 到对象。
- 三个正交视图以当前对象为中心。
- 每个对象记住正交缩放；首次进入使用统一默认缩放。
- 切换正交方向、切帧、开关相机面板时，不无故重置透视相机。
- 点掩码多边形仍可双击结束，按当前工具状态消解冲突。

验收标准：

- 连续精修 20 个尺度差异明显的 cuboid，不需要手动把对象重新找回画面。
- 在 20 次对象切换后，回到原对象时三个正交视图的缩放误差不超过一个离散档位。
- 绘制点掩码时双击只结束多边形，不触发对象聚焦。
- 鼠标滚轮与触控板缩放分别有回归用例。

#### P0-B 三种布局预设和一键恢复

**借鉴来源**：Supervisely 的自由面板与 CVAT 的稳定骨架。

实现范围：框体精修、传感器融合、点级分割三种预设；保留用户拖拽后的持久化；提供“一键恢复当前预设”。

验收标准：

- 从任意混乱布局到可工作的标准布局最多一次点击。
- 预设切换不改变相机、当前帧、选择集和未保存标注。
- 1,366 x 768 与 1,920 x 1,080 下不存在遮住保存、提交和工具退出入口的面板。

#### P0-C 持续创建与持续自动拟合

**借鉴来源**：Supervisely Auto Cuboid。

实现范围：类别与工具保持激活；完成当前对象后继续待命；界面显示 armed 状态；`Esc` 明确退出，`Space` 完成并继续。

验收标准：

- 同一类别连续标 20 个对象，只需要一次工具激活。
- 生成失败不会退出工具，并给出可恢复原因。
- 用户随时能看出下一次点击会选择对象、创建默认框还是运行自动拟合。
- 所有自动结果进入同一 undo 栈。

#### P0-D 场景时间轴第一版

**借鉴来源**：Supervisely episode。

第一版只做高价值、低歧义状态：

- 当前帧、关键帧、对象存在区间。
- 每帧标注对象数量与待确认候选数量。
- 传播、插值、AI 或跟踪任务的进度和失败帧。
- 点击区间跳到对应帧并选中对象。

验收标准：

- 用户能在不逐帧翻看的情况下找到 track 断点和异常密集帧。
- 后台任务取消后不留下“永久运行中”状态。
- 1 万帧场景的时间轴只渲染可视区，不为每帧创建常驻 DOM 节点。

#### P0-E 3D 专属质量模式

**借鉴来源**：CVAT 的冲突定位、Supervisely 的 Error Region 与 Issues，但规则要针对本项目。

第一批规则建议：

| 规则           | 检测内容                           | 点击后的定位                    |
| -------------- | ---------------------------------- | ------------------------------- |
| 空框或点数过少 | cuboid 内点数低于项目阈值          | 跳到帧、聚焦框、显示框内点      |
| 穿地或悬浮     | 框底面与估计地面差值异常           | 显示地面辅助层和高度差          |
| 尺寸异常       | 类别尺寸超出分位范围               | 打开尺寸编辑并显示同类范围      |
| 投影残差       | 多相机投影与持久化 2D 成员偏差过大 | 打开误差最大的相机视图          |
| 时序跳变       | 中心、尺寸或 yaw 突变              | 同时显示前后帧框与 ego 补偿结果 |
| track 断点     | 存在区间内缺帧或 ID 异常           | 跳到断点并给出插值、传播入口    |
| 点掩码重叠     | 互斥类别共享点索引                 | 高亮冲突点和两个对象            |

验收标准：

- 每条问题都能一键恢复到帧、对象、相机和辅助层上下文。
- 自动规则只创建 issue，不直接改标注。
- 接受、驳回、忽略都记录操作者、时间、规则版本和原因。

### 8.2 P1：建立多模态和大场景的基础设施

#### P1-A 空间分块与流式加载

推荐顺序：

1. 固定三套公开或内部可长期保存的数据集：100 万、1,000 万、5,000 万点。
2. 记录首屏时间、稳定 FPS、峰值内存、显存、对象选中延迟、点掩码编辑延迟。
3. 设计离线 tile manifest，包含包围盒、点数、属性、层级和字节范围。
4. 用视锥和屏幕误差选择 tile，预取相邻 tile，在 Worker 解码。
5. 对编辑中的点索引建立“全局点 ID 到 tile 局部索引”映射，避免点掩码失真。
6. 用默认关闭的完整 WebGPU Pilot 与 Legacy 路径做同机 A/B；只有渲染、选框和切帧均达到门槛后才讨论转正。

验收标准不应写成“达到厂商 120 FPS”，而应写成：同一机器、同一轨迹下，5,000 万点数据首屏无需完整下载，镜头移动峰值内存受控，选框与掩码操作没有因 tile 换入而丢点。

#### P1-B 持久化多模态对象

建议数据关系：

```text
track_id / object_id
  ├─ 3D cuboid member
  ├─ point mask member
  ├─ camera A 的 bbox 或 mask member
  ├─ camera B 的 bbox 或 mask member
  └─ frame、visibility、occlusion、source、revision
```

需要先决定三件事：

1. 2D 成员是独立真值，还是始终由 3D 投影生成。
2. 2D 与 3D 冲突时谁有权覆盖谁。
3. 相机标定版本变化后，旧成员如何重投影与审计。

推荐默认：3D 与人工 2D 成员都是真值，投影只是关联证据；自动投影结果不自动覆盖人工几何。

#### P1-C 3D AI 助手合同

不要从“接哪个大模型”开始，应先固定交互合同：

- click 或圈选目标。
- 生成一个或多个候选 cuboid。
- 显示置信度、点数、模型或算法版本。
- 可在预览态调整，接受后才进入正式标注。
- 拒绝原因可回流评估，但不阻塞当前标注。

第一阶段直接包装现有点聚类、地面过滤与 PCA yaw。第二阶段再接远程模型插件。这样即使模型暂时不可用，工作流仍能被真实用户验证。

#### P1-D 地面、测量和几何辅助层

三项可以共享一套非标注 overlay 基础设施：有图例、参数、可见性、缓存、撤销边界和“转为标注”入口。先做测距与地面预览，几何特征后置。

### 8.3 P2：扩大专业效率和质量覆盖

- Point Cloud Pen：把多边形点击与笔刷拖动合并，但保留明显的拖动阈值与屏幕半径。
- registration 跟踪任务：在现有传播之上增加 selected/all 范围、进度、取消、重试和失败帧，不替换 ego-aware 路径。
- 3D GT 与 Consensus：先固定 cuboid 匹配、点掩码 IoU、track 连续性和投影残差的口径，再做多人副本与合并。
- 2D 快赢：类别行即工具、Center Out bbox、对象 layers、可复用过滤器、mask Slice、视频双层时间轴。

---

## 9. 明确不建议照搬的设计

| 设计                                     | 为什么不照搬                           | 本项目应怎样处理                                         |
| ---------------------------------------- | -------------------------------------- | -------------------------------------------------------- |
| CVAT 非标定 contextual images            | 只能做视觉参考，丢失几何关系           | 学面板交互，保留本项目内外参与投影合同                   |
| 只靠线性插值的 track                     | 对有 ego motion 的点云不够稳           | 继续以 ego-aware 传播为主，线性插值为后备                |
| 依赖大量组合键的 3D 工具                 | 新用户难发现，浏览器和系统快捷键易冲突 | 主动作必须可见，快捷键作为加速层                         |
| “有选择跟选中，无选择跟全部”的隐式批处理 | 高效但误操作半径很大                   | 按钮直接写出对象数量和范围                               |
| 无限自由的面板布局                       | 多人协作时难复现问题，支持成本高       | 预设为起点，自由拖拽为增量，一键 reset                   |
| 厂商自测的点数与 FPS                     | 缺少数据、相机轨迹和硬件口径           | 用自己的可重复基准定义性能预算                           |
| 为“媒体很多”而把教程塞进工作台           | 会挤压编辑空间，也让帮助内容失去层次   | 首次任务给短引导，工具内给一张动图，完整教程留在文档中心 |

还要避免一个常见误区：Supervisely 的高价值不在深色主题、多面板或图标样式；CVAT 的高价值也不在四块画布的外观。真正可以迁移的是状态规则、动作顺序、错误恢复和视图之间的同步语义。

---

## 10. 教程与媒体索引

以下不是把所有视频重新描述一遍，而是按可借鉴问题整理本次实际检查的重点媒体。

### 10.1 Supervisely 3D 视频

| 主题                | 媒体                                                                                                                                         | 观察重点                                    |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| 3D episode 快速入门 | [完整演示](https://www.youtube.com/watch?v=L3o6aE9JI7s)                                                                                      | 面板结构、Definitions、时间轴与对象流程     |
| 自动 cuboid         | [33 秒演示](https://www.youtube.com/watch?v=ysin0aIet5c)                                                                                     | 工具保持、点击目标、Space 进入下一对象      |
| Smart Tool          | [30 秒演示](https://www.youtube.com/watch?v=8iQj4GNvp5U)                                                                                     | 圈选目标后生成类别无关 cuboid               |
| Ground Segmentation | [基础演示](https://www.youtube.com/watch?v=XcLOjhXbBGQ)、[算法对比](https://www.youtube.com/watch?v=fdAkOeApzMI)                             | 算法选择、参数、预览与应用                  |
| Point Cloud Pen     | [编辑对象](https://www.youtube.com/watch?v=egdhoPKd10A)、[创建对象](https://www.youtube.com/watch?v=sLefC2-0r3w)                             | 点击与拖动混合手势、增删点                  |
| tracking            | [跨帧跟踪](https://www.youtube.com/watch?v=nDlaDzJkoRk)                                                                                      | selected/all 范围、方向、帧数和进度         |
| registration        | [注册前](https://www.youtube.com/watch?v=1kiMWHekwok)、[注册后](https://www.youtube.com/watch?v=nGAp3KqzCko)                                 | 相邻帧对齐对跟踪稳定性的影响                |
| 2D 到 3D            | [bbox 转 cuboid](https://www.youtube.com/watch?v=f6m7WJxMCAM)、[Smart Tool 转 3D](https://www.youtube.com/watch?v=sbo9FbyPza0)               | 相机图像编辑与 3D 对象关联                  |
| 几何与布局          | [几何特征](https://www.youtube.com/watch?v=3WqjGbkqqfU)、[布局调整](https://www.youtube.com/watch?v=TwpyWbaLfZY)                             | 场景理解和面板可恢复性                      |
| 点云着色            | [颜色模式](https://www.youtube.com/watch?v=oqXPLMAwz5M)                                                                                      | Z、RGB、距离和相机来源之间切换              |
| 大点云性能          | [性能升级文档与 4 个嵌入演示](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2/3d-point-cloud-optimizations) | WebGPU、dense cloud、Pen 与 Select 的响应性 |

### 10.2 CVAT 3D 视频

| 主题           | 媒体                                                                    | 观察重点                         |
| -------------- | ----------------------------------------------------------------------- | -------------------------------- |
| 当前产品总览   | [2025 工作台总览](https://www.youtube.com/watch?v=K2SV5MVCuB0)          | 当前导航、四视图与对象精修       |
| 2D cuboid      | [Academy practical task 1](https://www.youtube.com/watch?v=1FufWL-ql0E) | 2D 图像中的 cuboid 语义与快捷键  |
| 3D 点云 cuboid | [Academy practical task 2](https://www.youtube.com/watch?v=dQSnK1ZFSyU) | 放置、正交视图、控制点和尺寸修正 |
| 历史产品脉络   | [2023 product tour](https://www.youtube.com/watch?v=xLtolcT-nRM)        | 区分仍在当前版本中的交互与旧界面 |

CVAT 文档中的关键 GIF 还覆盖了放置 cuboid、四视图调整、缩放、旋转、复制粘贴、方向、精确尺寸和 contextual image 拖动。对应入口集中在 [3D Object Annotation](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/3d-object-annotation/) 与 [3D Task Workspace](https://docs.cvat.ai/docs/annotation/annotation-editor/3d-task-workspace/)。

### 10.3 2D、视频与 QA 媒体

- Supervisely 的 bbox、polygon、brush、Smart Tool 文档分别展示连续创建、吸附、孔洞、正负提示与模型切换，入口见 [Labeling Toolbox](https://docs.supervisely.com/labeling/labeling-toolbox)。
- Supervisely Video 3.0 的 GIF 展示全局与局部时间轴、对象轨迹、auto track 与修正后续跑，入口见 [Video 3.0](https://docs.supervisely.com/labeling/labeling-toolbox/videos-3.0)。
- CVAT annotation editor 的图片与 GIF 系统性展示 object sidebar、layers、过滤、group、merge、split、propagate、polygon snap、mask Slice 与 Join，入口见 [Annotation Editor](https://docs.cvat.ai/docs/annotation/annotation-editor/)。
- CVAT QA 媒体展示 GT job、冲突列表、即时反馈、replica 与 analytics 钻取，入口见 [Quality Control](https://docs.cvat.ai/docs/qa-analytics/quality-control/)。

---

## 11. 推荐的实施切片

如果只允许连续做三个工程切片，建议这样排：

### 切片一：3D 精修体验

双击聚焦、对象中心正交视图、per-object zoom、相机状态保持、三种布局预设、reset。这个切片不改数据模型，风险最低，也最容易用操作时长验证。

### 切片二：场景工作流

持续创建、场景时间轴只读版、selected/all 显式范围、传播和 AI 任务进度、失败恢复。这个切片把已有的跨帧算法变成可理解的产品流程。

### 切片三：3D 质量流程

先上点数、尺寸、穿地、悬浮、时序跳变、track 断点和点掩码冲突规则，再加投影残差。所有 issue 都必须支持一键跳回完整上下文。

空间流式加载和多模态对象需要先做设计与基准，可以与切片二、三并行调研，但不应在缺少数据合同和性能基线时直接开大规模重构。

建议用三个结果指标判断是否真的借鉴成功：

1. 同一数据集标 100 个 3D 框的中位操作时长下降多少。
2. 3D issue 从发现到定位所需的点击数和时间下降多少。
3. 1,000 万与 5,000 万点场景的首屏时间、峰值内存和编辑延迟是否进入明确预算。

---

## 12. 主要官方资料

### Supervisely

- [3D Point Cloud and Episodes](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2)
- [Performance Upgrade for Dense Clouds](https://docs.supervisely.com/labeling/labeling-toolbox/3d-point-cloud-episodes-2/3d-point-cloud-optimizations)
- [3D Point Clouds Overview](https://docs.supervisely.com/labeling/overview/3d-point-clouds)
- [3D LiDAR Sensor Fusion](https://supervisely.com/labeling-toolbox/3d-lidar-sensor-fusion/)
- [Video 3.0](https://docs.supervisely.com/labeling/labeling-toolbox/videos-3.0)
- [Labeling Consensus](https://docs.supervisely.com/labeling/jobs/labeling-consensus)
- [Labeling Quality Control](https://docs.supervisely.com/labeling/jobs/labeling-quality-control)
- [固定快照中的 3D 主文档](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/3D-Point-Clouds/3D-point-cloud-episodes-2/README.md)
- [固定快照中的性能升级文档](https://github.com/supervisely/docs/blob/846b10b4a903300dc4cffc7cf785ad95723a0cb4/labeling/3D-Point-Clouds/3D-point-cloud-episodes-2/3D-point-cloud-optimizations.md)

### CVAT

- [Annotation](https://docs.cvat.ai/docs/annotation/)
- [3D Task Workspace](https://docs.cvat.ai/docs/annotation/annotation-editor/3d-task-workspace/)
- [3D Object Annotation](https://docs.cvat.ai/docs/annotation/manual-annotation/modes/3d-object-annotation/)
- [Contextual Images](https://docs.cvat.ai/docs/annotation/manual-annotation/utilities/contextual-images/)
- [3D Point Cloud Annotation Blog](https://www.cvat.ai/resources/blog/3d-point-cloud-annotation)
- [Point Cloud Annotation Update](https://www.cvat.ai/resources/changelog/point-cloud-annotation-update)
- [Quality Control](https://docs.cvat.ai/docs/qa-analytics/quality-control/)
- [Consensus](https://docs.cvat.ai/docs/qa-analytics/consensus/)
- [Canvas 3D view 源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-canvas3d/src/typescript/canvas3dView.ts)
- [Standard 3D workspace 源码](https://github.com/cvat-ai/cvat/blob/cd392352e76fc314a4cb8c271ad18097224afb77/cvat-ui/src/components/annotation-page/standard3D-workspace/standard3D-workspace.tsx)

---

## 13. 最终判断

本项目不需要复制一个缩小版 Supervisely，也不需要把 3D 工作台改造成 CVAT 的外观。

正确路线是：

- 用 CVAT 的对象聚焦、正交缩放记忆、相机稳定和复制放置规则，打磨现有 cuboid 内核。
- 用 Supervisely 的持续工具、场景时间轴、多模态对象和后台任务反馈，补齐生产工作流。
- 把本项目已有的点掩码、标定投影、ego-aware 跨帧传播变成明确优势。
- 以 3D 专属 QA 和可定位 issue 建立差异化。
- 以空间流式加载替换“整文件解析后抽样”，但只在可重复基准下推进。

这样做的结果不是“功能数量更多”，而是让用户在大场景、多相机、长序列里始终知道：**当前正在标什么、下一次操作会发生什么、结果来自哪里、哪里可能有错，以及如何一键回到错误现场。**
