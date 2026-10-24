<div align="center">

<img src="./apps/web/public/ai-annotation-platform-icon.svg" alt="AI Annotation Platform" width="112">

<h1>AI Annotation Platform</h1>

<p><strong>A production-grade labeling control plane for image, video, point-cloud, and AI-assisted dataset operations.</strong></p>

<a href="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/ci.yml/badge.svg?branch=main"></a>
<a href="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/docs-deploy.yml"><img alt="Docs" src="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/docs-deploy.yml/badge.svg?branch=main"></a>
<img alt="React" src="https://img.shields.io/badge/React-18-61dafb?style=flat-square">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square">
<img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776ab?style=flat-square">
<img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-16-4169e1?style=flat-square">
<img alt="VitePress" src="https://img.shields.io/badge/docs-VitePress-42b883?style=flat-square">

<a href="https://yyq19990828.github.io/ai-annotation-platform/">Docs Site</a>
·
<a href="#快速开始">Quick Start</a>
·
<a href="#架构">Architecture</a>
·
<a href="./docs-site/api/">API</a>
·
<a href="./CHANGELOG.md">Changelog</a>

</div>

AI Annotation Platform 把项目管理、Data Manager、多模态标注工作台、AI 预标注、审核、导出、后台任务与可观测性放在同一条产品链路里。它不是一个只画框的 Demo，而是一个用于持续迭代标注生产系统的全栈仓库。

