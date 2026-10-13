---
audience: [project_admin, super_admin]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-09-14
---

# 模型市场（/model-market）

模型市场把 ML Backend 能力目录、运行时观测、注册管理集中到同一个页面；项目管理员使用只读能力视图，超级管理员负责全局运行时与注册管理。

## 目的

跨项目纵览所有 ML Backend 与模型能力。从这里可以一站式：

- 按 model 条目检索能力目录
- 超管查看注册 backend 与未注册容器的实时健康 / GPU / pool 状态
- 超管对注册 backend 执行健康检查、卸载、预热
- 超管全局新增 / 编辑 / 删除 backend，并查看各项目对它的启用状态

## 主要视图

![模型市场列表](../images/superadmin/model-market/list.png)

超级管理员看到三段切换：**能力目录 / 运行时观测 / 注册管理**；项目管理员只看到能力目录和只读注册管理。当前视图写入 `?tab=catalog|runtime|registry`，注册管理内部的子视图与筛选也写入 URL（`registry_view=pools|instances|gpu|projects|issues` 及各子视图的搜索 / 筛选键，见下文），刷新页面、浏览器前进后退和分享深链都能恢复原来的子页与条件。显式切换主 TAB / 子 TAB 和对象跳转会新增浏览器历史；文本搜索只替换当前历史。搜索框保留输入中的空格，匹配时忽略首尾空格。

> **按角色可见范围**：本页对超管与项目管理员开放，但内容按角色收敛。超管看到全部三段（能力目录 / 注册管理顶部不再放全局统计卡——项目数留在项目绑定视图、模型数留在能力目录、运行统计留在运行时观测）。项目管理员**只看到能力目录 + 只读的注册管理**——「运行时观测」段与超管专属子视图隐藏（依赖 super_admin only 的全局接口），深链到不可见视图时自动回退到其可见页，不发相应超管请求；能力目录也退到 `/instances` 单端点视图（协议卡 + model 卡，不含各项目运行时池富化，不再因拿不到 overview 而整块报错）。项目管理员对自己项目的 backend 启用仍在项目设置里做。

### 1. 能力目录

工具栏固定为三行：第 1 行是搜索、分组、卡片 / 列表切换和唯一「刷新」（对所有 backend 重探 `/setup`，副作用保留在按钮说明里）；第 2 行是任务和模态快捷筛选加「更多筛选」（模型族 / 推理框架）；第 3 行显示当前结果计数和已应用条件标签。全部条件写入 URL（`catalog_q` / `catalog_group` / `catalog_view` 与 `catalog_task` / `catalog_modality` / `catalog_family` / `catalog_infra`，多选使用重复键），刷新、前进后退和分享深链可恢复；「清除条件」只清多选轴，保留搜索 / 分组 / 显示方式。手工改坏的分组 / 显示方式枚举不会静默丢失：渲染回落默认值，条件标签行显示红色「无效」提示并提供移除按钮，点击后从地址栏清掉非法参数。计数口径唯一：`匹配 N / 总计 M 个模型条目` 来自当前实际渲染集合，协议分组下单独标注能力类别数（`N 类协议能力`），不与模型条目数混用，也不把接入数量描述成实时在线数量。注册管理的问题中心也使用附着式筛选面板，服务池、实例、GPU 和 code 保留独立条件；「完成」关闭面板并保留条件，「清除条件」保留文本搜索。详见 [使用筛选](../reference/filtering.md)。

> 「能力目录」与 backend 注册解耦：默认按**协议能力 (task)** 分组渲染协议卡，无 backend 注册时仍完整展示协议层支持的全部能力 + 推荐 backend；详见 [ADR-0037](../../dev/adr/archive/0037-protocol-capability-catalog-decoupling)。

<!-- TODO IMAGE_CHECKLIST: images/superadmin/model-market/protocol-card-details.png — 能力目录协议卡 + ModelCard 详情态。 -->

能力目录默认按**协议能力 (task)** 分组：

