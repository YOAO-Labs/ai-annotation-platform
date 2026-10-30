# 推理后端插件化、Plugin-SDK 与官方插件独立仓库规划

> 状态：阶段 C 与 C2 已完成；阶段 D 已切换三个后端的源码、构建和维护消费者。新独立 ONNXTools／RapidOCR 镜像已通过各自完整单卡 GPU 生命周期资格及实际 SDK 安装、平台激活；YOLO 完整 CUDA 资格和新镜像的平台业务／治理闭环仍在推进，不能用旧镜像或 CPU 证据替代。
>
> 日期：规划于 2026-10-28，实施记录更新于 2026-10-31。公开仓库：[YOAO-Labs/Plugin-SDK](https://github.com/YOAO-Labs/Plugin-SDK) 与 [YOAO-Labs/YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins)。
>
> 授权范围：使用 Orca、OMP `openai-codex/gpt-6.1-terra`／`xhigh` 和独立工作树，由主进程审核并合并提交。用户已授权公开必要第一方制品、补齐既有 MIT 许可，并要求原有 vendor 改为固定 tag／哈希的 Git 子模块；不因此发布应用版本、扩展前端 SDK 或公开未获授权的权重。
>
> 前置资料：[能力边界与生态研究](../research/51-plugin-capability-boundaries-and-ecosystem.md)、[LibreYOLO 试点](1793156248_libreyolo-inference-plugin.md)、[核心模块部署与边界台账](../../docs-site/dev/concepts/module-deployment.md)。

## 1. 决策

采用 **平台拥有契约、SDK 按能力提供作者抽象、官方仓库提供具体实现**。运行语言是能力包的实现选择，不是把模型接口提升为所有插件基类的理由。统一的是可验证的身份、兼容与治理边界，不是模型基类、依赖环境或部署进程。

| 层次                     | 职责                                                                                                 | 明确不负责                                                                            |
| ------------------------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 统一插件契约             | manifest、身份、能力声明、版本兼容、权限、配置、安装与生命周期；需要时声明后端与前端组件及其兼容关系 | 不能凭声明获得权限、GPU 资格或替平台决定业务终态                                      |
| 推理能力 SDK             | 提供自定义推理插件的作者接口、HTTP 适配、开发工具与一致性检查；运行时机制与模型实现分离              | 不要求安装官方插件、Torch 或指定模型；检测接口与 GPU 生命周期不成为其他能力的基础     |
| UI／训练器／数据导出 SDK | 在实际扩展点落地时提供各自的输入、输出、宿主操作与一致性检查，分别声明依赖与兼容范围                 | 本期不实现空包，不依赖推理 SDK，不复制领域权限或业务终态规则                          |
| 平台宿主                 | 权限校验、扩展入口注册、资源管理、隔离、审计、安装治理                                               | 不替代 Identity、Assets、Learning、Jobs、Labeling 等领域 owner 的业务规则与最终提交权 |

后端以既有 HTTP 契约为当前边界；需要事件交互时沿公开、类型化且受授权的事件契约扩展，不提前增加通用事件总线。非 Python 实现可直接实现协议，SDK 只是便利工具。

一个完整插件将来可以同时包含后端镜像和前端界面，由同一份 manifest 描述组件及兼容关系。当前只实现推理能力配置，不为了可能的 UI 提前增加空组件、万能 hook 或通用插件框架。

### API SDK 与插件 SDK 的关系

采用 **分包、独立版本、共享必要契约、统一开发流程**，不合并为一个 SDK distribution。API SDK 用于调用平台，插件 SDK 用于实现供平台调用的服务；“管理插件”不等于“开发插件”。正式职责与依赖规则见[API SDK 与插件 SDK](../../docs-site/dev/concepts/module-deployment.md#api-sdk-and-plugin-sdk)。

- `ai-annotation-sdk` 及管理员 `aap` CLI 留在平台仓库，负责公开 API 和插件安装治理；插件作者抽象及工具在独立 `Plugin-SDK` 仓库发布，当前推理 distribution 为 `aap-inference-plugin-sdk`，开发命令为 `aap-inference-plugin`。`YOAO-Plugins` 只维护官方插件及模型专属实现。
- API SDK 核心安装不引入插件服务或模型运行时；插件 SDK 不默认依赖 API SDK。具体插件确有调用平台公开 API 的需求时，可显式依赖 API SDK，使用单独授予的作用域凭据，不继承管理员身份或访问私有应用包。
- 只共享跨边界必需的 manifest、安装身份、资源绑定、证明 schema 与纯校验规则；消费既有契约包，或从同一权威 schema 生成对应类型，不手工维护第二份类型真值，也不把平台全量 API DTO 搬入插件 SDK。
- 两套 SDK 分别声明平台 API、插件协议及宿主兼容范围，独立发布；需要组合使用时验证兼容组合，不要求版本号相同或随任一模型插件一起升级。本轮不新增统一安装包或命令包装层。

### 前端信任边界

- 第三方可安装前端优先使用**独立来源的受限 iframe**及明确的消息接口，不直接动态导入任意 React 代码到主应用。
- 信任等级由宿主依据来源、审查与管理员授权决定，不能由 manifest 自报 `trusted` 即生效。
- 消息接口需校验 origin、source、消息 schema、请求身份和操作权限；不转发原始登录 Token，不提供任意平台请求代理。
- 插件获取的是获准上下文和平台操作。写入时仍由相应领域重新校验权限、任务版本和执行状态，不能直接操作标注状态或绕过 Workbench 写入约束。
- 内置、受审查扩展可以采用宿主组件接口。两种方式复用能力契约，但不共享相同隔离策略。
- 只有出现具体的前后端联合插件用例，才定义所需扩展槽位、消息协议、联合升级／回退和 TypeScript SDK。本轮推理迁移优先使用现有参数表单与结果界面。

## 2. 迁移范围与事实基线

现有五个后端加上独立 LibreYOLO，共六个迁移单元。以现有后端的运行依赖和生命周期为单位，不按每个模型规格建仓或建插件。

| 插件单元         | 当前代码入口                                                       | 必须保留的实际能力                                                                                             | 主要特殊性                                                             |
| ---------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| LibreYOLO        | 官方仓 `plugins/inference/libreyolo`，原生 loader 位于其 `native/` | 原生 YOLOv9 t/s/m/c 检测、受控训练权重安装与试用；显式 CPU 和经验证的 CUDA 路径                                | 首个试点；不能再走 Ultralytics loader，也不能改写历史 CPU 产物身份     |
| Ultralytics YOLO | 官方仓 `plugins/inference/yolo`                                    | 检测、分割、姿态、旋转框；YOLO-World／YOLOE 文本与 exemplar；ByteTrack／BoT-SORT；已有受控检测／分割与评估资格 | 不能把“YOLO 插件”缩成 bbox 检测，也不能因 LibreYOLO 拆出而删除这些能力 |
| RapidOCR         | 官方仓 `plugins/inference/rapidocr`                                | `ocr-det`、`ocr-rec`、`ocr-e2e`；文本、方向、语言属性与合法模型组合                                            | ONNX Runtime；动态引擎池及每个引擎实际 det／cls／rec sessions          |
| ONNXTools        | 官方仓 `plugins/inference/onnxtools`                               | 车辆检测、车型／颜色分类原子，以及检测后分类 composite                                                         | 三句柄、四个业务 sessions；两份权重仅获准内部验收，不代表可公开分发    |
| Grounded-SAM2    | `apps/grounded-sam2-backend`                                       | 文本检测／分割、点／框／Mask／涂抹、多候选、框批量分割、SAM2 视频追踪                                          | image／video 双池、embedding、跨窗 session、对象身份与精确 Mask        |
| SAM3             | `apps/sam3-backend`                                                | 文本检测／分割、交互提示、exemplar；SAM3.1 multiplex 与 SAM3 PVS 两类视频能力                                  | GPU-only，image／multiplex／PVS 三池；独立权重授权与有状态追踪         |

这张表基于当前实现调查，不代表六个插件均已独立发布或取得 GPU 资格。SAM vendor 的训练代码或训练 extra 不等于平台已支持 SAM 训练回流。

主要源码依据：

- [YOLO 模型矩阵](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/yolo/model_registry.py)、[YOLO HTTP 服务](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/yolo/main.py)、[受控分割](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/yolo/managed_segmentation.py)。
- [RapidOCR 目录](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/rapidocr/catalog.py)、[引擎池](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/rapidocr/engine_pool.py)。
- [ONNXTools 服务](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/onnxtools/main.py)、[属性 schema](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/onnxtools/attribute_schema.py)、[句柄池](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/inference/onnxtools/handle_pool.py)。
- [Grounded-SAM2 服务](../../apps/grounded-sam2-backend/main.py)、[视频执行](../../apps/grounded-sam2-backend/video_predictor.py)。
- [SAM3 服务](../../apps/sam3-backend/main.py)、[multiplex](../../apps/sam3-backend/video_predictor.py)、[PVS](../../apps/sam3-backend/pvs_video_predictor.py)。

### LibreYOLO 当前进度不能扩大解释

LibreYOLO 已交付独立 SDK、插件及安装治理，在自有隔离环境完成真实 GPU 训练与原生 checkpoint 回流、受控安装试用及人工发布、当前任务／批量／真实裁剪、CPU 历史冻结与退役，以及三种路由模式的在途排空、升级失败恢复和受控重启。原生 close 两轮使 Torch allocated／reserved 回到零，旧 YOLO 中的 LibreYOLO 适配分支已删除；证据及已知范围限制见[试点 Outcome](1793156248_libreyolo-inference-plugin.md#outcome)。

**尚未完成**：阶段 D 的完整模型与平台治理联合验收、两个 SAM 后端的独立迁移、全扩展点统一装配。SDK、共享工具与 LibreYOLO 的唯一源码切换、公开 Python 制品、干净构建及拆仓后的平台业务验收已按第四批完成；三个普通后端的源码切换和新镜像资格按第五批单独记录。单卡串行通过不证明双卡隔离、并发调度、模型精度或生产容量；代码、依赖及权重的许可前置也不会因本机可运行而消失。

## 3. 仓库边界

采用三个明确所有者：平台拥有业务与契约权威，`Plugin-SDK` 独立发布自定义插件开发抽象，`YOAO-Plugins` 独立发布官方维护的具体插件。第三方作者不需要把插件提交到官方仓库。每个能力包、插件、锁文件与运行环境独立维护，不要求所有插件共同升级。

| 内容                                                                                      | 唯一源码归属                       | 跨仓使用方式                                                                   |
| ----------------------------------------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------ |
| 核心 Inference owner、Registry、路由、GPU 仲裁、权限、Jobs／Labeling／Learning 业务与 SQL | `ai-annotation-platform`           | 公共服务／有限 owner 契约；禁止搬入插件仓或让插件直接读取核心表                |
| 公共 wire 类型、manifest schema、任务与受控训练产物契约                                   | 平台维护的便携契约包               | 发布版本化 wheel、JSON Schema 与一致性夹具；插件消费制品，不复制另一份类型真值 |
| 平台 Python 客户端及管理员 CLI                                                            | `packages/python-sdk` 留在平台     | 通过公共 API 管理安装；不作为插件 SDK 的默认运行依赖                           |
| 自定义插件作者接口、能力专属开发工具、推理运行时与 Mask 工具                              | `Plugin-SDK`                       | 能力分包、独立 Python distribution；不依赖官方插件、平台 checkout 或私有应用包 |
| 官方算法服务、模型适配、LibreYOLO 原生 loader、vendor、镜像与资格工具                     | `YOAO-Plugins`                     | 消费 SDK 发布制品；每个官方插件独立发布，OCI 按 digest 安装                    |
| 权限与资格证明的解释、准入决定                                                            | 平台宿主／Inference owner          | 插件提供事实与证明，不能自行批准或修改资格标准                                 |
| 模型权重与受控训练产物                                                                    | 各自获准的资产渠道／平台领域 owner | 授权准备、摘要校验、只授予所需访问；不随代码仓任意复制或公开                   |
| 插件预训练模型／变体、文件身份、来源、许可与兼容描述                                      | 对应插件，随插件版本发布           | 平台读取并校验，不另建手工维护的模型清单；展示不授予下载、启用或再分发权限     |
| 审批、可用状态、项目授权、存储位置及 S3 入库／缓存准备                                    | 平台管理员工具与领域 owner         | 插件进程不得持有 S3 管理凭据；用户训练 checkpoint 仍归 Learning／Assets        |

平台依赖的**便携证明 schema／纯校验规则**已收口到无第三方运行依赖的 `aap-lifecycle-contract`。生产 API 和管理员 CLI 使用该契约，API SDK 的 `plugins` extra 不再依赖模型后端运行时，训练契约按实际用途保留；证据生产、文件摘要采集、模型装载和清理机制继续由 `aap-backend-runtime` 持有。此次提取保持原证明格式与阈值，不把宿主权威校验交给具体插件；多模型能力匹配、多池资格与跨仓制品渠道仍需后续验收。

Mask 算法工具可随模型代码迁移；平台的几何语义、像素格式与现有一致性夹具仍受核心契约约束，不借拆仓改写编码或增加新的权威格式。

预训练清单迁移按现有 34 个受控 `.pt` 文件身份拆分：Ultralytics 插件持有 30 项，LibreYOLO 插件持有 4 项，保留原大小、摘要与来源。RapidOCR、ONNXTools 发布各自格式的模型描述，不强行套用 `.pt` 缓存契约，不捏造未知来源或许可。平台契约构建器停止导出混合清单；历史不可变发布保留原样，但不再充当新插件的目录源码。

平台保留 `scripts/prepare-controlled-model-assets.py`：本地导入时校验大小和 SHA256，批准后按 `model-assets/sha256/<摘要>/weights.pt` 入训练桶，或取回已核验字节准备节点缓存。此工具不发布公网插件，不把权重上传权限交给模型运行进程。

独立插件仓库放在平台工作树的 **`plugins/YOAO-plugins/`**，本地目录名按此大小写固定；远程仓库仍为 `YOAO-Labs/YOAO-Plugins`。平台根 `.gitignore` 使用 `/plugins/YOAO-plugins/` 忽略整个嵌套仓库，其代码由插件仓库独立跟踪和提交，不作为平台目录或 Git submodule 提交。

目标结构按能力分组，当前只创建已有实现。UI、训练器和数据导出不预建空包；未来新增能力不得反向依赖推理运行时：

```text
Plugin-SDK/
  packages/
    inference/
      sdk/
      runtime/
      mask-utils/
  scripts/

plugins/YOAO-plugins/
  plugins/
    inference/
      libreyolo/
        native/
      yolo/
      rapidocr/
      onnxtools/
  scripts/
```

暂不创建前端 SDK 占位目录、不拆成六个插件仓库；平台不把 `YOAO-Plugins` 自身作为 Git submodule。第三方 vendor 则按固定提交使用子模块，两者不是同一规则。跨仓依赖用发布制品；仓内开发可使用 workspace sources，但发布 wheel 与镜像必须不含指向另一个 checkout 的 editable/path 依赖。

同一能力的代码统一归入 `packages/inference/`，不是用三个同前缀目录代替能力层级；内部保持现有 Python distribution／import 名称。runtime 和 Mask 支持 Python 3.10，作者 SDK 要求 Python 3.11，独立分发避免下游无关依赖与运行版本被提升。当前检测作者接口仍明确限定推理能力，不能宣称已覆盖 UI、训练或导出。未来能力沿各自 owner 的契约扩展：UI 使用受限宿主操作，训练器交付受校验的运行及产物描述，导出器消费获准输入并交付结果；权限、数据账本和最终业务提交留在平台。

真正跨能力复用的身份、兼容与配置契约才进入公共层；不从推理包抽出一个带 GPU、HTTP 或模型字段的“通用插件基类”。现有 `aap_inference_contract` 仍是推理契约，不把它改名后冒充全能力合同。

## 4. 规划基线中的接缝

下表保留规划时的源码调查快照，行号对应当时实现，**不是当前未完成清单**。其中通用能力匹配、多池与 CPU／CUDA 安装已在第二批验收；依赖声明准备和第三批候选制品见后文记录。检测小接口有意保留，实际拆仓与干净跨仓构建仍须独立验收。

| 规划时限制                                                 | 当时源码依据                                                                            | 扩展要求                                                                                     |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| manifest 能力词表只有 `inference/detection/managed_models` | `apps/_shared/inference_contract/src/aap_inference_contract/plugin.py:47-76`            | 复用既有 Protocol v2 能力语义，按实际迁移用例扩展；不允许任意 JSON 冒充未知能力              |
| 激活校验只接受一个 `detect`、非交互、bbox、固定输入组合    | `apps/api/app/services/plugin_installation_observation.py:76-90`                        | 由获准能力配置与实际 `/setup` 做对应验证，支持多模型目录；不能直接删除校验                   |
| 资格拓扑固定为 `models` 池、cap 1                          | 同文件 `:150-154`；`packages/python-sdk/src/ai_annotation/plugin_deployment.py:256-263` | 验证声明、实测报告和运行观测的全部命名池，包含 ORT sessions、embedding 与视频借用            |
| 本地安装器固定 `cuda:0`、GPU 绑定及 LibreYOLO 数据路径     | `packages/python-sdk/src/ai_annotation/plugin_deployment.py:158-199`                    | CPU／CUDA 形成明确运行配置；无 GPU 的插件不能被要求伪造 GPU 证明；配置不得按插件名继续堆分支 |
| 开发接口只返回 `Sequence[Detection]`                       | `packages/inference-plugin-sdk/src/aap_inference_plugin/types.py:11-30`                 | 保留小检测接口；复杂后端使用完整 HTTP 应用适配，不强迫所有任务继承同一个类                   |
| Docker context 和开发依赖指向平台 `apps/_shared`           | 各 backend Dockerfile、pyproject；`docker-compose.ml.yml`                               | 在新仓干净检出可构建；依赖、vendor、权重和编译前置都有明确来源                               |
| 部分服务实际依赖只在 Dockerfile 安装                       | RapidOCR／ONNXTools pyproject 与 Dockerfile                                             | 补齐对应运行分发的依赖声明和锁文件，不让“pip 安装成功”掩盖无法启动                           |

统一能力声明不是再维护一份与 `/setup.models[]` 漂移的模型目录：manifest 表示获准意图与资源／权限，运行观测表示实际能力，宿主只开放两者一致且已取得对应资格的能力。未知能力、身份不符、资格过期或池拓扑不符应明确拒绝。

CPU、CUDA、设备故障回退策略须分别声明并验收。不得把 CPU 执行记作 GPU 合格，也不在搬迁中默默删除现有设备回退行为。SAM3 没有 CPU 路径，不能为了统一接口伪造它。

运行时不统一升级：当前 SDK 要求 Python 3.11 及以上，而 Grounded-SAM2 的镜像基线是 Python 3.10，且包含特定 CUDA 扩展。它可沿完整 HTTP 契约包装；兼容性必须经真实构建验证，不能靠降低 `requires-python` 或顺带升级全部 Torch/CUDA 解决。

## 5. 开发者路径

提供两种受同一契约约束的接入方式：

1. **新建简单推理插件**：作者实现 `load`／`predict`／`close` 接口与目录配置，SDK 完成协议、认证、借用和边界转换。`aap-inference-plugin init/dev/check/build` 不默认安装官方模型；生成的可运行 CPU 图像分析示例仅演示作者接口，不冒充预训练模型，也不代表已支持全部推理任务。
2. **迁移既有复杂服务或其他语言实现**：保留原 HTTP 服务和模型池，通过 manifest、安装身份、配置、准入与生命周期接入。先证明完整功能不丢失，再提取有实际复用价值的共享机械过程。

打包也允许两条路径：Python 检测作者使用已存在的离线 wheelhouse／hash lock 构建；包含 CUDA 编译、vendor 或其他语言的服务保留明确的独立 Dockerfile。两者都交付固定 digest 的 OCI 制品，并通过相同的宿主安装与行为资格，不强迫使用一份通用基础镜像。

模板与指南须让开发者能在**没有平台源码、数据库、Redis 和内部签名私钥**的环境完成本地开发与协议检查。最终平台联调使用普通安装和项目启用流程，不要求开发者手写内部表或理解 Celery。

开发指南串联职责：`aap-inference-plugin` 用于创建、开发、协议检查和构建推理插件；API SDK／`aap` 用于连接平台并管理安装、启停和退役。开发者可在联调环境同时安装两者，但插件运行镜像不因此默认包含 API SDK 或管理员凭据；安装与项目启用仍是独立步骤。

## 6. 执行顺序与验收门

### A. 完成 LibreYOLO 纵向切片

保持原计划范围：真实训练 → 原生 checkpoint → GPU 受控加载／试用 → 人工发布 → 当前任务与批量预标注。完成实际制品生命周期、同卡仲裁／隔离、CPU 历史冻结引用与退役证据后，才能删除旧适配器。

修复试点验收发现的具体问题，不在此阶段实现前端 SDK。控制公钥已改为独立只读挂载，并以实际容器写打开返回 `EROFS` 验证；数据目录仍可写，资格报告只读。保留真实失败、不可判定状态和恢复路径，不通过放宽权限或资格校验达成“成功”。

### B. 固定可移植推理契约与分发边界

先完成上一节接缝中的实际前置：多模型／多结果能力匹配、CPU／CUDA 配置、真实多池资格，以及公共契约和运行时的依赖边界。此阶段仍只处理推理扩展点，不建设全平台插件装配器。

验收包括：现有 LibreYOLO 行为不回退；非 bbox／多模型目录不能被错误拒绝，也不能越过许可；CPU 无 GPU 依赖可运行；真实多池不能漏报；契约导入不加载 Torch、模型实现、ORM 或 Celery。已有 HTTP 字段、模型 ID、几何与错误语义保持不变。

SDK 边界另需通过独立安装验证：API SDK 核心安装可导入并调用公开 API，不安装插件服务／模型运行时；插件 SDK 可在没有 API SDK、平台源码或管理员凭据的环境完成本地开发与协议检查；管理员安装工具使用迁移后的便携证明契约，仍拒绝不匹配的身份与资格。组合使用时验证声明的兼容版本，不以“同一 checkout 可导入”代替跨仓分发验收。

### C. 首次实际拆仓：SDK、共享运行时、LibreYOLO（已完成的历史阶段）

先在 `plugins/YOAO-plugins/` 准备独立插件仓库，为公共契约提供能实际获取的版本化制品，再迁入 SDK、共享运行时与 LibreYOLO。本地嵌套位置只方便开发联调，不允许把父目录中的平台源码当作隐式依赖；仍须从插件仓库干净检出构建 wheel／镜像，在没有平台源码的环境验证开发者路径，再连接平台完成与阶段 A 相同的业务验收。

首次公开渠道使用 `YOAO-Plugins` GitHub Releases，固定标签、真实源码提交及逐文件 SHA256。此历史发布保持不可变。后续 SDK 制品改由独立 SDK 仓库发布，官方仓库不再发布通用实现；平台便携契约仍由 `scripts/build-plugin-contracts.py --revision <platform-commit> --output <new-directory>` 导出五个包的 wheel、sdist 与选定源码归档，不含模型清单。构建器固定 `build==1.3.0`、`setuptools==80.9.0`、`wheel==0.45.1`、`hatchling==1.27.0`；分发仓库及标签不冒充平台源码 owner 或提交。普通消费者不要求私有 Token；第三方依赖、模型权重和 OCI 仍有独立分发门禁。

第三批已先交付私有候选：固定提交的 wheelhouse 经真实 loopback HTTP 取回、独立摘要核验和离线安装，并在不挂载平台源码的环境运行生成的作者项目及 OCI 镜像。这证明本地候选闭环，不等于正式 Releases／PyPI／OCI 发布渠道或干净插件仓检出已经完成；不能据此删除旧源码或跳过后续平台业务验收。

每份代码确定唯一源码归属。切换平台依赖、Compose、CI、资产准备脚本、文档及所有调用方后，删除已迁出的旧实现；不保留双维护镜像、跨应用 re-export 或运行时回退到旧目录。历史已部署旧镜像在引用排空前仍可运行，这不是保留第二份活跃源码。

原有 vendor 源码改为 Git 子模块，最终以 gitlink 的完整 commit 固定；tag 仅用于选择版本，不能跟随浮动分支。先比对当前 vendor 与上游，保留必要本地修改和 LICENSE／NOTICE，再切换递归克隆、CI、镜像及源码打包入口。TrackEval 等裁剪／适配副本不能直接替换成未经验证的完整上游导入。该项只改变第三方源码获取和构建方式，不提前迁移其所属后端或改变算法行为。

### C2. 独立作者 SDK 与能力分层（已完成的结构前置）

在三个隔离工作树并行处理：SDK 作者接口及无官方模型依赖的开发路径、SDK 独立构建发布与 CI、官方插件能力目录与构建迁移。主进程维护平台消费者、公开制品渠道及计划，审核并合并每个提交。

验收门：

- `Plugin-SDK` 在无平台与官方插件 checkout 的环境构建、安装和运行自定义推理插件；实际输入产生检测与空结果，不以 mock／echo 代替作者实现。
- SDK 不默认安装官方插件、LibreYOLO native 或 Torch；推理专属 CLI 与 GPU 检查不变成 UI／训练／导出的必需依赖。
- 官方插件源码统一到 `plugins/inference/`；专属 loader 留在所属插件内，通用 SDK／运行时／Mask 旧目录删除，所有构建与消费者使用独立发布制品。
- 平台、官方插件、开发工具和当前文档完成切换；历史 release 与既有运行镜像保持原样，不把目录迁移或 CPU 冒烟当成新的 GPU 资格。

实施证据：

- 公开 SDK 的实际镜像构建命令已运行：固定 Python 基础镜像，完整 30 包 wheelhouse，pip 安装层禁网并逐项核验哈希。私有 CPU 镜像 `sha256:c1021431359d91698a0b88391cdc4e2872576f31df14f6dc22b8392521ce845c` 以 UID/GID 10001、只读根文件系统、只读 Token、可写 `/var/lib/aap-inference` 通过八项 HTTP 检查；无 GPU 设备、官方模型、Torch 或平台源码挂载。系统包层本次实际下载；镜像未公开，也未取得 GPU 或平台部署资格。临时容器和 Token 已清理。

- 独立 SDK 源码提交 `743b0f42d0b2e85dc5722fdab2c97ddc7c6cbaad` 已公开；三个 distribution 位于 `packages/inference/{sdk,runtime,mask-utils}`，发布为 [python-sdk-0.1.0](https://github.com/YOAO-Labs/Plugin-SDK/releases/tag/python-sdk-0.1.0)，manifest SHA256 为 `6cffa1d704efa4f06d3e301623c1310b1c8e1274cd6690194083d78ed4d98f56`。匿名干净检出重建的三个 wheel 与发布字节一致。
- 默认作者项目以 Pillow 实现真实颜色区域检测，不使用预训练权重。公开 wheel 安装、非 editable 作者包、`dev` 与 HTTP `check` 已通过双区域／空图、类别／坐标／分数、认证、参数和本地路径拒绝检查；修改类别名和阈值后重新构建的作者插件也通过独立预期检查。未安装官方插件、Torch 或平台 API SDK；开发入口清除继承的宿主控制环境，Ctrl-C 后子进程退出。
- Python 3.10 独立安装 runtime、mask-utils 及其必要契约后，依赖检查、运行时响应和真实 mask 转多边形通过；不安装要求 Python 3.11 的作者 SDK 或推理契约。作者 SDK 的 35 项本地测试与两个 SDK GitHub Actions 运行通过。
- 官方仓库提交 `53717ebed7622e4fb12a6c94c64acacfef85c331` 已审核合入并公开。四个官方插件归入 `plugins/inference/`，LibreYOLO native 归入自身目录，通用实现及 mask 评估脚本已从官方仓库删除。五个 wheel 的匿名干净构建与已审核构建一致；[python-53717ebed762](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/python-53717ebed762) 同时发布四份从插件 wheel 提取的预训练目录，manifest SHA256 为 `82608ae226fbc84f436cdfe2e5c5877da1a759a29ef03e8a88981e618ce3ee10`。仅发布第一方源码、包与目录，不发布权重或第三方 wheelhouse；官方 CPU CI 通过。
- 平台六个 Python 消费者及共享镜像 requirements 已改用 SDK 的固定公开 URL／SHA256，lockfile 的包版本集合不变。官方三个后端 lock、OCR 独立构建输入和 LibreYOLO 候选构建器也已切换；历史 release 不覆盖，现有日常服务和保留镜像未替换。
- UI、训练器和数据导出的 SDK／官方实现尚未创建。后续按真实能力定义独立接口与依赖，不把推理抽象提升为所有插件的共同基类。

### D. 普通图像及多任务后端

共享接缝稳定后，Ultralytics YOLO、RapidOCR、ONNXTools 可分隔离工作树并行迁移，各自独立验收和发布；不要求互相等待同一个镜像版本。

- **YOLO**：完整多任务、多系列、开放词表、exemplar 与 tracker；受控检测／分割的原图 RLE、安装收据与已有评估语义不能丢失。
- **RapidOCR**：det／rec／e2e、方向与语言属性、合法组合、阈值隔离；核验所有实际业务 sessions 的 provider 与释放，不只看 GPU provider 列表。
- **ONNXTools**：用获准真实权重分别验收检测、crop 分类和 composite；核对属性枚举及全部业务 sessions。权重授权不明确时不得宣称该插件已可公开分发。

### E. 有状态交互与视频后端

优先用 Grounded-SAM2 固定交互与跨窗生命周期验收，再覆盖 SAM3 的 exemplar、multiplex 和 PVS 差异；实现可并行，验收顺序不构成所有代码必须串行的要求。

必须保留精确 Mask／RLE、低分辨率 logits、多候选、prompt revision、对象与源帧身份、正反向传播、session start／continue／close／lost，以及取消后真实执行和借用的结束。

这些后端目前是整窗 JSON 返回，vendor 内部 generator 不等于逐帧 HTTP 流。平台仍负责跨窗编排、进度、取消、迟到结果隔离和最终写入，不能把这些 owner 责任移给插件。

### F. 按插件完成平台切换与历史退役

平台 Compose 从本地 `build: ./apps` 切换为固定发布制品，原开发覆盖层只用于明确的插件开发联调。逐项更新原环境发现／`ML_MANAGED_BACKEND_URLS` 等配置，避免环境同步重新创建旧注册项或覆盖插件身份。

新安装获得新身份；项目明确启用与切换，受控模型重新试用、人工发布。旧的任务、作业、release、收据和冻结引用不重写。关闭新引用后，等待在途执行与视频会话按原规则完成或取消，取得全池排空／物理停止证据和零依赖证明后，才退役旧实例。

代码退役与历史数据保留分开；保留可审计记录和固定旧镜像用于已授权的历史执行／恢复，不留下无期限的兼容代码旁路。

## 7. 通用完成标准

- **独立性**：新仓干净检出，依赖有来源与锁定；运行不挂载平台源码，不读核心数据库，不持有整桶管理凭据或生命周期私钥。
- **能力不缩水**：迁移前后以真实请求核对完整目录、合法变体、提示、几何单位、属性、任务身份、错误和设备行为。百分比图像结果、归一化交互／视频结果与原图 RLE 不能混用。
- **真实模型**：多目标、空结果、边界输入和代表性用例使用真实权重与真实 HTTP；mock／静态 schema 检查不算模型或 GPU 资格。
- **GPU**：各自保持独立依赖栈；观察实际物理 GPU、provider、所有模型池和 sessions。沿现有标准验证两轮冷加载、取消／drain、卸载、基线和工作集回收；使用 Torch 的插件检查 allocated／reserved，ORT 还需证明实际 sessions 释放，不能仅检查 Torch 数值。
- **隔离**：只授予指定 GPU、资源额度、数据目录与网络范围；公开签名验证材料只读，敏感凭据私有；`allowed_asset_sources` 字段不等于已经落实网络层隔离。
- **平台治理**：off／observe／enforce 的准入、权限、停用、升级、未知响应恢复和退役均覆盖；安装不等于项目启用，更不等于模型发布。
- **有状态能力**：升级或停用不能绕过活跃 borrower、builder、embedding、tracker session；超时、取消确认或租约过期不等于计算已经停止。
- **交付**：每插件独立版本和镜像、兼容矩阵、模型资产来源与许可、开发说明、运维说明、实际验证记录；未取得资格的能力明确不可用，不伪造空成功。

GPU 验收按可用且已获授权的独占设备安排，不接管其他人的训练进程。CPU 检查可并行，多个工作树不能同时争用同一张验收卡后仍声称独占。

## 8. 发布与许可证门禁

目标源码仓库已按用户指示公开，必要第一方制品沿用本项目 MIT 许可；根许可文件和各分发包的许可元数据需随制品提供。第三方代码及模型仍逐项保留上游 LICENSE／NOTICE：不能把所有插件统一重新授权为 MIT，仓库公开和容器隔离均不消除各自的许可义务。

- Ultralytics 发布元数据声明 AGPL-3.0，并提供企业许可路径；迁移前单独审查适用条款及权重。
- SAM3 vendor 的实际 LICENSE 是 SAM License，不能依据相冲突的 MIT classifier 判断；SAM3 与 SAM3.1 权重存在分别授权的 gated 入口。
- Grounded-SAM2 vendor 带 Apache、BSD 等不同通知；代码许可不能替代权重许可。
- LibreYOLO、RapidOCR、ONNXTools 的代码许可与每个权重文件的许可分别核验；RapidOCR 部分权重已有特定声明，不能推广到全部模型。
- ONNXTools 两个权重的来源／授权仍是明确的分发前置；不把本机已有文件当成授权证明。
- 不提交模型大文件、用户训练 checkpoint、数据集、验收环境凭据或个人 HF Token。gated 资产采用管理员明确授权的准备步骤，之后按摘要使用。

来源：[Ultralytics 发布元数据](https://pypi.org/pypi/ultralytics/8.4.183/json)、[LibreYOLO 发布元数据](https://pypi.org/pypi/libreyolo/1.6.0/json)、[RapidOCR 模型许可](https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/)、[ONNXTools 固定来源许可证](https://github.com/yyq19990828/onnxtools/blob/102fcfeb08fc00f865c334a181bfd0bb518e5ed8/LICENSE)、[SAM3 vendor LICENSE](../../apps/sam3-backend/vendor/sam3/LICENSE)、[Grounded-SAM2 vendor](../../apps/grounded-sam2-backend/vendor/grounded-sam-2/LICENSE)。这些资料不构成尚未核验权重的分发授权。

## 9. 与现有后端重构的关系

无需先把旧后端重构的所有领域和部署维度全部做完，再开始推理插件迁移；也不能凭新理念把旧计划的未完成项标为完成。继续使用既有 Inference 契约、有限 Native 数据能力、Jobs、Assets、Learning 和 Labeling owner。

本计划接续 LibreYOLO 试点并覆盖模型服务拆仓，不退役 Identity／Workspace／Assets／Learning／Insights 等核心模块目标，不把训练执行器、存储 agent、前端 UI 或整个 `apps/inference` 搬进插件仓。跨域缺口只在确实阻塞相应验收时按原所有权解决，不增加模型名称专用调度或宽权限旁路。

### 并行工作流与共享门禁

后端 owner 解耦和推理插件化是两条并行交付线，共用一条便携契约前置线；不要按旧目录机械拆成互相改写相同接口的任务。后端完整目标与剩余事项继续以[核心模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)为唯一入口，本计划只列插件交付依赖和当前协调切片。

| 首批工作包                   | 独占修改范围                                                  | 依赖与合入门禁                                                                                                            |
| ---------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 便携生命周期契约             | 纯证明包、运行时生产者与 API／管理员 SDK 消费者、相关构建输入 | 单一契约负责人；保持原字段、阈值和错误语义，证明独立安装及宿主不加载模型运行时；先由主进程审核提交                        |
| Learning 反馈的 Audit 边界   | 反馈事实读取、既有回归场景和对应用户说明                      | 可与契约线独立推进；复用公开 Audit 关系和原事务，用无父表读取权限的测试角色及 HTTP 快照重放验收，不借机撤销全局 grant     |
| RapidOCR／ONNXTools 依赖闭合 | 两个后端各自的依赖声明、锁文件、Docker 构建输入和安装说明     | 先独立准备依赖图，接入已审核的契约提交后再做安装／构建验收；不并行改写 manifest、准入规则、qualification 脚本或根 Compose |

Orca run 为 `run_1e86c5259b79`，基线为 `1d6573b2a2e8c28b4e9c0c92d30456ac7d142da1`。三个执行者分别使用 `worktree-agent-plugin-contract`、`worktree-agent-learning-feedback` 和 `worktree-agent-onnx-packaging` 工作树；模型固定为 OMP `openai-codex/gpt-6.1-terra`，工作树本地 `defaultThinkingLevel: xhigh`，终端已核对实际模型与推理等级。该本地设置不提交到产品源码，也不修改全局配置。

执行者只提交所属切片，交接精确 commit、实际验证、失败和限制；主进程独立审核后以正常 Git merge 合入，再验证组合行为、维护正式台账。共享接口发生变化时只接入审核后的提交，不复制另一工作树的未提交文件。完成的执行者先释放终端，已合入且干净的工作树在保留证据后由 Orca 移除。

这一批交付的是阶段 B 的依赖边界准备和一个独立的 Learning 读取切片，不代表整个阶段 B 或 C 完成。第二批补足下述通用准入与真实 CPU／多池业务切片；可获取的版本化制品和干净跨仓构建仍是后续门禁，不得凭锁文件或导入检查替代。

### 第二批：通用准入与真实非检测／多池切片

Orca run 为 `run_ed041b0afdc5`，基线为 `8f9dd16f6bf55f7aff726450c3698127d2e749f7`。四个执行者仍使用 OMP `openai-codex/gpt-6.1-terra`、`xhigh` 和独立工作树；主进程负责共享合同审核、资源授权、正常合并与最终组合验收。

| 工作包               | 独占范围与交付                                                                                                              | 合入／验收门禁                                                                                                                |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 通用准入契约         | `worktree-agent-plugin-contract-b`：完整模型能力投影、CPU／CUDA、命名池、ORT session 证据、历史 inventory 及共享 owner 控制 | 实际 setup／新鲜 health／获准意图一致；未知、漏报和身份漂移拒绝；历史身份不靠默认值补齐；共享 SQL 使用前向迁移                |
| 安装器与 CPU 业务    | `worktree-agent-plugin-installer-b`：管理员 SDK／CLI、原生 HTTP 接入、RapidOCR CPU 工件与资产准备                           | 独立安装、CPU 无 GPU 依赖／设备、真实项目启用与持久化结果、正常停用／排空／退役；不以直连模型 HTTP 代替平台链路               |
| 生命周期与多池资格   | `worktree-agent-plugin-lifecycle-b`：RapidOCR 挂起根因、共享终止围栏、ONNXTools 三池／四 session                            | 获准真实权重与正例／空例／边界输入；独占 GPU、实际 provider／设备、两轮冷加载与释放；失败报告保留，不能调低阈值或漏报 session |
| Learning → Jobs 读取 | `worktree-agent-learning-jobs-b`：原计划指定的四处读取                                                                      | 复用既有 owner 与 caller UOW；受限角色、执行身份、越权、取消、回滚和重放；不撤销其他消费者仍需的私表权限                      |

CPU 与接口工作可以并行；同一卡的 operator 资格和平台托管推理必须串行交接。共享合同只消费主进程审核后的提交，不跨工作树复制未提交代码。实际平台调用暴露的健康元数据丢失和终止控制路径缺口属于这一切片的前置，沿原 Inference owner 修复；既有 Labeling 上传 ACL 问题不借机扩修，也不扩大运行账号权限。

本轮使用本机 OCI registry 和明确批准的内部验收权重，不构成公开分发授权。正式数据夹具若需隔离 operator 准备，须记录实际 Assets 字节校验与发布路径，并与普通上传 API 的失败分开；业务推理、结果读取和安装治理仍使用正常权限。实际拆仓继续留在阶段 C。

第二批已按上述门禁收口。主进程正常合并了通用契约 `05ec83f04`、原生生命周期 `deec0a4fe`、通用安装器 `dfb266020` 及 Learning 读取 `f9fbcf7f2`；冲突保留最新严格 GPU 绑定、真实 HTTP 健康投影回归及已获准的 CUDA 启动／报告校验，不恢复早期弱校验或无追踪的 GPU 探测。

主进程独立读取了五个已完成的平台业务作业、OCR／车辆持久化结果及三个已退役安装，并校验实际三池／四 session 资格报告和设备记录。合并后的 owner／契约定向检查 64 项、SDK／OpenAPI 检查 87 项通过，SDK wheel 构建及真实 CLI 入口通过；合并源码对实际平台的 SDK 读取与报告校验亦通过。新鲜的未启动取消安装由普通 Celery worker 自动收口，已启动实例在实际 Token 时限后回收，未直接修改账本或伪造健康证明。

按用户授权清理了两处无依赖的旧实例；保留的主、备实例经原匹配 SDK／宿主恢复，原镜像、配置、GPU 绑定和启用路由权重均已核对，模型缓存为冷启动。第二批证据位于 `.worktree/orca/run_ed041b0afdc5/`；仅内部验收权重与本机 OCI 制品，不推送远程、不发布版本。

### 第三批：候选制品闭环与样本评估读取

Orca run 为 `run_e7a85605ded7`，基线为 `cd345fdeaeeb7a917a9c97e5f466f4f0b197d4e4`。两个执行者使用 OMP `openai-codex/gpt-6.1-terra`、`xhigh` 和独立工作树，分别负责制品闭环与 Learning 评估分发；主进程审核提交并进行独立运行验证，不扩展到新后端迁移、GPU 验收或远程发布。

- 制品源码 HEAD 为 `35b5d1be0851e5aed0b36b42594b1567b5860a9a`；归档固定于 `f9b8e1388c6366534664f7b603f099375d06fd33`，该 HEAD 相对归档源码仅修订指南。79 个锁定 wheel 包含九个第一方 distribution，不包含 API SDK、平台源码或权重。两个独立构建得到相同归档 SHA256：`f7eb92e27185a2c5155a3362cdf756fc899ae03cb52123c268728dea225de7d3`。
- 已通过实际 HTTP 取回、解包前独立摘要核验、离线安装与 `pip check`；缺失 wheel、损坏 wheel 和不匹配平台均拒绝。纯契约导入不加载模型、Torch、ORM、Celery 或平台私有模块；规范资产清单仍是唯一维护来源。
- 主进程复核实际归档与第一方元数据，构建并离线安装生成的作者项目，实际启动其入口后执行未修改的 `aap-plugin check`。预先独立标定的双车／空图及认证、几何／类别／分数、参数和本地路径拒绝全部通过；不把仅运行内置插件入口算作作者项目验收。
- 固定官方 Python 基础镜像上的独立 OCI 构建通过，产物 ID 为 `sha256:debf087087b64cd9db94fb05da48b66bf73728dc2b9bc3016cbb344394c53c41`。初次 Debian HTTP 下载阻塞后，通过任务专用镜像代理取得仍受 Debian 签名／hash 校验的包；未改基础镜像、生成的 Dockerfile 或共享网络配置，代理已停止。pip 安装层禁网。主进程实际启动镜像并通过相同 HTTP 检查，确认 UID/GID 10001、无 GPU 请求及仅只读权重／Token 挂载。
- Learning 提交 `ddb77956e`、审核修正 `14f7b0cb7` 合入 `a634acb42`。主进程四项 PostgreSQL 检查及实际 `maintain`／`claim` 验证同事务绑定、提交后发布、提交失败回滚和重复领取围栏；其 broker／对象／旧传输为替代，不是模型推理或冷 worker 资格。详细边界见核心模块台账。
- 证据与候选包保留在 `.worktree/orca/run_e7a85605ded7/`。这是 CPU 执行而非 GPU-free 依赖栈；不声称 OCI digest 可复现、公开许可、正式渠道或实际拆仓完成。日常服务和保留实例未改变。

### 第四批：公开拆仓与固定提交 vendor

Orca run 为 `run_6118885dd686`。主进程先建立五个便携契约的可取回公开制品，再并行组织插件仓源码迁移、平台消费切换及 vendor 子模块化，执行者保持独立工作树。以下分别记录已审核合入的源码／制品切换和实际运行验收，不把公开发布等同于新镜像资格。

- 平台契约源码基线为 `0a21bc402432aa93a80fcb9f12d2f532e7109635`，公开发布为 [`contracts-0a21bc402432`](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/contracts-0a21bc402432)，manifest SHA256 为 `b38851f5e736a760021c226039562c2afc9efd21c8ab229f0bb403bab7eca292`。主进程已从真实公开 URL 在全新环境安装五个契约包，通过依赖检查和隔离导入，未加载模型或平台运行时。
- 插件包源码固定为 `d6de8e6078540993f349a2b41bcc2d381aeac319`，公开发布为 [`python-6118885dd686`](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/python-6118885dd686)，manifest SHA256 为 `81a8d94d7b0b360957e824f9dec0006ecb85d90006b0576be87af195aa89cd2a`。主进程已核验五个 wheel、MIT 通知、源码归档与逐文件摘要后发布；公开消费者和干净镜像按下文验收，平台重新采用不沿用旧报告。
- 原有 84 张真实 SAM mask 未公开；保留在受控验收目录，独立插件仓仅保留合成样本与固定协议金样本。真实输入的原评测由独立仓评测器读取，不能以删除数据为由缩减原验收范围。
- vendor 提交 `ce78bc777792ffd432ebc5353911aa1d3595c992` 与平台切换提交 `d993485aa` 已由主进程审核合入 `2cc4cd26cd0a543ca922937d32465c929ec41e1e`，工作树 vendor 初始化与 CPU 验证依赖修正继续合入 `0245c6aa920ab6e6611996af5414069b45b3cdaa`。平台旧 SDK、共享运行时、Mask 工具、原生适配器、LibreYOLO 与候选构建器已删除，消费者改用公开固定 wheel；独立插件仓文档已推进并推送至 `6bc47388d6f06107880b8e2c7ffd643f1052aa5f`，未改写已发布的 `d6de8e6` 制品。
- 主进程已在集成工作树从公开地址初始化三个子模块，并实际执行 TrackEval 完美跟踪／身份切换场景，使用 NumPy 2.4.4 得到预期指标。SAM3 补丁应用后三个运行文件与迁移前逐字一致，重复应用被拒绝；这不冒充 SAM3 或 Grounded-SAM-2 的 GPU 模型资格。
- 插件仓远端 [Python acceptance](https://github.com/YOAO-Labs/YOAO-Plugins/actions/runs/41006919648) 在 `e0e222f` 成功；平台集成与新镜像的本地运行资格单独记录。
- 匿名干净检出重建的五个 wheel 与公开制品逐字一致。全新 CPython 3.11 作者项目从公开 URL 锁定九个第一方包，实际构建并非 editable 安装作者 wheel；作者入口和新镜像均通过原双车／空图的八项 HTTP 检查，没有从待测响应重写期望。
- 新镜像 ID 为 `sha256:1b39f1357d20e4f6fa1d967d8ee446389e787fb2bb5bdcff58da13b781f64a4c`，仅推送本机 registry，固定引用为 `127.0.0.1:5050/aap-libreyolo-extraction@sha256:12db8062c12d10e77082e50d0db31aee7f66efb12f95bf823a072d60cd4291ce`。CPU 运行使用 UID/GID 10001、无 GPU 设备、只读权重与 Token；pip 安装层禁网，系统包层使用缓存。主进程实际运行并核对九个已安装 distribution 与生命周期文件摘要；这些检查不代替 GPU 资格。
- 平台执行者完成五个模型后端、SDK、运维与工作树的定向检查，实际构建 API／RapidOCR CPU 镜像。API 镜像离线导入 778 个路由与 TrackEval，没有模型运行时／Torch；RapidOCR 镜像实际健康检查和私有 SAM RLE 往返通过。其余 GPU Dockerfile 仅做构建静态检查，不宣称各模型均已重建或重新取得资格。主进程另执行集成源码的 13 项 runtime 检查与实际 API vendor bootstrap。
- 公开消费证据已归档到 `.worktree/orca/run_6118885dd686/public-acceptance-evidence.tar.gz`，SHA256 为 `ce6dfa9efdc439afe2a09ab0f41a949f87ad954ba6915e4d8fb93bfbb6117fe2`；其他父进程运行收据保存在同一目录。84 张真实 SAM mask 的私有评测覆盖 106 行，不将这些输入或内部 OCI 推送到公开发布。
- 新镜像的 GPU 资格报告 SHA256 为 `f386fda9122950cb2147b42d9d63f146cbb4d4b1645307aa6834cdd259d51f3b`，绑定注册项 `fd691da5-0dae-4c9a-8685-5b57219a5ee8` 和实际 RTX 3090。两轮原生 CUDA 装卸载、取消计账、忙碌卸载拒绝、旧代际／重放／不完整控制头拒绝等九项门禁通过；卸载后 Torch allocated／reserved 归零，原生 wrapper 与各类借用归零。宿主实际监测容器 GPU 进程和无外来计算，主进程在同一不可变镜像内用真实设备事实复验报告。报告生成使用进程内 ASGI，不冒充网络 HTTP；后续平台业务另经实际 HTTP 验收。
- 真实训练 run `f1d246b6-f24d-4a4c-a8c3-6fbf873f9eef` 完成三轮训练，产出 36,698,917 字节的原生 `best.pt`，SHA256 为 `c88b896965fc84925c0d1d9897f56695aa1eb82ef8d49efb7b62e3d79768a410`。训练继续使用已审核、不依赖抽离运行时的既有 executor 镜像；受控加载与推理使用本批新镜像。模型版本 `304eae7a-0deb-4002-bfa6-118e11da95cb` 经实际 GPU 试用和人工 release `336f432d-51c2-4236-9ac5-5a4fc4075955` 发布。
- 当前任务作业 `a64fd8a2-ae41-4702-9814-7f5de576cef2` 完成 1/1，批量作业 `d04eb413-4daf-46a4-8d30-39a7260688c2` 完成 2/2，无失败、跳过或仲裁失败。主进程独立读取三个持久化 prediction、作业冻结引用及 release，确认来源为真实执行、固定 checkpoint 与新注册项。阈值为 0.01，空图实际保留 7 个误检，未篡改结果或据此宣称训练精度；独立 stock 权重的双车／空图协议检查另行通过。
- 显式追加取消选用的第二代 release 后，受控部署按 `ready → draining → retired` 退役，安装经正式 SDK 正常卸载、容器删除；等待真实 Token 时限后，collector 的成员、提交、租约与队列均为零，GPU 无遗留计算。旧 release、三份预测和冻结作业引用未改写。原 CPU 模型／退役收据及裁剪作业 `3268581c-e3a1-4e52-a331-a22770abe88b` 的 `device=cpu` 冻结引用保持不变。
- 原保留推理服务已恢复，主／备实例的镜像、配置、设备、owner 与启用权重 1:100 不变，健康检查显示冷缓存。原 operator JWT 已自然过期，注册／路由与 CPU 历史比对使用只读数据库事务，不重置凭据；认证后端健康检查另行通过。首次直接发信号停止旧服务曾触发托管进程重启，执行者在任何新 GPU 计算前中止；主进程正式停止托管服务后才重新授予独占窗口，失败记录保留。
- 本批只证明单卡串行排他和该业务闭环，不转移旧镜像的完整路由／并发矩阵资格。既有普通上传 `display_seq_tasks` ACL 失败未扩修，夹具由获准 Assets owner 路径准备，业务仍走正常 API／worker。首次试用的 drained 成员与签名存储 origin 错配分别通过正常 resume 和任务专用 `ML_BACKEND_STORAGE_HOST` 修正，未放宽准入、资产来源或账本约束。
- 业务证据归档为 `.worktree/orca/run_6118885dd686/business-acceptance-evidence.tar.gz`，SHA256 为 `016d7bfdac4ad83aeb691cdb59ec23b9e5d30aec0c4ac834f2d3ddafd9de4c86`；主进程复核、原环境恢复及资格生成器归档收据在同目录。训练 checkpoint 已从实际对象存储取回并独立校验后私有保存。两个任务自有测试命名空间、验收容器、临时凭据／构建目录及三个已合并工作树已清理；原日常／手动验收环境和内部不可变镜像保留。

| 子模块                                             | 公开来源                       | 固定提交                                   |
| -------------------------------------------------- | ------------------------------ | ------------------------------------------ |
| `apps/sam3-backend/vendor/sam3`                    | `facebookresearch/sam3`        | `4cbac146c1b5a1e3a7f5c6a894901090b4dfd65b` |
| `apps/grounded-sam2-backend/vendor/grounded-sam-2` | `IDEA-Research/Grounded-SAM-2` | `b7a9c29f196edff0eb54dbe14588d7ae5e3dde28` |
| `apps/api/app/vendor/TrackEval`                    | `YOAO-Labs/TrackEval`          | `8335d7a840a02d20580a72c2f0e71f003dc99561` |

TrackEval 使用保留原裁剪适配层的公开 fork，不把平台调用切换为未经验证的上游完整评测器。三个 gitlink 均固定完整 commit；tag 只辅助选择，不追踪浮动分支。

### 第五批：阶段 D 源码切换与新镜像资格（进行中）

- 平台已合入 `4b32ceaac2cf6fac07c4b4cd55a19c6a82fef064`：删除三个已迁出后端的旧源码，Compose 使用显式启用的固定摘要镜像，宿主维护程序按服务选择拉取／核验制品或保留 SAM 的本地构建。安装包探针、训练资格工具、生命周期夹具和 CI 消费者使用独立制品，不保留第二套活跃实现。
- `c200b1f764419bdd1683d64a556a727176a54917` 修复原生 checkpoint 兼容契约：必填 `format` 可显式为 `null`，但不是通配符。后续审批、同成员发送重验及固定 Tracker 选择集合的源提交 `906628f2619be01ec8dd96f03385218e3626b64c`，连同重启及迁移运维文档 `a98d24aa1bfb3afbe5bd3af6ff13fcd36f8205be`，已审核并合入主工作树。五个便携契约发布为 [contracts-906628f2619b](https://github.com/YOAO-Labs/Plugin-SDK/releases/tag/contracts-906628f2619b)，manifest SHA256 为 `5c26a3829073324bf6f9cc29521457d296920424e5b1b3c34d672408aa956663`；匿名取回、逐文件核验、干净安装、实际官方选择描述及纯批准／撤销决策已通过，旧发布不覆盖。
- Plugin-SDK 的原生 Torch 生命周期修正发布于 [python-sdk-6b0f437333b3](https://github.com/YOAO-Labs/Plugin-SDK/releases/tag/python-sdk-6b0f437333b3)，runtime wheel SHA256 为 `863e633ab8a55d1ff29936d379e5eeaaa68fbe59b6f0489ea71dcbdb8b042e03`：不再把空 ORT session 计数附加到原生 Torch 报告，ORT 的实际计数与 provider 门禁保留。作者下载器进一步固定上述新契约，源码 `e5f38f48ba37cc18c17dc9c2750962122a2e6228` 的实际匿名 CLI 取回与[远端 CI](https://github.com/YOAO-Labs/Plugin-SDK/actions/runs/41124876743)通过；未因此替换已获资格镜像的运行依赖。
- 新鲜验收工作树为 `plugin-acceptance-08`，使用专用 PostgreSQL `33483`、Redis `33484`、数据库 `aap_wt_cbd41baab38eaa57_test`、独立 Registry／Inference／probe、唯一普通／推理／控制 worker 及 beat。没有在共享 PostgreSQL 创建角色，没有更改主 `.env`、规范工作树的 Python 环境或原保留服务。夹具经既有 Workspace／Assets／Labeling owner 准备，实际原件下载与大小、SHA256 核验通过；原上传 ACL 缺陷未扩修。
- ONNXTools 使用 `a054091cfd3377991e14c2139c86434ae60b2675` 的本机不可变制品 `127.0.0.1:5050/aap/onnxtools@sha256:5919ffba2162313863ef76ac8e7b917e646ac3ff85a68f0d2c9e1709de03028b`。新身份 `2aa2b4ea-b822-468f-bea0-48e9f2508cb3` 的未修改原始报告 SHA256 为 `3f2f673faee77e6bc1ac697acd7c723e275742f5214017c182ec518d3eff6aca`；两轮各完整观察四个 CUDA-primary sessions，回收分别为 97.93%／95.99%，释放后池及活动计数清零。正常 SDK 使用该身份绑定报告完成激活，不沿用旧注册项报告。
- RapidOCR 使用 `d0798c3068ab9ced0aedb5b6c5da41e14facf87f` 的制品 `127.0.0.1:5050/aap/rapidocr@sha256:70b379eaf2227beb708221c630f9c6836b52c00baa7430daee75694b434f19a0`。新身份 `9115dd3d-0398-4671-a500-6ea1a4c15166` 的原始报告 SHA256 为 `4127c1c2bde870a913094152a2c46ca01ba3b01fb17e28ca659d1cd08d412c7c`；两轮各完整观察九个 CUDA-primary sessions，正常 SDK 激活通过。八份已批准输入重新核验；只复制资产清单与权重，没有复制旧 Token、收据、身份或签名私钥。
- YOLO 当前运行制品由 `e6bef4d40725edadefe37c8c1bd9a15c454615e8` 构建，镜像 ID 为 `sha256:13d691f717343a8fa9d85c5015e392e44594caace55fb153ee176718440344a0`，OCI 为 `127.0.0.1:5050/aap/yolo@sha256:44c188e520a2c7c29c8f9895770c912bac940a04f43b34111cab5d9e04c902a7`。精确镜像无源码挂载的 CPU 检查确认九模型资源均为 CPU、规格显存为零、无 GPU 请求及 CUDA 初始化，实际安装的 runtime 与五个契约文件匹配公开 wheel。新身份 `2d90cb37-1a4f-488e-a9dd-60b36f990b9b` 的完整 CUDA 资格已通过；检测／分割原始报告 SHA256 分别为 `afb1797b3c4d1437ec3686ee6b210826e5b8253e05ce36bab27a584f9e21ab98`／`b37ce9d37a7d5e5b7cdbe8ba406eb6a9120ef2ecdc73057c0c957e0152df4ec6`。两份报告均覆盖两轮全部工作负载、激活身份及故障门禁，回收比例均为 98.31%／98.36%；编码器实际位于 CUDA，两个 tracker 各处理六帧并产生轨迹，最终 builders／borrowers／请求清零且完全卸载。主进程核验原始报告并保留私有归档后释放执行者，实际 GPU1 无计算进程、占用 18 MiB；未据此宣称平台激活或业务完成。
- 真实 CPU 安装 `0c256b44-5970-4d30-8661-44390343dd29` 正常激活；管理员 SDK 从该容器实测 `yolo11s.pt`，仅批准 `detect/series=yolo11/size=s`。作业 `21f0a0f9-33f2-48c2-a4af-f297ff864ce6` 完成并持久化预测，GPU 提交内存不变。未批准具体／默认规格、撤销与不可用分别返回 409，插件 `/predict` 计数不增加；非管理员审批 403、旧 CAS 409。该 CPU 安装已正常退役至 revision 6，精确容器已移除。批准与可用性、安装、GPU 资格、项目权限互不替代；无项目受控发布时的 409 仅为负例，不代替真实训练来源的正向验收。
- 官方部署描述工具覆盖四个插件，绑定固定制品、原安装清单、完整选择和预期文件身份；自身规范摘要不改写安装清单。管理员 SDK 通过容器内固定探针、文件描述符与前后身份检查测量实际挂载内容；真实 Docker 的宿主同名目录替换回归证明不会核验错误目录。纯契约不拥有权重清单，用户训练 checkpoint 仍沿 Learning／Assets 路径。
- 源码测试修正 `64f7233dc775efb0fff3ac51bc6fde27cc3eb33a` 删除三处硬编码 GPU 的旧断言，显式覆盖配置 CPU、未探测 CUDA 和运行时已落 CPU 的状态；完整 CPU 检查 423 项通过、两项跳过，选择描述六项通过。先前本地三项失败及远端失败运行保留，未成功取回的远端失败日志不归因为本地断言。官方当前 `a18a2d32e682729e9beb47d84139fb6f2b07b6c4` 只进一步澄清依赖文档，[远端 CI](https://github.com/YOAO-Labs/YOAO-Plugins/actions/runs/41124881378)通过；运行镜像仍固定上述 `e6bef`，不把文档提交当作新镜像资格。
- 官方第一方制品已从 `a18a2d32e682729e9beb47d84139fb6f2b07b6c4` 构建并发布为 [python-a18a2d32e682](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/python-a18a2d32e682) 预发布；manifest SHA256 为 `1ddf5d4e1b5a9d734a09b751a419fd0273cf20972052890953de20686242caae`。五个 wheel、五个 sdist、四份插件预训练目录及源码归档均完成摘要、大小和第一方许可检查，17 个公开附件已匿名取回核验；四个隔离环境分别以 `--no-deps` 安装对应公开 wheel，实际读取／生成的清单与发布清单一致，未导入模型运行时或平台应用。它不是完整依赖安装或模型推理证明，不包含权重、第三方 wheelhouse、OCI 镜像或新的 GPU 资格。
- 发送重验修复后，同一新账本 incarnation 中，ONNX→OCR 与 OCR→ONNX 两次驱逐后的首次请求均正常完成，提交内存分别切换至 16384／8192 MiB，没有重试、重选、清账本或扩大容量。作业 `14a4f95d-2628-4879-ab31-41167de71e5a` 的原请求未限制类别，其四个裁剪分类结果按原样保留；另一个显式 `parent_class_filter=["car"]` 的作业 `ba0aa468-8dad-433c-8862-267314795b65` 仅为两个 `car` 框写入属性，五个非车辆框保持空属性。二者分别证明首次请求与类别隔离，不能混用。
- 批准集合随有界规范命令冻结，健康刷新后原子重验同一成员、进程、配置、项目授权和池代际；混合池保留合格成员间的既有权重，固定 tracker scope 不重新选号。撤销后的发送前拒绝明确为 `not_sent`。迁移前需先排空／明确退役旧 scope、停止旧执行者；旧记录不补造身份。实际受限角色与 owner HTTP 的检查证据独立保存，已被执行者删除的原 JSON／JUnit 只保留可取回的实际工具输出，不冒称原文件仍在。
- 主进程通过真实登录、项目选择和规格控件验证 OCR 已批准 `v5/mobile/universal` 与未批准 `v5/server/universal`，以及 YOLO 已批准 `s` 与默认未批准 `n`。明暗主题、桌面和窄屏截图均实际检查，无页面／详情横向溢出或观察到的 JavaScript 错误；认证 heartbeat 的一次 HTTP 409 单独保留。任务浏览器、临时凭据配置与 Vite 已关闭，未改用户主题或日常环境。
- ONNX 正常重启经历 180 秒命令期限，但原激活在约 266 秒正常提交；既有中断重启恢复分支复用新容器并保存旧排空证明，没有再次替换容器。新容器资产重测及正常成员 resume 后，冷作业 `412cfc0a-7d5c-462a-aa96-eb2a76ea2e8c` 完成，安装正常退役至 revision 12。最初因成员仍 draining 而拒绝的冷请求保留，不冒充首次成功。
- OCR 正常重启在约 275 秒完成，旧容器排空证明保留；新容器资产重测、正常成员 resume 和 owner 证明恢复后，冷作业 `716a5bbe-d988-497e-9638-61d29554e6a8` 完成，安装正常退役至 revision 12。真实 Token 时限后的 collector 已证明 ONNX／OCR 两个成员全部回收；实际快照中成员、分配、提交内存、租约、队列及转换均归零，持久域一致。零成员域依规则为 `not_ready/proof_incomplete`，并非残留资源；ready 要求至少一个 active 成员，退役收口不伪造 ready，也不清账本。
- 早期 `platform-cutover-07` 的资格报告、激活／卸载拒绝与未知 `3/32768` 分配历史保留。主进程隔离失败原生容器网络，停止该栈全部发行者／调度者，等待最后发行者退出后的 332 秒，再核对实际空闲并移除精确任务容器；旧数据库与 Redis 未清空，宿主清退不冒充正常 `safe_to_stop/retired` 收据，也没有重新启动旧权威。
- 受控模型正向验收要求同项目真实成功的 TrainingAttempt、唯一已核验权重及最终指标；无发布时的拒绝和资格夹具中的预训练副本均不能替代。最小真实训练使用任务自有、经管理者确认的预测辅助标注，不宣称人工审核真值。既有执行器只支持 CUDA，且训练租约不等于推理仲裁；因此必须在原生安装仍 prepared／未启动时独占 GPU1 训练，确认进程退出及训练租约闭合后才激活推理。新训练节点、Token、缓存、目录和存储已独立准备，实际目录只暴露本次批准的 `ultralytics/yolo11s`，未使用继承的日常训练配置。未启动执行器时，训练运行预检依规则拒绝尚无实时核验租约的请求，不插入或伪造租约。
- `a8ad8354a` 修复原件授权的 Identity 私表读取，97 项现有原件检查及真实受限角色的准入／缺失／停用回归通过；`6fd4849d8` 进一步将训练快照接到既有 Workspace／Labeling／Identity owner，保留同一 PostgreSQL 语句的类别、任务、媒体引用和标注快照。实际受限角色的普通与数据集／批次／媒体过滤预检均返回 ready，正常导出数据版本 `fe3788b8-fa55-4e3f-81a9-1486f80e0cf1`，训练／验证各一张图片；真实 ZIP 摘要为 `82937d101757992666361fb4e7b99ecb4c03aba3458d0f6a0bfca6053394be71`，内部图片、标签与清单一致。两种真实行锁争用及准备后的标注变更检查通过，未重读媒体；相关回归已进入显式模块边界检查列表。宽范围检查仍有旧 mock、存储绑定和克隆 schema 夹具失败，未扩修或宣称全量通过。两张相关视角图片只用于链路冒烟，不证明模型精度。
- 原生安装的正常 prepare 会创建 pending 成员；周期重建将其声明的 8192 MiB 保守记为 unknown，不能把先前零成员快照当作当前状态。该安装从未启动，代际高水位均为零，没有 Token 时限或控制意图。训练前仅停止任务自有推理签发者及 ML／control／beat，确认进程退出并等待完整 305 秒保守时限；API／普通 worker 继续提供训练能力。账本及 prepared 身份保持不变，这是操作层面的独占隔离，不是清账本、正常退役或跨 owner 的共享仲裁。
- 原训练镜像缺少输入回执策略及实际回执发布能力，正常领取规则拒绝作业；旧运行 `8e52b0ff-a31d-4525-b2e1-5d7bc5a66da1` 及唯一未领取 attempt 经正常 API 取消，原镜像绑定保留，未增加兼容白名单或伪造能力。从已提交 `e07f1f9e2b5ce8da2dda0367511bb75cc545ecc1` 使用既有固定 Dockerfile／依赖构建执行器，OCI 为 `sha256:adbdf04db91cef16b16ec9d06609e6d5e6f7d3a2598754ae9f8ac38de8fd0ede`，实际无 GPU、无网络容器验证了输入回执策略、源码摘要和运行时版本。任务自有 bind 目录的 UID 不匹配启动失败保留；容器与宿主分别使用相同 Token 字节、各自 owner 的 `0600` 文件，不扩大容器能力。
- 新运行 `ba3f6259-39f3-46e4-a678-877a45e31433`、attempt `2a2f9fd3-7793-4707-9b15-04683ee4097e` 通过正常执行器完成真实一轮训练。实际存储中的 `best.pt` SHA256 为 `d1ab1f6b1dcffa517c70c381e95b995de16f6cb92048cbc6a869da90d4d9a758`，最终指标与 manifest 也完成摘要核验；输入回执按真实 run／config／attempt 验证通过，train／val 各一个成员。正常 API 注册模型 `8c8751ff-8ecf-462c-b83a-a896a6ed0a10`，仍归 Learning／Assets，不进入插件预训练目录，也不自动发布。执行器正常退出，训练租约无所属 attempt，GPU1 无计算进程；之后才单独授予原生插件的平台验收窗口。相关视角样本和内部验收批准不构成独立精度评估或公开权重分发许可。
- `1ec4f4891` 修复 GPU owner 把完整密封端点捕获响应误套进 64 KiB 小消息上限的问题。正常激活失败后，真实受限角色复现捕获了 95,154 字节的响应；修复复用已发布的 24 MiB subject 契约，并单独保留有界信封开销，策略、权限及终止请求的原小消息限制不变。同一角色随后正常打开原安装的完整快照，检测资格报告摘要仍为 `afb1797b3c4d1437ec3686ee6b210826e5b8253e05ce36bab27a584f9e21ab98`。三项定向端点检查与单独隔离 PostgreSQL 中的真实 70,000 字节元数据回归通过，测试资源正常清理；首次缺少 Node `pg` 依赖的预检失败保留，后续仅在任务私有目录按既有锁文件离线准备依赖。没有裁剪报告、替换镜像、清账本或修改验收环境权限。该读取证明不替代后续正常激活及平台业务验收。
- 私有证据位于 `.worktree/orca/run_74dff0b9bbeb/stage-d-07/`、`plugin-acceptance-08/.worktree/acceptance08/` 及原生资格工作树。八份 YOLO 权重及开放词表编码器仅获准内部验收，六帧合成运动片段不冒充自然视频，预训练副本不冒充实际训练 checkpoint。阶段 D 仍待精确 YOLO 镜像的平台业务、真实训练来源的受控发布、安全退役及原保留 LibreYOLO 主／备恢复；恢复义务单独记录，未提前标记完成。

## Outcome

- 本轮已完成六个迁移单元、真实共享依赖、检测专用假设、有状态边界与许可证前置的盘点，形成上述顺序与验收门。
- 目标 `YOAO-Labs/YOAO-Plugins` 最初按私有仓库创建；用户现已设为公开，并授权必要第一方制品使用既有 MIT 许可。第三方和权重仍按各自门禁处理。
- 统一契约／分运行环境 SDK／前端信任分层为采用的目标方向；本期只推进推理与 Python 开发体验，不宣称通用插件框架或前端 SDK 已实现。
- 正式架构入口：`docs-site/dev/concepts/module-deployment.md`。已实现的依赖边界影响记入 `CHANGELOG.md` 的 Unreleased；不据此指定发布版本或日期。
- 实施与验收：LibreYOLO 原限定单卡资格，以及第二批 RapidOCR CPU、ONNXTools 多池 CUDA 的限定平台业务与正常退役已通过，细节与边界见[正式架构台账](../../docs-site/dev/concepts/module-deployment.md#inference-plugin-admission)。首次公开拆仓的源码与制品消费切换已合入；匿名干净构建、新镜像独立 GPU 资格及平台训练回流闭环按第四批完成，其余后端完整迁移不在本批范围。
- 第三批私有候选与生成项目／镜像 CPU 检查的历史证据保留，Learning 样本评估分发已合入。第四批完成阶段 C 的固定公开制品、唯一源码归属和限定运行验收；后续按阶段 D 分后端迁移，分别安排许可、真实模型与治理验收，不将本批通过泛化为其他后端完成。
- 本地独立插件仓库位于 `plugins/YOAO-plugins/`，平台根 `.gitignore` 忽略整个嵌套仓库；源码由独立仓库提交，不作为平台子模块。原有第三方 vendor 已改为上表所列固定提交子模块，旧复制脚本已移除，许可和必要补丁保留。
- API SDK 与插件 SDK 保持分包、独立版本及共享必要契约。生命周期纯校验的实现与消费者已在契约提交 `8ddcd65be` 中收口并合入；正式规则见 `docs-site/dev/concepts/module-deployment.md#api-sdk-and-plugin-sdk`。
- 并行的 Learning 反馈读取提交 `47b5eec70` 已审核合入；其无 Audit 父表读取权限和 HTTP 快照重放证据记在核心模块台账，不计作插件迁移完成。
- RapidOCR／ONNXTools 依赖准备提交 `f1cf6e37b` 已审核合入：真实运行依赖、共享契约输入、锁文件与 hash-locked 导出已对齐，ONNXTools 上游 Git 身份和 RapidOCR cuDNN 修复保持。两套独立 wheel 的真实 HTTP 启动通过；主进程另构建了两个镜像，在禁网、只读、无 GPU 的容器内验证 `/health`、认证 `/setup`、未认证拒绝、唯一 headless OpenCV 和 PNG 编解码。模型池保持 unloaded，不计作模型推理或 GPU 验收。
- 首批主进程验证生命周期证据 11 项、管理员 SDK 43 项、隔离 PostgreSQL 反馈两案及 API 准入一案；当时的 API 镜像在未安装模型运行时和 Torch 的情况下接受合法证明、拒绝四类损坏证明。首批 ONNXTools 定向 CPU 检查为 25 项通过；第二批集成与实际业务证据见上文，重叠检查不累加成独立案例数。
- 首批 RapidOCR 检查失败并超时的记录保留。第二批已定位为测试等待最终 HTTP 响应与真实 builder 释放的循环等待，以及合成 Request 缺少 `.app` 导致 admitted work 泄漏；按真实并发顺序和 Request 形状修复夹具，未放宽活动请求或超时不变量。执行者全套源代码检查与真实 HTTP 围栏验证通过，平台 CPU 业务另行验收。两后端 stock metadata 的 `pip check` 仍按 README 报告有意替换／由基础镜像提供的分发包缺失，不宣称普通 pip／uv 安装全绿。
- 文档全站构建被已确认与首批基线字节相同的 `docs-site/dev/reference/env-vars.md` Vue 标签错误阻断，未扩修无关文件。首批页面校验证据保存在 `.worktree/orca/run_1e86c5259b79/`，第二批七份受影响文档的 Markdown／Vue 编译亦通过；不把已知全站失败记作通过。既有 Labeling 上传 ACL 问题保留为独立边界，不以扩大权限完成插件验收。
- 前端 API 类型已从当前 snapshot 重新生成。`tsc --noEmit` 仍在 `ProjectModelsPanel.test.tsx` 报告五处 `status` 字符串宽化错误；该测试文件及 `ModelDeploymentOut` schema 与本批基线相同，未扩修或宣称全量类型检查通过。