工作台支持按账号保存图片、视频和通用命令的自定义快捷键；类别直选使用 `1–9` 和 `0`，更多类别可搜索。使用方式见[工作台快捷键](./docs-site/user-guide/workbench/index.md#完整快捷键)，偏好读写契约见[认证与账号 API](./docs-site/api/guides/auth.md)。

项目负责人可通过[在线学习数据版本](./docs-site/user-guide/projects/online-learning.md)按检测或分割任务浏览项目派生的冻结版本，下载包含原图、类别映射和独立训练/验证划分的 Ultralytics 数据包。检测版本提供图片／类别／划分摘要、类别频次、尺寸与框分布热力图、每图目标数及版本差异，可对照前后冻结样本，或明确采用该版本新建实验；历史数据包和训练引用保持不变。接口见[训练数据版本 API](./docs-site/api/guides/training-datasets.md)。

删除无引用的数据版本和终态实验时，先持久保存清理责任，再移除文件；存储故障由后台继续处理。自动训练策略在保存时核验初始版本的任务与媒体资格，避免接受调度无法使用的父版本。

训练和推理统一使用 `task=detect|segment|classify|pose|obb`；AAP/COCO/YOLO 由 `format` 区分。混合标注项目可按任务分别抽取训练数据版本，保留工具、类别和媒体来源，参见[训练数据接口](docs-site/api/guides/training-datasets.md)。

候选模型可逐层查看数据、实验、精确尝试、部署与发布谱系，并追加研究、拒绝发布或归档说明；归档不会改变当前项目发布。

成功训练的权重可在同一页面注册为候选模型，完成类别绑定、认证推理实例加载及固定样本试用后发布。受控模型支持单阶段图片检测预标注，任务保留创建时的模型、实验和数据版本来源；发布与回退仅影响新任务。

逐帧视频预标在提交时固定任务、来源与采样配置，只接受单个检测阶段；同一待执行请求重放保留原清单。范围与恢复规则见[项目接口](docs-site/api/guides/projects.md)和[媒体输入边界](docs-site/dev/concepts/accepted-media-inputs.md)。

启用独立训练执行器后，从侧栏「训练队列」查看训练仓库、可管理项目与全局实验队列。项目内按训练实验、训练数据、模型与发布、自动训练分标签操作；实验详情提供阶段进度、验证与损失曲线、各次续训记录和校验后的产物下载。项目内可比较 2–4 个实验的具体执行、冻结输入与最终评估，只有证据完整且口径兼容时才计算差值或叠加曲线。项目内可查看完整原生参数目录、默认值与支持边界。扩展配置固定参数定义和有效值，并核验执行器的实际配置。训练固定数据与环境，单张 GPU 串行执行；失联期间保留资源租约，防止重复训练。

OpenAPI 明确描述后台作业取消、独立训练与 CPU 增强预览的控制响应；取消请求和已取消终态、空领取与分配、缺失字段和 null 分别保留其含义。客户端仍须查询实际作业与执行状态。

训练器目录支持已验证的 Ultralytics 与 LibreYOLO 图片检测，以及受控 `yolo11n-seg` 图片实例分割。分割冻结简单多边形顶点和实例来源，支持原生训练、完整检查点续训、最佳权重下载及分开的框／掩码报告；分割模型回流和自动策略仍需独立资格验证。LibreYOLO 提供原生权重、指标和完整续训检查点下载；已通过原生 CPU 资格检查的 YOLO9 t/s/m/c 可注册、试用、人工发布与回滚；训练器能力在页面和目录 API 中按基础权重明确展示。

项目训练配方保存完整参数、原始基础权重与不可变修订。新建实验可先核对生效参数差异再采用配方，或从完整配置实验克隆新草稿；数据版本仍需明确选择。自动策略固定具体配方修订，新修订不会自动替换已采用配置。

Ultralytics YOLO11 新实验可从同项目保留候选初始化权重，要求规格与原生类别顺序完全一致。预检和执行核对固定权重身份，从第 0 轮和全新训练状态开始；配方与克隆保留所选来源，本实验的断点续训使用自己的检查点。

Ultralytics YOLO11 的配置页可生成原生增强预览，对照冻结原图、变换后的完整矩形框和实际来源；预览使用独立且有界的 CPU 槽，种子／轮次不改变训练草稿。结果保留一天，LibreYOLO 暂不开放变换预览，仍可配置与训练。

训练监控按执行记录展示资源采样、阶段耗时、带观测依据的剩余时间范围和脱敏实时日志，明确区分整卡与训练进程数据。失败说明与检查点续训资格由实际回执和后端校验决定；采样中断不会改变训练状态或释放 GPU。

项目参数搜索支持最多 20 个显式网格组合，开始前预览全部配置并填写试验数、计划总轮数和原生计算时间预算。试验复用普通训练队列串行执行，截止时间由执行器独立停止；只对同口径、已校验的最终验证报告排名，时间证据缺失时暂停后续试验与续训。

项目模型支持逐样本评估：明确选择已安装候选及最多 32 个冻结验证成员，固定置信度、匹配 IoU 与推理环境，查看真值／预测叠加、TP／FP／FN 和误差诊断。同口径的两个评估可比较差值；历史证据保持不变，打开当前标注任务前会核对图片与标签变化。该报告使用固定阈值匹配，不等同于训练报告的 AP/mAP。

「主动学习人工选样」固定模型、未标注候选池和显式预算，保留检测启发式建议与等量随机对照。负责人选择子集后创建普通人工批次，继续分派、标注和审核；反馈只记录实际审核与部分会话时间，缺失的纠正次数和完整工时保持未知，不自动发布或宣称效率提升。

Audit 提供独立分区维护、归档和日聚合刷新命令，使用固定数据库能力及归档桶专用凭据。公开审计查询和归档管理接口保留原有权限与响应；配置和采用范围见 [Audit 运行时](./docs-site/api/guides/audit-runtime.md)。

## 工作台 AI 案例

图片标注工作台支持对已确认标注做[二次推理](./docs-site/user-guide/ai/current-task-inference.md)，由标注员、项目管理员或超级管理员使用。审核工作台隐藏相关入口，接口也拒绝审核员调用；人工审核修改保留。

<table>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>智能点 · Smart Point</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/smart-point.mp4">
        <img src="./docs-site/public/media/sam/smart-point-poster.webp" alt="智能点单击生成对象轮廓" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>智能框 · Smart Box</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/smart-box.mp4">
        <img src="./docs-site/public/media/sam/smart-box-poster.webp" alt="智能框框选生成多边形候选" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>Magic Box</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/ai/assisted-annotation.mp4">
        <img src="./docs-site/public/media/ai/assisted-annotation-poster.webp" alt="Magic Box 粗框自动收紧目标框" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>Exemplar</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/exemplar.mp4">
        <img src="./docs-site/public/media/sam/exemplar-poster.webp" alt="Exemplar 示例驱动相似目标发现" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
  </tr>
</table>

## 控制台能力

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>Project & Labeling Ops</strong><br>
      项目、数据集、批次、任务分配、审核回退、个人通知与后台任务历史。
    </td>
    <td width="33%" valign="top">
      <strong>Image & OCR Workbench</strong><br>
      bbox、rotated bbox、polygon、polyline、keypoint、mask 与 OCR 标注链路。
    </td>
    <td width="33%" valign="top">
      <strong>Video Workbench</strong><br>
      <code>video_bbox</code>、<code>video_track</code>、关键帧、插值、outside 段、帧缓存与 chunk 服务。
    </td>
  </tr>
  <tr>
    <td width="33%" valign="top">
      <strong>3D & Multimodal Workbench</strong><br>
      点云 3D 框、点级分割、scene 时序、LiDAR 坐标归一化与相机投影联动。
    </td>
    <td width="33%" valign="top">
      <strong>AI & Model Ops</strong><br>
      Grounded-SAM-2、SAM 3、YOLO、ONNXTools、RapidOCR，以及交互预测、批量预标和视频追踪。
    </td>
    <td width="33%" valign="top">
      <strong>Review & Data Delivery</strong><br>
      Data Manager 项目概览、任务 / 对象 / 轨迹浏览、选定任务操作与成员绩效看板，<a href="./docs-site/user-guide/review/index.md#在视频画面标记问题">视频问题标记与上下文恢复</a>（源帧范围、对象、画布和时间窗）、审核反馈，以及 COCO、YOLO、DAVIS、MOT、KITTI、Multi-camera COCO、Point Mask 等导出。
    </td>
  </tr>
</table>

管理员可用[项目邀请](./docs-site/user-guide/superadmin/user-management.md)为新老账号指定目标项目；超级管理员可在[系统设置](./docs-site/user-guide/reference/settings.md)管理常用运营参数、查看生效来源并恢复部署默认。接口与并发语义见[系统设置 API](./docs-site/api/guides/system-settings.md)。

成员管理支持按项目、用户组、角色和账号状态分页筛选，统计与导出采用相同范围；批量邀请与分组逐项报告结果，批次派发前可核对负责人变化和任务负载。

项目管理的列表与卡片支持服务端分页，默认每页 20 个，搜索、筛选和页码可通过链接恢复。分页接口为 [`GET /api/v1/projects/query`](./docs-site/api/guides/projects.md#列表与分页)，原项目数组接口继续供 SDK 与项目选择器使用。

取消数据集与项目的关联前可预览将清理的任务、标注和空批次；数据集与原始文件保留。详见[数据集导入导出指南](./docs-site/user-guide/datasets/index.md)。

素材上传完成校验后才进入数据集与项目建题；重复完成不会覆盖已接受的原始文件。删除素材或数据集会保护仍被项目和学习历史引用的内容，只在提交后由独立进程重试清理派生金字塔。原始文件保留。

普通图片批量预标（含 Scene 模式中的单图任务）在提交时固定任务与来源清单，准备原图后再执行；同一幂等请求复用原清单，后续新增任务不会进入原作业。范围与计数规则见[项目 API 指南](./docs-site/api/guides/projects.md)。

普通批量预标保存已推理结果和处理进度，写入繁忙或工作进程重启后继续保存原结果；已提交结果、覆盖清理和费用不会重复累计。权限或来源变化仍会拒绝迟到写入。

图片尺寸、缩略图和超大图金字塔使用同一媒体控制服务及独立图片执行进程；中断后可接管已受理作业。重试支持 UUID 幂等键和持久作业 ID，必需的高清切片缺失时工作台保留安全预览。接口见[图片与视频媒体](docs-site/api/guides/assets-media.md)，部署见[图片运行手册](docs-site/ops/runbooks/image-pyramid.md)。

目录的列表、详情、文件分页、创建/更新及数据集与项目的双向关联列表可显式交由独立 Assets 服务处理；默认本地模式使用相同 SQL 合同。创建和更新可附带 UUID `Idempotency-Key`，响应中断后复用该键确认原结果。协议与权限见[独立素材目录](./docs-site/api/guides/assets-catalog.md)。Scene、位姿、标定与点云素材准备复用同一工件，保留标定 CAS、同事务审计和导入回滚；见[空间素材协议](./docs-site/api/guides/assets-spatial.md)。

单文件上传初始化、发布完成、来源删除、同步 ZIP 入库、旧对象桶扫描和连接器管理均由 Assets Source 工作流处理；本地模式复用同一工作流，配置独立服务后走内部 API，远程错误不会回退到本地。公开路由和响应保持不变，连接器列表仍返回全部可见项，内部使用有界分页读取。创建、更新、删除和测试支持可选 UUID `Idempotency-Key`，成功响应会回传该键；见[独立来源控制](./docs-site/api/guides/assets-source.md)与[存储连接器 API](./docs-site/api/guides/storage-connections.md)。连接器导入使用受理时保存的配置和加密凭据快照，删除连接器不会删除已受理作业的快照；执行进程保存枚举结果和逐文件进度，支持崩溃恢复及取消围栏。

本地图片与 nuScenes 导入脚本通过独立的可信 Assets operator SQL 能力登记 Dataset 和 DatasetItem；nuScenes 路径还在同一调用方事务中组合标定、场景、工作区和任务写入。operator 凭据是执行授权，既有 owner UUID 只用于归属标记。该能力不是 Source HTTP runtime，也不改变导入脚本的对象存储回滚边界。见[同库数据接口](./docs-site/api/guides/module-data.md#可信导入器数据集与素材登记)和 [ADR-0104](./docs/adr/0104-operator-source-registration.md)。

视频媒体切片的语言无关合同、控制进程和执行入口位于 `apps/assets-media`，覆盖元信息、时间表、海报、播放副本和帧／分块的作业围栏，并已通过隔离本地进程与容器联调。部署入口见 [DEV.md](./DEV.md#视频媒体的独立控制与执行)，当前全模块状态及剩余边界见[独立部署台账](./docs-site/dev/concepts/module-deployment.md)。Go 继续作为单独实验。

隔离测试的 E2E 项目、成员及模板通过 Workspace 固定能力写入，清理保留调用方事务并保护未退役视频；见[测试夹具协议](./docs-site/api/guides/workspace-runtime.md#门控-e2e-夹具)。这不代表日常环境已经切换。

规模种子的项目、成员和计数写入也使用对应 owner；清理会保护视频执行、学习历史及辅助来源引用。截图支持读取保留原会话缓存，见[规模种子边界](./docs-site/api/guides/workspace-runtime.md#规模种子与截图支持读取)。

视频推理输入的清单、精确帧、证据与回收已通过隔离数据库、HTTP、独立执行进程及真实对象存储联调；逐帧预标、覆盖、失败重试与候选采纳已接入固定输入，并完成受限容器的 360 帧负载、Celery 优雅交接与 Media 租约接管验证。完整模块部署继续推进，见[受理视频输入协议](./docs-site/api/guides/module-data.md#受理视频输入与帧计划)。

专用视频跟踪、范围传播和 Mask 修正使用各自作业的固定原片输入，逐窗口复用同一保留版本；采样设置变化不会改写已接受请求，候选采纳与派生标注保留原片证据。控制和执行复用现有 Media 工件，见[跟踪原片接口](./docs-site/api/guides/assets-media.md#视频跟踪的原片输入)。

视频输入回收同时核对未退役执行和历史垃圾前缀的当前引用，无法证明安全时保留。升级前须停止并确认清理进程退出、盘点旧租约；历史过期状态及已删除对象不自动恢复，见 [DEV.md](./DEV.md) 与[输入保留台账](./docs-site/dev/concepts/module-deployment.md#tracker-input-retention)。

视频执行增加冻结计划与不可变窗口账本；确认旧范围退役且模型兼容身份完整后，可复用已完成窗口或重放未闭合会话。真实进程中断接管已在隔离本地环境验证。worker 成功、取消和失败的最终写入已有独立 Labeling owner 源码切片，仍在本地验收；人工审核及中间 start/progress 仍由 API 持有，见[视频窗口协议](./docs-site/api/guides/module-data.md#视频执行窗口账本)、[Labeling 写入协议](./docs-site/api/guides/labeling-runtime.md#视频-worker-的最终写入)和 [ADR-0159](./docs/adr/0159-independent-labeling-video-writer.md)。

Mask 内容读写提供可选独立工件 `apps/assets-mask-content`，沿用现有引用和业务权限；回收统一使用持久删除围栏与不可变回执，可本地装配或调用独立协调服务。部署与配置见 [DEV.md](./DEV.md#mask-内容的独立字节服务)，协议见 [Mask 字节接口](./docs-site/api/guides/mask-content.md)。

Mask 独立协调工件为 `apps/labeling-mask-gc`；预览返回续扫位置，扫描页数与删除数分别限额，未知删除结果继续保留围栏。素材仍被相机链接、场景轨迹、视频执行或保留输入引用时，删除返回 409，公开 OpenAPI 已列出该冲突响应。须先在对应功能中解除引用；取消执行仍保留历史引用时会继续拒绝删除，见[素材删除规则](./docs-site/user-guide/datasets/index.md#删除素材或数据集)。

普通批量与失败项重试保存推理结果和精确写入回执；发布不确定、写锁竞争或 worker 中断后继续原作业与已保存输出。重试、派发和结果读取使用独立 SQL 合同，部署边界见[结果检查点](./docs-site/dev/concepts/module-deployment.md#普通批量结果检查点)。

工作台单张图片预标使用同一媒体工件提供的版本化输入副本，整图模型和 ROI 读取同一版本。独立运行需配置专用的 `MINIO_INPUTS_BUCKET` 并启用版本管理；启动和权限要求见 [DEV.md](./DEV.md#图片金字塔的独立控制与执行)，其余消费路径见[媒体输入边界](./docs-site/dev/concepts/accepted-media-inputs.md)。

项目成员可在工作台阅读[标注指引](./docs-site/user-guide/projects/index.md#编写标注指引)及示例图；指引编辑和图片上传、删除由项目负责人或超级管理员管理。
图片完成登记前重新核对当前管理权限；项目转移或账号停用后的旧请求不能继续登记，写入争用返回冲突。读取与存储失败语义见[项目 API](./docs-site/api/guides/projects.md#标注指引图片)。

平台把**账号身份（平台角色）**与**项目职责（项目角色）**分开：同一个员工可以在 A 项目标注、B 项目质检、C 项目只读，无须切换账号。项目访问与写入由项目成员关系和固定能力集决定，全项目导出收紧到质检员 / 负责人、自审按持久贡献者证据拒绝。设计见 [ADR-0075](./docs/adr/0075-platform-identity-vs-project-authority.md)，迁移、回滚与已签发 URL 限制见[员工项目角色迁移与回滚 runbook](./docs-site/ops/runbooks/project-role-migration.md)。

评论、问题与回复在提交前重新确认当前权限和真实任务／线程范围，资源争用返回冲突；通知同事务保存、提交后尽力推送。接口与原评论／反馈镜像的独立性见[任务讨论与问题线程](./docs-site/api/guides/tasks-and-annotations.md#任务讨论与问题线程)。

视频章节按当前可见任务的真实项目权限管理，修改前刷新来源绑定与章节记录；写入争用返回冲突。角色与元数据语义见[视频章节 API](./docs-site/api/guides/tasks-and-annotations.md#视频章节)。

## 架构

<table>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>数据航线</strong><br>
      <a href="./docs/assets/readme/data-route.excalidraw">
        <img src="./docs/assets/readme/data-route.svg" alt="数据航线 Excalidraw 图" width="100%">
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>系统架构</strong><br>
      <a href="./docs/assets/readme/system-architecture.excalidraw">
        <img src="./docs/assets/readme/system-architecture.svg" alt="系统架构 Excalidraw 图" width="100%">
      </a>
    </td>
  </tr>
</table>

## 快速开始

### 前置依赖

| 依赖           | 版本                       |
| -------------- | -------------------------- |
| Node.js        | `>= 22`                    |
| pnpm           | `>= 10`                    |
| Python         | `>= 3.11`                  |
| uv             | Python 包管理              |
| Docker Compose | 本地数据库、队列、对象存储 |
| pre-commit     | 可选，但推荐               |

### 本地启动

```bash
# 1. 安装依赖
pnpm install
cd apps/api && uv sync --extra test && cd ../..
pnpm codegen

# 可选：已安装 pre-commit 时启用本地 hooks
pre-commit install

# 2. 启动基础设施
docker compose up -d postgres redis minio mailpit

# 3. 初始化数据库
cd apps/api && uv run alembic upgrade head && cd ../..

# 4. 启动后端和前端
pnpm dev:api        # http://localhost:8000
pnpm dev:web        # http://localhost:3000

# 多 worktree 并行时可用一条命令启动当前 checkout 的前后端
pnpm dev:worktree   # 独立数据库/Redis/bucket，自动选择端口并配置代理
```

`dev:worktree` 为每个工作树的 `dev/test/e2e` 模式管理独立数据库、Redis、八个 bucket
及本地文件；共享本机 PostgreSQL/MinIO 实例，不修改共享数据库。默认从 API `8100`
和 Web `3100` 寻找空闲端口，可用 `--with-worker` 启动本工作树的后台任务消费者。
`doctor` 诊断、`stop` 停止、带精确确认值的 `reset/destroy` 重建或清理环境。
首次启动不复制已有账号和项目。详见[独立工作树开发环境](docs-site/dev/how-to/worktree-environments.md)。

Go 审计月报仅保留为[隔离实验](apps/audit-summary/README.md)，不参与默认开发、生产部署或普通测试；正常月报使用本地 Python 计算。

需要演示数据时：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed.py
```

### 常用入口

| 服务          | 地址                       | 说明                            |
| ------------- | -------------------------- | ------------------------------- |
| Web App       | http://localhost:3000      | 标注平台主界面                  |
| Swagger UI    | http://localhost:8000/docs | 运行时 API 文档                 |
| Docs Site     | http://localhost:5173      | `pnpm docs:dev` 后打开          |
| MinIO Console | http://localhost:9001      | `minioadmin / minioadmin`       |
| Mailpit       | http://localhost:8025      | 开发邮件收件箱                  |
| Grafana       | http://localhost:3001      | `monitoring` profile 启动后打开 |

### 局域网生产部署

已有开发环境时，可使用独立 `docker-compose.lan-prod.yml` 复用 PostgreSQL、MinIO 和模型服务，生产账号、项目、数据集、队列和存储桶单独管理。前端提供局域网 HTTPS 3030，API 提供 HTTPS 8080；客户端首次导入内部 CA 公共证书。配置、初始化与回退步骤见[局域网生产部署](./docs-site/ops/deploy/lan-production.md)。

## 可选服务

```bash
# Celery 后台任务：通用队列、GPU / CPU 预标、导出、视频帧、通知等
docker compose up -d celery-worker celery-worker-maintenance celery-worker-gpu celery-worker-cpu celery-worker-export celery-beat

# GPU ML Backend 在叠加文件 docker-compose.ml.yml，按显存预算选择独立 profile
# Grounded-SAM-2：适合图片 SAM / DINO 与视频 tracker
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend

# SAM 3：独立 GPU profile，需要 HF_TOKEN 与更高显存
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-sam3 up -d sam3-backend

# 其它可选后端：YOLO、ONNXTools、RapidOCR
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-yolo up -d yolo-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-onnxtools up -d onnxtools-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-rapidocr up -d rapidocr-backend

# Prometheus + Grafana
docker compose --profile monitoring up -d prometheus grafana
```

修改 `apps/api/**` 下的 worker 业务代码后，Celery 不会热重载，重启 worker 即可；修改依赖、Dockerfile 或 compose build 配置才需要 rebuild。开发 worker 不自动迁移数据库；更新包含迁移时，应先按上面的初始化命令显式迁移，再启动 worker。

视频元数据、完整帧时间表和分块样本探测可独立部署为 `apps/media-probe`，使用专用
凭据及文件流协议，不需要业务数据库或对象存储访问权。通过 `MEDIA_PROBE_URL`
显式选择；启动与消费者配置见 [DEV.md](./DEV.md#独立素材探测服务)，
接口见[独立素材探测协议](./docs-site/api/guides/media-probe.md)。

数据集的追加与初始分块建题通过版本化 PostgreSQL 数据接口共享外层事务，保留任务、
计数、进度和回执的共同回滚；初始操作固定接受时的授权与关联代次，支持重启恢复和显式取消。
项目删除也通过各所有者的原生命令组合，与审计及删除回执共同提交，并保留学习引用与作业访问范围。
独立受限账号的配置与本地验收见 [DEV.md](./DEV.md#同库事务的数据接口)。
[所有模块的部署与语言替换状态](./docs-site/dev/concepts/module-deployment.md)
仍按完整运行工件、协议、权限与事务逐项验收。

Notifications 可通过独立 WebSocket 服务持有认证、逐帧授权与 Redis 订阅，API 保留
原 `/ws/notifications` 地址。可选部署和最小凭据配置见
[DEV.md](./DEV.md#独立-notifications-websocket)；当前独立运行资格按模块台账记录。

## 开发工作流

```bash
# 全仓格式与静态检查
pnpm format:check
pnpm lint
pnpm typecheck

# 自动修复格式（提交前执行）
pnpm format

# 前端测试
pnpm test
pnpm test:e2e          # 需要 Postgres/Redis/MinIO；自启 3001/8010 与 annotation_e2e
pnpm --filter @anno/web test:e2e:visual  # 视觉基线（独立于默认功能集）
pnpm --filter @anno/web test:e2e:stress  # 完整布局压力矩阵

# 后端
cd apps/api && uv run pytest

# API 契约
pnpm openapi:export    # API 变更后刷新 snapshot
pnpm openapi:check     # CI 同款契约校验

# 文档站
pnpm docs:dev
pnpm docs:build
```

E2E 不复用开发环境的 `3000/8000` 或 `annotation` 数据库。测试路由
默认关闭，仅对显式开启且名称以 `_e2e` / `_test` 结尾的数据库开放。
详见 [E2E 运行说明](./apps/web/e2e/README.md)。

API 变更后同步跑 `pnpm openapi:export` 和 `pnpm codegen`；环境变量变更后同步更新 `.env.example` 并跑 `pnpm docs:gen-env-vars`。

Data Manager 将未解决问题与普通评论分别计数，兼容旧反馈筛选；成员绩效统计已保存、仍保留的图片与标注记录，不要求先提交任务。字段、统计口径与权限见[项目 API 指南](./docs-site/api/guides/projects.md)。

传统任务文件上传、完成与缩略图回填要求当前项目管理权；原图地址遵循完整任务可见性，保留普通成员读取自己可见任务的能力。上传完成会复查当前权限、任务状态与来源绑定，繁忙或过期目标不会被强制改回待标注，详见[任务文件 API](./docs-site/api/guides/tasks-and-annotations.md#历史任务文件入口)。

视频轨迹 API 保留旧关键帧缺失的来源字段，客户端应按「来源未知」处理；新建标注仍使用现有来源默认值，详见[任务与标注 API](./docs-site/api/guides/tasks-and-annotations.md)。

视频作业取消先保存状态与审计，再重投同一作业完成后台退役。若队列投递无法确认，接口返回可重试的 503；重复相同取消请求不会重建作业或重复审计。取消成功仍需等待退役后才能删除项目或解绑数据集，详见[视频作业 API](./docs-site/api/guides/video-tracker-jobs.md)。

内部账号支持邮件恢复、按状态查询、逐项目职责交接、紧急停用与恢复。管理员确认交接时会重新核对接收资格及预览状态；恢复不会拿回已交接工作或恢复已撤销的凭据。接口与兼容字段见[认证与账号生命周期](./docs-site/api/guides/auth.md)。

工作台显示保存和本机待同步状态，并按账号保留离线操作；普通标注创建支持持久幂等键，避免重试生成重复对象。详见[任务与标注 API](./docs-site/api/guides/tasks-and-annotations.md#保存标注与提交任务)。

任务讨论 API 汇总原标注评论和原生任务留言，任务留言支持提及项目成员、项目负责人与启用的平台超级管理员（候选见 `GET /api/v1/projects/:id/mention-candidates`），图片任务留言还可附绘图；提供按标注 ID 批量统计评论数，图片和视频画布提示可通过默认开启的账号偏好关闭。聚合记录保留来源、附件与视频锚点；问题 API 提供根记录筛选、准确状态数量和历史回复分页，图片和视频像素问题均可关联对象，读写都检查项目与任务权限。详见[任务讨论与问题线程](./docs-site/api/guides/tasks-and-annotations.md#任务讨论与问题线程)。

问题回复、状态变化、任务留言和标注评论提及复用站内通知与按类型偏好，点击时重新核对权限并定位原对话。`GET/PUT /api/v1/notification-preferences` 按类型返回并更新「接收通知（in_app）」与「弹出提示（toast）」；PUT 至少提供其一，只原子合并请求出现的键，`toast` 读取为显式选择或类型默认计算出的有效值。讨论通知在业务提交成功后推送，网络推送失败不影响已保存内容，详见[讨论通知](./docs-site/api/guides/tasks-and-annotations.md#讨论通知)。

通知偏好的读取、原子更新和静音判断，以及普通通知的同事务追加，由 `apps/notifications` 的语言无关合同与固定 owner 函数持有。普通即时推送可选择现有独立 publisher 的私网 HTTP 入口，配置与提交语义见 [Notifications 即时发布](./docs-site/api/guides/module-data.md#notifications-即时发布)。原调用方继续控制提交、回滚及提交后推送；收件箱读取和完整投递仍按模块台账推进。切换受限凭据前须更新并排空旧 API／worker，详见 [Notifications 能力](./apps/notifications/README.md)和[开发运行说明](./DEV.md#notifications-偏好事务内能力)。

业务审计、HTTP 元数据及其 Celery 持久化现通过 `apps/audit` 的版本化追加能力写入，保留原事务、actor 快照及匿名重试。查询、归档及完整凭据边界仍在推进；现有 LAN 的分区所有权还不能直接采用严格追加配置，见 [Audit 能力](./apps/audit/README.md)与[模块台账](./docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

命名布局预设通过账号偏好 API 跨设备保存；`namedPresets` 采用带 revision 的整份 map 替换与省略删除语义，陈旧设备写入返回冲突，而 `contexts` 可在预设专用 PATCH 中缺席。安全更新方式与兼容规则见[账号偏好与命名布局预设](./docs-site/api/guides/auth.md#账号偏好与命名布局预设)。

资料、偏好、心跳、自助改密和头像引用可由同一独立 Identity 工件处理；偏好合并、当前凭据校验、状态和业务审计在所有者事务内提交。头像字节继续通过现有存储入口处理，在线扫描使用独立权限。协议见[自助账户命令](./docs-site/api/guides/identity-runtime.md#自助账户命令)，完整 Identity 与其他模块继续按[模块台账](./docs-site/dev/concepts/module-deployment.md)推进。

工作台之外的全局界面偏好（主题、二次推理工具条显隐、登录后「本次更新」提醒的已确认版本）保存在账号偏好的 `ui` 子树并跨设备同步；`ui.changelog_seen_version` 为空表示从未确认，客户端仅在当前构建版本更高时提醒一次。契约见[账号偏好与命名布局预设](./docs-site/api/guides/auth.md#账号偏好与命名布局预设)。

图片 Polygon 和 Raster Mask 支持预览后原子切割与保留对象 ID 的撤销/重做。提交和恢复使用独立幂等请求，恢复受完整版本集与原切割后 30 天期限约束，Mask 支持保留全部像素的直线切割，撤销后的内容引用受资源清理保护。详见[Mask 切割](./docs-site/user-guide/workbench/mask-brush.md#直线切割为两个实例)、[Polygon 切割](./docs-site/user-guide/workbench/polygon.md#切割为两个对象)与[API 合同](./docs-site/api/guides/tasks-and-annotations.md#polygon-原子切割与恢复)。

SAM3 示例召回支持成对返回矩形框与原生 Mask，图片和视频当前帧均可分别采纳；客户端须保留 Mask 在完整候选数组中的索引。详见[交互式示例候选合同](./docs-site/api/guides/ml-backend.md#交互式示例候选)。

## 文档地图

Python SDK 与 `aap learning` 提供冻结训练数据、实验观测、配方与预览、逐样本评估、受控模型发布、学习策略、参数搜索和人工主动学习入口。请求保留预检摘要、幂等 ID 与版本冲突保护；完整流程见 [SDK Cookbook](./docs-site/dev/sdk/cookbook.md)，终端只读训练监控见 [TUI 参考](./docs-site/dev/sdk/tui.md)。

Agent 可使用随包离线 `capabilities` / `describe` 读取操作、请求 schema 与约束，通过可选 JSON 错误和 stdin 请求执行脚本；详见 [Agent 接入与操作](./docs-site/dev/sdk/agents.md)。

SDK 与 CLI 还提供当前账号通知、反馈线程处理、编排库复制及带幂等身份的预标注提交；读取进度与执行变更分开，详见 [通知、反馈与预标注](./docs-site/dev/sdk/workflows.md)。

Data Explorer 脚本入口支持项目 schema、任务／对象／轨迹查询、保存视图、分配预览与选中任务导出，详见 [Data Explorer 与保存视图](./docs-site/dev/sdk/data-explorer.md)。

SDK 和 CLI 支持视频媒体／分段租约、追踪候选与质量审阅、Mask 修正、轨迹编辑、项目模板及存储资产重试，见 [视频、模板与存储运维](./docs-site/dev/sdk/video-and-templates.md)。模板／源项目创建在服务器继承未指定类型与配置，明确覆盖优先；公开 REST 覆盖路线包含运维，仅执行器私有协议排除。

任务文件采用初始化、签名上传和明确完成的分步流程，签名与排队前校验项目／任务权限；数据集场景提供读取、轨迹与管理员更新，见 [文件上传与数据集场景](./docs-site/dev/sdk/files-and-scenes.md)。

账户／会话、用户／分组／邀请、根健康、系统设置、审计、连接器及 ML 运维可通过 SDK 和 CLI 显式操作，见 [账户、管理与运维](./docs-site/dev/sdk/account-and-operations.md)。项目管理员的审计、预标队列与分组操作由服务器按项目归属限制；批量部分失败和健康降级保留机器可读回执及非零退出码。

Identity 准入、API key、核心会话、自助账户、注册恢复／邮件、分组目录／指派、自助注销，以及完整邀请生命周期与 MailV2 已有本地 owner 资格。用户目录的列表、分页、统计和导出已接入同一 Identity 工件，并通过独立进程、受限数据权限、真实 API、重启和备份恢复的本地验收，保留原可见范围、公开响应与导出审计。管理员重置密码也已通过受限连接、真实 API、重启重放、备份恢复与镜像的本地验收，保留临时密码响应和现有 JWT／API key 行为。平台角色预览和变更也已通过受限权限、真实 API、重放、恢复与镜像的本地验收，保留项目职责阻断和最后一名未预留超级管理员保护；管理员停用和恢复也已通过受限权限、独立进程、真实 API、重启重放及备份恢复的本地验收，保留项目范围、自助注销预约与凭据撤销规则；管理员删除账号也已通过本地受限权限、独立进程、真实 API、转交／重放及备份恢复验收，保留既有任务范围和凭据行为；离职交接预览与提交也已通过窄权限、独立进程、真实 API、原子转交与应急停用、重启重放和备份恢复的本地验收，保持公开接口、预览摘要与已受理操作的回执语义；任务、批次、项目成员、负责人显示和历史记录使用的六字段用户摘要也已通过受限 SQL、独立进程、普通成员权限、真实 API、备份恢复和镜像的本地验收，保留停用用户的历史展示、项目角色回退及调用方事务；共享认证依赖的账号存在、启用状态和角色复查也已移到同一 AUTH owner，并通过只读主体上下文、事务保留、双向语言 peer 与真实 API 的限定本地验收；其余用户查询和剩余模块继续推进，完整 Identity、另一语言提供方与生产容量尚未完成。新验证器的受控切换要求旧 JWT 重新登录，API key 状态保留；日常环境尚未切换，见[认证与权限](./docs-site/api/guides/auth.md)、[Identity 私有运行协议](./docs-site/api/guides/identity-runtime.md)、[目录 ADR](./docs/adr/0123-independent-identity-directory-reads.md)、[管理员改密 ADR](./docs/adr/0124-independent-identity-admin-password-reset.md)、[平台角色 ADR](./docs/adr/0125-independent-identity-platform-role-commands.md)、[管理员停用／恢复 ADR](./docs/adr/0126-independent-identity-suspension-commands.md)、[管理员删除 ADR](./docs/adr/0127-independent-identity-deletion-commands.md)、[离职交接 ADR](./docs/adr/0128-independent-identity-offboarding-commands.md)、[用户摘要 ADR](./docs/adr/0129-identity-batched-user-brief-reads.md)、[主体上下文 ADR](./docs/adr/0130-identity-post-admission-actor-context.md)及 [DEV.md](./DEV.md#identity-独立认证准入)。

执行期的单账号读取与已接受 API-key 最终授权已接入 Identity 的同事务能力，保持当前权限、账号／项目／key 锁序、取消及原错误行为；定向本地检查通过，范围见 [ADR-0161](./docs/adr/0161-identity-current-authority-consumers.md)。集合查询、展示投影、完整模块凭据及独立运行验收继续按唯一台账收口，当前不切换日常或生产环境。

项目成员新增、职责预览／变更及移除支持可选的独立 Workspace owner，保留公开接口、职责交接、冲突和审计语义，并已完成隔离本地验收。负责人转移也支持独立的受限命令绑定，默认路径保留调用方提交边界。项目与模板查询已提供独立读取绑定；成员列表与提及候选也已接入目录 owner，其余 Workspace 入口继续迁移，见 [Workspace 运行指南](./docs-site/api/guides/workspace-runtime.md)。

项目／模板配置模型与纯策略已移到 Workspace 包，预标注阶段结构与规范化由共享 Inference 契约包持有，Workspace 无需安装 Inference 提供方。六项公开配置调用已接入配置 owner，默认保留调用方事务；独立安装包、进程、受限镜像与本地并发执行已完成限定验收，见 [配置命令 ADR](./docs/adr/0136-workspace-configuration-command-owner.md)。

开发点云种子的 Project 读写也已接入固定 Workspace 能力，保留目录／nuScenes 的不同默认值、提交时点和提交后对象清理。运维授权与定向复验见 [开发点云种子](./DEV.md#workspace-开发点云种子)；其他开发脚本和完整模块资格继续按部署台账推进。

主种子脚本自身的账号创建和演示成员写入也使用固定 Identity／Workspace 能力，保留已有账号和原事务行为。受限凭据复验见 [主种子账号与演示成员](./DEV.md#主种子账号与演示成员)；主脚本自身已通过冷中断／恢复验收；媒体子入口和完整模块资格仍按同一台账跟踪。

媒体子种子及截图目录的账号操作也已迁移到 Identity 能力，保留各自的默认值与校验规则。范围和剩余运维入口见[模块台账](./docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

媒体及截图种子的 Project／成员读写也已接入固定 Workspace 能力，保留 COCO8 补工具、截图固定时间及原事务行为；定向及匹配冷工件的真实媒体／截图操作验收均通过，完整模块资格继续按同一台账推进。

图片导入及规模种子的账号选择已接入 Identity 事实／固定只读能力，保留任意首账号和创建时间排序的不同规则。实现、限定验收及其他运维入口见同一模块台账。

历史角色审计使用独立的 Identity 审计关系，兼容较早数据库；需要在只读命令之外显式安装受限关系。部署前置条件与实际资格见[模块台账](./docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

截图作业恢复／拒绝夹具从 Identity 公开事实读取账号 ID；偏好完整恢复使用仅限测试库的固定 Identity 写入能力。受限凭据与授权步骤见 `DEV.md`，整体状态仍以模块台账为准。

门控 E2E 路由的账号、邀请及组织成员清理也通过 Identity 测试能力执行。可信测试运维凭据先应用普通 Identity 围栏，再显式追加夹具能力；普通 API 权限保持，安装步骤见 `DEV.md`。 Jobs 测试清理通过[固定项目范围能力](./docs-site/api/guides/module-data.md#jobs-测试项目清理)保留视频退役检查；普通运行账号的 Jobs 私表 INSERT／UPDATE／DELETE 已撤销，筛选夹具通过固定的测试初始化能力保持原时间戳和事务语义。 项目预标和所选任务导出的[幂等锁读](./docs-site/api/guides/module-data.md#jobs-动作幂等查找)也由 Jobs owner 执行，保留原复用、冲突和事务行为。

类别重命名已接入同一个配置 owner，在单一事务内组合项目绑定和 Labeling 类名写入；同名请求由独立提供方持锁完成投影。三路径、独立安装包、受限镜像、重启、恢复与完整迁移已通过限定本地验收，见 [类别改名 ADR](./docs/adr/0143-workspace-project-class-rename-owner.md)。

指南对象的受限签名、HEAD 与普通删除能力已加入既有 Assets Source 运行时，支持无需数据库凭据的 `guide-only` 模式。公共指南工作流由 Workspace 编排，借用事务、专用 DSN 与独立 HTTP 三路径已通过冻结回放、实际权限锁及备份恢复验收；受限容器重启后仍可读取已登记素材。范围见 [指南素材 ADR](./docs/adr/0144-workspace-guide-assets-owner.md)，完整模块与语言替换资格继续推进。

模板列表、详情和写入后的响应投影也已接入同一个读取 owner，默认借用调用方事务，可选专用 DSN／HTTP；三条路径的旧响应对照、独立安装包、受限镜像、故障恢复与完整迁移均已完成限定本地验收，见 [模板查询 ADR](./docs/adr/0137-workspace-template-query-owner.md)。

成员列表与提及候选的目录 owner 已通过三路径响应对照、独立安装包、受限镜像、故障与备份恢复的限定本地验收；现有权限、成员排序和停用账号展示规则保留。相邻命令和其他模块继续推进，见 [目录读取 ADR](./docs/adr/0140-workspace-directory-query-owner.md)。

项目访问上下文 `/access` 也已接入现有 Project Query owner，沿用同一部署配置，保留两轮当前权限读取与最终成员版本；三路径响应对照、独立安装包、受限镜像、重启、恢复及完整迁移已通过限定本地验收。完整 Workspace 与全模块独立部署仍在推进，见 [访问上下文 ADR](./docs/adr/0141-workspace-project-access-query.md)。

项目访问纯规则与行解码位于共享契约包；Workspace 新增分阶段锁定事实接口，供 Inference 在同一事务内组合原有权限围栏。它是命名流水线独立 owner 的前置能力，范围见 [项目访问事实 ADR](./docs/adr/0145-workspace-project-access-facts.md)。

普通单项目业务读取已统一经过 Workspace 快照边界，支持无 actor 的后台元数据读取，保留原缓存、刷新和调用方事务；当前权限检查仍在原入口完成。排序项目集合锁与两处 Labeling 计数投影已通过同事务 owner 能力执行。项目范围聚合、看板及作业／通知列表通过 Workspace 事实关系过滤，保留原角色和 Identity 校验。非锁定成员集合、绩效名册和批次相关判断也已使用[成员事实关系](./docs/adr/0174-workspace-nonlocking-member-facts.md)，保留各调用的规则和 ORM 行为。Project 集合、投影及 owner 子查询现也通过[原生事实关系](./docs/adr/0175-workspace-project-fact-relations.md)组合 Workspace 字段和 Labeling 计数。生命周期／审核的三处原子计数增减也已由 [Labeling 数据能力](./docs/adr/0176-labeling-lifecycle-counter-deltas.md)执行，保留并发累加、缓存与 Task／Audit 共同事务。数据集解绑也已接入 Workspace 的[可移植事务绑定](./docs/adr/0177-workspace-caller-transaction-dataset-unlink.md)，保留原响应、幂等重放和共同回滚。项目删除和解绑现会在视频执行未退役时返回 409，先取消并完成退役后重试；旧作业缺证明时继续保留。普通调用账号现可显式应用 [Workspace 凭据边界](./docs/adr/0179-workspace-consumer-credential-closure.md)，只保留固定函数与事实关系；完整凭据及模块装配继续按[唯一台账](./docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。

命名流水线的列表、详情、创建、更新、删除和应用已接入 Inference owner，支持借用调用方事务、专用数据库连接及独立 HTTP 进程。公开接口保留原样；嵌入项目的预标注配置继续由 Workspace 持有。三路径、独立工件、受限容器、并发与恢复已通过限定本地验收，配置见 [Inference 运行指南](./docs-site/api/guides/inference-runtime.md)。

Workspace 的后端能力探测通过 Inference 私有协议返回模态，Workspace 只持有事务绑定的不透明快照；API 的借用连接、专用 DSN 和 HTTP 以及独立 Workspace 均使用同一能力。配置与边界见 [Inference 运行指南](./docs-site/api/guides/inference-runtime.md#workspace-的后端能力探测)；完整模块与语言替换仍按部署台账推进。

健康对账与服务池成员写入采用一致锁顺序；删除与成员移除会拒绝安全检查后的拓扑变化，项目绑定争用会完整回滚。接口冲突及重试见 [ML Backend API](./docs-site/api/guides/ml-backend.md#修改--删除)。

健康检查的外部观察支持独立 Inference 进程，密封快照与回执绑定原事务；能力提取、GPU 围栏与写回仍在原调用方事务完成。健康数据读写和能力对账使用固定 Native 函数，独立数据包可借用调用方事务。配置见 [Inference 运行指南](./docs-site/api/guides/inference-runtime.md#健康观察与调用方写回)，完整健康控制面继续迁移。

启动时的环境后端同步与截图 seed 共用 Registry ingress 策略及固定写入能力。整个环境同步命令可选择借用连接、专用 DSN 或独立 HTTP owner；seed 保留原调用方事务。配置与未完成范围见 [环境同步指南](./docs-site/api/guides/inference-runtime.md#registry-环境同步)。

推理路由正在接入独立的持久执行账本；运行计数不再依赖 Redis 租约，卸载与重载要求管理占用和真实后端连续性。配置与本地验证边界见[路由执行权指南](./docs-site/api/guides/inference-runtime.md#持久路由执行权)。GPU 自动驱逐、作业重启交接与完整模块资格仍在推进。

作业交接的定向退役协议已提供并通过限定本地验收：先关闭单个 scope 的准入，再确认该范围的实际后端工作结束，其他作业继续运行。视频 runner 已接入执行代次、原子路由绑定和当前业务写入检查，重派发复用后台汇总；持久窗口检查点与协调恢复已完成限定本地验收，见[协调恢复 ADR](./docs/adr/0160-coordinated-video-restore.md)；完整 Inference 装配仍按模块台账推进。

类别与属性用量已接入 Labeling 读取边界，先核验当前项目管理权，保留原统计口径和两次顺序读取；独立安装包、三路径响应、语句间提交、受限镜像与备份恢复已完成限定本地验收，见 [Labeling 运行协议](./docs-site/api/guides/labeling-runtime.md)与 [统计 owner ADR](./docs/adr/0142-labeling-class-usage-query-owner.md)。任务／批次控制、人工与批量标注、预测记录与采纳、轨迹命令／撤销、跨帧和视频框写入已接入同事务命令边界，完整模块验收仍按[唯一台账](./docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。

项目与批次维护、数据回填、成员绩效、搜索、指引资源及预测／标注导入见 [项目、批次与数据运维](./docs-site/dev/sdk/project-workflows.md)。分派绑定原预览版本，项目范围约束先于查询限制，批次导出与项目导出使用相同能力检查。

| 角色 / 任务                | 入口                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 标注员、审核员、项目管理员 | [docs-site/user-guide/](./docs-site/user-guide/)                                                                                |
| 本地开发                   | [docs-site/dev/tutorials/local-dev.md](./docs-site/dev/tutorials/local-dev.md)                                                  |
| 测试策略                   | [docs-site/dev/testing.md](./docs-site/dev/testing.md)                                                                          |
| API 文档                   | [docs-site/api/](./docs-site/api/)                                                                                              |
| Python SDK / CLI           | [docs-site/dev/sdk/quickstart.md](./docs-site/dev/sdk/quickstart.md)                                                            |
| ML Backend 协议            | [docs-site/dev/reference/ml-backend-protocol.md](./docs-site/dev/reference/ml-backend-protocol.md)                              |
| Data Manager               | [用户指南](./docs-site/user-guide/projects/data-manager.md) · [查询与校验契约](./docs-site/dev/reference/data-manager-query.md) |
| 视频帧服务                 | [docs-site/dev/reference/video-frame-service.md](./docs-site/dev/reference/video-frame-service.md)                              |
| 导出格式                   | [docs-site/user-guide/reference/export-formats.md](./docs-site/user-guide/reference/export-formats.md)                          |
| 部署与运维                 | [docs-site/ops/](./docs-site/ops/)                                                                                              |
| 架构概念                   | [docs-site/dev/concepts/](./docs-site/dev/concepts/)                                                                            |
| ADR                        | [docs/adr/](./docs/adr/)                                                                                                        |
| 变更记录                   | [CHANGELOG.md](./CHANGELOG.md)                                                                                                  |

部署版文档站：[https://yyq19990828.github.io/ai-annotation-platform/](https://yyq19990828.github.io/ai-annotation-platform/)

## 仓库结构

```text
ai-annotation-platform/
├── apps/
│   ├── api/                       # FastAPI 后端、SQLAlchemy、Alembic、Celery workers
│   ├── media-probe/               # 独立 ffprobe 服务、流式协议与替换验收
│   ├── web/                       # React 18 + TypeScript + Vite 标注前端
│   ├── grounded-sam2-backend/     # Grounded-SAM-2 GPU ML Backend
│   ├── sam3-backend/              # SAM 3 GPU ML Backend
│   ├── yolo-backend/              # YOLO 检测 / 分割 / Exemplar ML Backend
│   ├── onnxtools-backend/         # ONNX 检测与属性推理 ML Backend
│   ├── rapidocr-backend/          # OCR 检测 / 识别 / 端到端 ML Backend
│   └── _shared/                   # Backend runtime、协议与 mask 工具共享包
├── docs-site/                     # VitePress 用户 / 开发 / API / 运维文档
├── docs/
│   ├── adr/                       # 架构决策记录
│   ├── changelogs/                # 历史版本变更
│   ├── plans/                     # 实施计划
│   └── research/                  # 调研报告
├── infra/                         # Docker、Prometheus、Grafana、Nginx 等基础设施配置
├── scripts/                       # OpenAPI、docs impact、seed、维护脚本
├── docker-compose.yml             # 基础栈（postgres/redis/minio/celery + 监控 profile）
├── docker-compose.ml.yml          # GPU ML Backend 与截图协议 stub 叠加
├── pnpm-workspace.yaml
└── package.json
```

## 技术栈

| 层   | 技术                                                                             |
| ---- | -------------------------------------------------------------------------------- |
| 前端 | React 18、TypeScript、Vite、TanStack Query、Zustand、Konva、Three.js、Playwright |
| 后端 | FastAPI、Pydantic、SQLAlchemy 2、Alembic、Celery、pytest                         |
| 数据 | PostgreSQL 16、Redis 7、MinIO / OSS、DuckDB 分析视图                             |
| AI   | Grounded-SAM-2、SAM 3、YOLO、ONNX Runtime、RapidOCR、开放 ML Backend 协议        |
| 文档 | VitePress、Mermaid、OpenAPI / Scalar、ADR                                        |
| CI   | GitHub Actions、docs impact、visual regression、OpenAPI snapshot check           |

项目开工检查和员工指南确认的 API 见[项目 API 指南](docs-site/api/guides/projects.md#开工状态)，操作说明见[快速开始](docs-site/user-guide/getting-started.md)与[项目设置](docs-site/user-guide/projects/index.md#开工准备与项目设置)。

[成员管理](docs-site/user-guide/superadmin/user-management.md)支持服务端分页筛选、同范围导出、批量邀请和分组，以及角色影响预览；[批次分派](docs-site/user-guide/projects/batch.md#分配人员)确认前展示负责人变化与任务负载。

## 贡献前检查

- 只改和当前目标直接相关的文件。
- 前端样式使用 `apps/web/src/styles/shadcn.css` 的 `--sc-*` 运行时 token 或 Tailwind 语义类，不再读取旧 `--color-*` 变量。
- API、环境变量、用户可见行为变更时，同步更新相关 docs。
- 提交前至少跑和改动范围匹配的 lint / test / OpenAPI / docs 校验。

## License

MIT

讨论、历史、缺陷报告、Mask 格式／修复及点云质量入口见 [讨论、缺陷报告与质量处理](./docs-site/dev/sdk/annotation-quality.md)。回执与版本绑定原选择，派发失败保留可查询的 run/job 身份。

SDK 覆盖全部 558 项稳定公开 REST（含运维），十项执行器私有操作排除，另提供六条公开 WebSocket。任务、租约、相机／轨迹和几何提交见 [任务工作流](./docs-site/dev/sdk/task-workflows.md)；可选 `streams` extra 与有界 NDJSON 监听见 [公开事件流](./docs-site/dev/sdk/streams.md)。