- 始终渲染 9 张协议卡（detection / obb / segmentation / keypoint / classification / ocr / doc_layout / tracker / interactive_seg），数据来自 `GET /v1/ml-capabilities/protocol`（与 backend 注册无关）。
- 已注册 backend 的 model 按 `model.task` 字段挂载到对应卡片下；卡片标题旁显示「N 个模型已接入」徽标。
- 未挂任何 model 的协议卡显示「暂无接入」徽标，首层保留一行典型模型说明和「去注册 backend」入口，推荐后端列表收进「推荐后端」展开区域。
- 零接入时顶部加 onboarding 横幅，强调「平台支持 9 类 AI 标注能力，当前还没有 backend 接入」。

已接入的 model 会复用统一的 ModelCard 展示，**首层摘要化**：

- 首层保留选型所需信息：名称 + 来源后端、任务（协议分组下不重复）/ 模态 / 原子·内置流程 / 交互式 / **可批量 / 交互·有状态** / 设备徽标、一行**输入 → 输出摘要**（如「整图 / 框提示 → bbox」）、以及驻留情况——已知的池大小与加载状态，instances 单端点路径如实显示「运行状态未知」；来源 backend 未连接时显示「缓存」提示。
- 「详情」按钮展开第二层：推理框架、模型族、完整可接受输入、输出几何 / 输出属性（schema label 优先，回落扁平 `output_attribute_types`）、资源余项（vram 等）、变体组合与淘汰历史。展开按钮是独立按钮，不把整卡包成大按钮。
- 现有规则允许时首层直接提供预热按钮；预热执行逻辑未变，浏览展开不会触发预热。
- 若 backend `/setup` 里 task、infra、prompt 或几何枚举不在平台受控词表内，卡片右上角会显示 `⚠ 协议 N`，hover 可看具体字段和值；这是诊断提示，不会阻断目录解析。

切换到「分组：backend / infra / 不分组」时进入 model-centric 视图（按 model 条目展开，任务徽标恢复显示）。除了项目已启用的 backend，平台已知但未接入任何项目的内置 backend（如 docker-compose 自带的）也会在这里列出并标「平台内置」；仅当平台完全没有已知 backend 时才显示空态。

通用筛选：

- 卡片 / 列表切换；协议能力分组下列表按协议能力逐行展示，backend / infra / 不分组下列表按 model 条目展开。
- 搜索模型名、model id、模型族、task 中文标签和来源 backend；协议能力分组下，搜索同时过滤协议卡（命中 task label / summary / typical_models 的卡保留）。
- 与 task / model_family / infra / modality chips 过滤叠加。

同一维度内选择多项表示满足任一项，不同维度之间需要同时满足。模型族筛选也作用于协议分组；基础设施和模态按模型实际展示的能力匹配，切换分组不改变筛选含义。

协议列表视图中，每个协议行保持紧凑（能力、输入/输出、接入数量、模型名称摘要）；已接入超过 3 个模型时显示「查看全部 N 个模型」按钮，点击在行下实际展开全部模型的摘要卡（替换旧版不可点击的「+N 个模型」文字）。

### 2. 运行时观测

运行时观测是 runtime-centric 视图（**仅超管可见**）。它以**服务池**为默认比较层：页头是一条紧凑摘要带（路由模式 · 可路由实例 / 服务池数 · 需关注服务池 · 数据来源新鲜度与未纳管容器数），其下按服务池摘要卡 → 实例面板 → 详情 Sheet 逐级下钻。宽屏下服务池以两列排布，展开的卡片自动跨列；窄屏回落为单列。数据来自 `topology` + `runtime-snapshot` 两个权威读模型，按稳定 ID 关联（不再按 URL join）。

<DocsVideo
  src="/media/superadmin/model-market/runtime-pools.mp4"
  poster="/media/superadmin/model-market/runtime-pools-poster.webp"
  alt="模型市场运行时观测页依次展示服务池摘要、数据来源、车辆检测双实例及两台实例的详情面板"
  caption="从服务池下钻到实例：核对路由模式、数据新鲜度、权重、当前并发与 GPU 驻留；尚未接入的共享路由指标保持未知语义。"
/>

页面提供单一「刷新」动作 + 自动刷新开关，并展开「数据来源」区域显示各来源（拓扑 / 路由账本 / 健康探活 / GPU 仲裁 / 模型驻留）的 `updated_at` / `stale` / `error`。单个来源失败不会抹掉其它可信数据——例如路由账本不可用时，拓扑与最近健康配置仍然展示，数据来源区域显示部分可用告警。

<DocsVideo
  src="/media/superadmin/model-market/runtime-data-sources.mp4"
  poster="/media/superadmin/model-market/runtime-data-sources-poster.webp"
  alt="模型市场运行时观测页展开数据来源，对比新鲜的拓扑与连接超时的路由账本"
  caption="局部观测源退化不等于全局中断：页面保留 4/5 个可信来源与两个服务池，并在陈旧来源上明确呈现超时原因、上次更新时间和退避状态。"
/>

**服务池摘要卡**是两行轻量字段带（plan §4.2）：第一行是身份与独立状态轴——展开按钮、池名 + 短 ID（tooltip 可看全值）、路由策略、**健康**与**路由**分开的徽标、数据新鲜度；第二行是带标签的字段：可路由 / 总实例（附停流、离线计数）、并发 inflight（缺失显示「暂无路由指标」而非 0）、驻留数（未知如实显示「未知」）、CPU 回退数、流量（无指标时只显示一条「暂无路由指标」，不画空分布条）。非正常状态的 reason code 显示在「状态依据」行。正常池保持 topology 稳定顺序，不随轮询数值重排；卡片无装饰悬浮动画，刷新不改变展开状态。

> **「暂无路由指标」**：P95、错误率、最近选择、选择 / 拒绝计数等流量真值在合同中保留为 `null`，前端统一显示「暂无路由指标」，不会回落为 `0` 或「健康」。这些字段等后续版本接入共享路由计数器后才会显示真值。

资源区的驻留实例数只统计可信探活中处于加载、已驻留或释放中的实例；`unloaded`、未知状态与过期缓存均不计入。实例面板首层展示名称、健康、路由、并发、驻留与缓存状态（缓存中的 `connected` 不会显示成实时健康），缓存指标、GPU claim 与维护操作在展开区内；缺失的路由指标统一保留未知语义，不显示为零。点「详情」打开右侧 Sheet，里面才展示模型 / 视频驻留池、cache、variant、builder / borrower、generation、原始诊断和复制 ID。

<!-- TODO IMAGE_CHECKLIST: images/superadmin/model-market/runtime-instance-detail.png — 实例详情 Sheet（路由、健康、GPU claim、驻留与诊断） [manual] -->

> **信任边界**：`/observe` 直连 `ML_BACKEND_OBSERVE_URLS` 里的地址探活，**不带应用层鉴权**——它假定这些地址在可信内网、免鉴权可达。请勿把该变量指向可从不受信网络到达的地址；需要鉴权的 backend 应通过「注册管理」注册（注册项携带 `auth_method` / `auth_token`，走鉴权链路），而非只靠裸 observe URL。

**未注册容器**单独放在折叠区：只显示直连 health / latency / compute / GPU / 模型驻留；允许显式注册或 smoke test，但**不展示权重、routable、流量分布，也不自动并池**。

**实例维护走安全顺序**：drain（停止接收新请求）→ 等待 inflight 归零且快照新鲜（quiescent）→ 卸载。成员必须精确处于 `draining`；`route_inflight` 缺失、账本 stale 或 Redis 不可用都不能作为零证明。当 `ML_BACKEND_ROUTER_MODE != enforce` 时，drain 只标记为「预配置未生效」而非「已停流」。同一权威门禁也用于移除服务池成员和物理删除 registry。

### 3. 注册管理

<!-- TODO IMAGE_CHECKLIST: images/superadmin/model-market/registry-service-pools.png — 注册管理服务池主视图（结构化 tab + 展开成员 + 维护操作） [auto] -->

注册管理按实体拆成四个结构化视图 + 问题中心，超管看到全部五个 tab，项目管理员只看到服务池 + 实例两个只读视图。子视图写入 `?registry_view=…`，每个子视图拥有自己的搜索 / 筛选条件与明确的主操作（实例视图突出「注册实例」，服务池视图突出「新建服务池」）；tab 行上的计数来自对应数据源，请求未完成时显示加载占位、失败且无缓存时显示「?」，不会把未就绪或失败的数据冒充成 0。问题中心 tab 的徽标按当前最高诊断严重度着色。每个物理 backend 全局只有一行，注册一次、所有项目共享；服务池是路由选择的逻辑边界（ADR-0050），实例是可定位到物理 URL 的 registry 记录。

各子视图的 URL 条件键：

| 子视图   | URL 键                                                                                 | 清除条件范围                           |
| -------- | -------------------------------------------------------------------------------------- | -------------------------------------- |
| 服务池   | `pool_q`、`pool_health`                                                                | 清健康筛选，保留搜索                   |
| 实例     | `instance_q`、`instance_health`、`instance_pool`（池定位条件）                         | 清健康筛选与池定位，保留搜索           |
| GPU 资源 | `gpu_q`                                                                                | 只匹配本页资源 ID / 节点 / 设备        |
| 项目绑定 | `project_q`、`binding_view`、`binding_pool`                                            | 切视图保留搜索；池限定可单独移除       |
| 问题中心 | `issue_q`、`issue_severity`、`issue_pool`、`issue_instance`、`issue_gpu`、`issue_code` | 清附着筛选与严重度，保留 code 文本搜索 |

对象定位键 `pool_id` / `instance_id` / `gpu_id` 与问题筛选键分开：从问题中心「定位」跳到对应子视图会清除目标页可能遮住对象的搜索和健康筛选，再展开目标对象或打开详情 Sheet；问题中心的筛选条件保留。服务池「查看实例」同样移除旧的实例搜索和健康筛选，并保留本次服务池限定。实例详情随浏览器后退关闭，前进时恢复；已删除或当前角色不可访问的对象显示「对象不存在或当前不可访问」，不会静默扩大到全部对象。非法的筛选枚举会给可见提示和移除入口，不静默丢弃。

**服务池**（super_admin + project_admin）：

| 主列   | 内容                                                                                                           |
| ------ | -------------------------------------------------------------------------------------------------------------- |
| 服务池 | 名称、短 ID（tooltip / 复制按钮可取完整值）、策略                                                              |
| 成员   | 可路由 / 总实例，按状态分段                                                                                    |
| 容量   | inflight / 总并发、饱和拒绝；明确标注「暂无路由指标」当无计数                                                  |
| 项目   | 启用项目数                                                                                                     |
| GPU    | 关联 GPU 的最高诊断严重度                                                                                      |
| 状态   | 健康、configured → effective 路由、router mode、数据新鲜度分开显示                                             |
| 操作   | 创建、重命名、启停、删除空池，加入 / 移除成员、修改权重、暂停 / 恢复接流、查看实例、审核能力变更（**仅超管**） |

服务池行可展开成员实例；菜单中的「查看实例」会跳到实例子视图并按该池过滤（`registry_view=instances&instance_pool=<id>`）。项目管理员看不到 `routing_policy`、权重与 GPU 列（服务端裁剪为 `unknown` / `null`，非前端隐藏），也没有操作按钮。

**实例**（super_admin + project_admin）：主表收敛为六列——实例身份（名称 + 短 ID + URL 副行 + 来源轻标签）、所属服务池、健康及路由（健康 / 接流 / 路由分开显示）、并发与 GPU 摘要、最近检查、操作（详情 + 超管操作菜单：健康检查 / 编辑 / drain / resume / 审核能力变更 / unload / 删除，按风险排序）。详情 Sheet 展示完整实例 ID 与 URL、所属服务池、接流与路由、权重、声明并发上限、GPU 静态预算与仲裁模式、GPU 诊断及健康自报摘要。当能力指纹变化导致成员被自动禁用时，「审核能力变更」会展示服务池基线与实例当前合同的差异；确认动作重新探活并复核候选指纹，只有池内其余接流成员仍然等价时才原子恢复成员和服务池。

**GPU 资源**（**仅超管**）：顶部汇总运行时就绪状态、全局期望模式、Observe / Enforce 就绪性和 Rollout 状态；资源表按资源身份（设备序号 / 节点 + 短 ID，未提供设备序号时显示短 ID 而不凭空命名）、静态声明 / 预算（已声明预算、backend 数、弹性超售）、运行时 committed、队列与租约、desired → effective 模式和最高诊断展示。静态声明超售与运行时实际占用是两根独立 Progress 条，预算未知时显示文字而不是空进度条冒充 0%。资源行展开后列出受影响实例。`gpu_q` 可按资源 ID / 节点 / 设备搜索。

<DocsVideo
  src="/media/superadmin/model-market/gpu-resources.mp4"
  poster="/media/superadmin/model-market/gpu-resources-poster.webp"
  alt="模型市场 GPU 资源页展示运行时就绪状态、两张卡的静态与运行时预算，并展开阻断卡查看受影响实例"
  caption="从全局就绪性下钻到单卡：区分静态声明与运行时 committed，核对 desired → effective，并从 blocker 资源反查受影响的 L4 实例。"
/>

**项目绑定**（**仅超管**）：默认按项目显示关联服务池、可用实例数和风险，支持切换为按服务池反查项目；关联关系依据当前拓扑与已启用后端展示推导（页面标注「依据已启用实例关联」），真实配置仍以项目设置为准。可用 `project_q` 搜索（按项目视图搜项目、按服务池视图搜池）与 `binding_pool` 池限定。本页只读，修改入口跳项目设置。项目已启用但池内无可路由实例时单独告警。

**问题中心**（**仅超管**）：按 `code + subject_type + subject_id` 稳定去重，同一问题只渲染一次主记录；默认按 blocker → critical → warning → info 排序。`issue_q` 做 code 文本搜索，严重度快捷筛选与附着的服务池 / 实例 / GPU / code 条件独立叠加；筛选选项优先显示对象名称（重名附短 ID），实际筛选值仍是稳定 ID。每条主记录的对象与影响对象显示可读名称，可直接「定位」跳到对应子视图；源数据没有可匹配记录时显示非交互摘要。资源 / 实例行不复制诊断全文。

<!-- TODO IMAGE_CHECKLIST: images/superadmin/model-market/registry-issue-center.png — 问题中心去重、严重度、受影响对象与筛选 [manual] -->

对应后端端点：`GET /admin/ml-integrations/topology`（角色裁剪读模型）、`GET /admin/ml-integrations/runtime-snapshot`（仅超管）、`/admin/ml-integrations/service-pools/*`（pool/member CRUD + drain/resume，仅超管）、`POST/PUT/DELETE /admin/ml-integrations/registry/:id`（实例增删改）、`POST /admin/ml-integrations/registry/:id/health`（健康检查）。

> **角色门控**：超管看到全部五个 tab 与所有 mutation。项目管理员只看到「服务池 + 实例」两个只读视图，不显示 GPU 资源 / 项目绑定 / 问题中心，也看不到 `routing_policy` / 权重 / GPU UUID / 预算 / 内部 reason / 诊断拓扑——这些字段在服务端响应中已裁剪为 `unknown` / `null`（非前端隐藏）。项目管理员对自己项目的 backend 启用仍在项目设置里做（详见 [启用 ML 后端](../projects/ml-backends)）。

## 视频追踪观测与任务监控

模型市场区分**图像推理**与**视频追踪**两种模态。

### 观测 / 预热面板按模态拆分

backend 的变体面板拆成两组：

- **图像推理变体**：SAM + DINO 双下拉，预热加载到图片池（grounded-sam2 图片 predictor）。预热走 `POST /{backend_id}/reload`（含 `task_type` 可选体）或 `POST /{backend_id}/warmup`（协议 v2 §4.4，backend 声明 `warmup_endpoint=true` 时启用）。
- **视频追踪权重**：video tracker 不使用 DINO，预热加载到**独立 video 池**（`POST /reload` body 携带 `task_type=video`）。有多档视频权重时显示 SAM 下拉；只有单一视频模型时显示独立权重条目和预热按钮，不制造无意义下拉。
- 分组是否显示优先读取健康检查落库的 `health_meta.capabilities.modalities`；纯图像 backend 不显示视频组，纯视频 backend 不显示图像组。未健康检查过、没有 modalities 快照时，页面回落到 `/setup` enum / tracker 判断，避免把未知能力的 backend 误隐藏。

<DocsVideo
  src="/media/superadmin/model-market/video-pool.mp4"
  poster="/media/superadmin/model-market/video-pool-poster.webp"
  alt="模型市场运行时观测页展开 SAM 3 视频追踪实例，查看独立视频池容量、活跃会话、GPU 驻留与视频权重预热入口"
  caption="视频 tracker 使用独立显存池：从服务池下钻到实例，核对视频池 1/3、活跃会话、SAM 3.1 常驻状态和只作用于视频池的预热入口。"
/>

> ⚠️ **常见误区**：视频 tracker 用的是独立 `_video_pool`，不能只预热图片池。视频组的预热会走 `POST /reload` 并传 `task_type=video`，正确加载 video 池。
>
> 若 backend 自报支持视频但未上报 `video_pool`（旧版本），视频组会降级提示，不影响图像组。

### 视频追踪任务监控

视频追踪任务监控已并入 [`/ai-pre/jobs`](../projects/ai-preannotate) 的「视频」模态 tab；图像 tab 由 `async_jobs(kind=batch_predict|prediction_retry)` 提供，视频 tab 由 `async_jobs(kind=video_tracker)` 提供。旧链接 `/model-market/video-jobs` 自动跳转到 `/ai-pre/jobs?tab=video`。**模型市场只保留后端 / 显存池健康观测**（上面的模态拆分预热面板），任务（job）历史归 ai-pre。

<!-- 注：「能力目录」完整双层架构已在 ## 1. 能力目录 节描述，此处不再重复。 -->
<!-- 如需实例层细节请参见 ADR-0037 与 ML Backend 协议文档。 -->

## 新建 / 编辑 Backend

点「注册管理」实例子视图（或服务池视图）工具栏的 **「注册实例」** 即可新增一条全局 backend 记录；点某行的「编辑」可改其 URL / 名称 / 鉴权 / GPU 资源与预算 / 驱逐优先级 / `max_concurrency` 及其他 `extra_params`。注册表单字段与校验详见 [ML Backend 注册](./ml-backend-registry)。env 配置的 backend 启动后会自动出现在列表里（来源 `env`），无需手动注册。

## 删除

「注册管理」列表支持逐条删除全局 backend（**仅超管**）。删除会从注册表移除该记录，所有引用它的项目 `ml_backend_id` 自动置空（ON DELETE SET NULL）。若有正在运行的预测 job 会返回 `HTTP 409` 阻断。项目管理员若只想让某 backend 对本项目失效，应在项目设置取消勾选启用，而非删除。删除规则详见 [ML Backend 注册](./ml-backend-registry#删除)。

## 路由历史

| 旧路由                               | 新路由                       | 跳转方式                                             |
| ------------------------------------ | ---------------------------- | ---------------------------------------------------- |
| `/model-market/video-jobs`           | `/ai-pre/jobs?tab=video`     | 前端 `<Navigate replace>`（客户端跳转，非 HTTP 301） |
| `/model-market?tab=failed`（旧书签） | `/ai-pre/jobs?status=failed` | 前端检测 `tab=failed` 自动跳转                       |

> `/ml-integrations` 路由**没有**配置 301 重定向，该旧路径已废弃（无路由匹配则 404）。`/admin/ml-integrations/*` 是后端 API 路径（仍在使用），与前端页面路由不同。
