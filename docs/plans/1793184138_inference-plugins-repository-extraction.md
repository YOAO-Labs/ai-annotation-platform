# 推理后端插件化与 YOAO-Plugins 独立仓库规划

> 状态：规划完成，尚未实施多后端迁移。LibreYOLO 试点已按原计划完成限定单卡验收；不因首个插件通过而宣称其他后端、通用装配或独立仓库分发完成。
>
> 日期：2026-10-28。目标仓库：[YOAO-Labs/YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins)，本轮已创建并核验为 **Private**；未推送当前工作区代码。
>
> 授权范围：规划全部现有推理后端的插件化、创建目标仓库。公开仓库、推送代码、迁移其他后端或实现前端 SDK 不属于本轮已完成事项。不指定发布版本或发布日期。
>
> 前置资料：[能力边界与生态研究](../research/51-plugin-capability-boundaries-and-ecosystem.md)、[LibreYOLO 试点](1793156248_libreyolo-inference-plugin.md)、[核心模块部署与边界台账](../../docs-site/dev/concepts/module-deployment.md)。

## 1. 决策

采用 **统一插件契约，按运行环境提供 SDK**。统一的是可验证的行为边界，不是统一语言、模型基类、依赖环境或部署进程。

| 层次                | 职责                                                                                                 | 明确不负责                                                                            |
| ------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 统一插件契约        | manifest、身份、能力声明、版本兼容、权限、配置、安装与生命周期；需要时声明后端与前端组件及其兼容关系 | 不能凭声明获得权限、GPU 资格或替平台决定业务终态                                      |
| Python SDK          | 降低服务端插件开发成本；模型加载、执行、释放和 HTTP 协议适配，复用共享运行时                         | 不要求插件导入 API、ORM、Celery 或核心私有实现；不把所有算法塞进检测接口              |
| TypeScript 前端 SDK | 有实际需求时提供页面、侧栏、工具栏等扩展入口；受控访问当前项目、任务和平台操作                       | 本期不实现，不提供主应用任意代码执行、登录凭据或私有 store 访问                       |
| 平台宿主            | 权限校验、扩展入口注册、资源管理、隔离、审计、安装治理                                               | 不替代 Identity、Assets、Learning、Jobs、Labeling 等领域 owner 的业务规则与最终提交权 |

后端以既有 HTTP 契约为当前边界；需要事件交互时沿公开、类型化且受授权的事件契约扩展，不提前增加通用事件总线。非 Python 实现可直接实现协议，SDK 只是便利工具。

一个完整插件将来可以同时包含后端镜像和前端界面，由同一份 manifest 描述组件及兼容关系。当前只实现推理能力配置，不为了可能的 UI 提前增加空组件、万能 hook 或通用插件框架。

### 前端信任边界

- 第三方可安装前端优先使用**独立来源的受限 iframe**及明确的消息接口，不直接动态导入任意 React 代码到主应用。
- 信任等级由宿主依据来源、审查与管理员授权决定，不能由 manifest 自报 `trusted` 即生效。
- 消息接口需校验 origin、source、消息 schema、请求身份和操作权限；不转发原始登录 Token，不提供任意平台请求代理。
- 插件获取的是获准上下文和平台操作。写入时仍由相应领域重新校验权限、任务版本和执行状态，不能直接操作标注状态或绕过 Workbench 写入约束。
- 内置、受审查扩展可以采用宿主组件接口。两种方式复用能力契约，但不共享相同隔离策略。
- 只有出现具体的前后端联合插件用例，才定义所需扩展槽位、消息协议、联合升级／回退和 TypeScript SDK。本轮推理迁移优先使用现有参数表单与结果界面。

## 2. 迁移范围与事实基线

现有五个后端加上独立 LibreYOLO，共六个迁移单元。以现有后端的运行依赖和生命周期为单位，不按每个模型规格建仓或建插件。

| 插件单元         | 当前代码入口                                         | 必须保留的实际能力                                                                                             | 主要特殊性                                                             |
| ---------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| LibreYOLO        | `plugins/libreyolo`；`apps/_shared/libreyolo_native` | 原生 YOLOv9 t/s/m/c 检测、受控训练权重安装与试用；显式 CPU 和经验证的 CUDA 路径                                | 首个试点；不能再走 Ultralytics loader，也不能改写历史 CPU 产物身份     |
| Ultralytics YOLO | `apps/yolo-backend`                                  | 检测、分割、姿态、旋转框；YOLO-World／YOLOE 文本与 exemplar；ByteTrack／BoT-SORT；已有受控检测／分割与评估资格 | 不能把“YOLO 插件”缩成 bbox 检测，也不能因 LibreYOLO 拆出而删除这些能力 |
| RapidOCR         | `apps/rapidocr-backend`                              | `ocr-det`、`ocr-rec`、`ocr-e2e`；文本、方向、语言属性与合法模型组合                                            | ONNX Runtime；动态引擎池及每个引擎实际 det／cls／rec sessions          |
| ONNXTools        | `apps/onnxtools-backend`                             | 车辆检测、车型／颜色分类原子，以及检测后分类 composite                                                         | 三句柄、最多四个业务 sessions；两个模型文件来源与授权仍需核验          |
| Grounded-SAM2    | `apps/grounded-sam2-backend`                         | 文本检测／分割、点／框／Mask／涂抹、多候选、框批量分割、SAM2 视频追踪                                          | image／video 双池、embedding、跨窗 session、对象身份与精确 Mask        |
| SAM3             | `apps/sam3-backend`                                  | 文本检测／分割、交互提示、exemplar；SAM3.1 multiplex 与 SAM3 PVS 两类视频能力                                  | GPU-only，image／multiplex／PVS 三池；独立权重授权与有状态追踪         |

这张表基于当前实现调查，不代表六个插件均已独立发布或取得 GPU 资格。SAM vendor 的训练代码或训练 extra 不等于平台已支持 SAM 训练回流。

主要源码依据：

- [YOLO 模型矩阵](../../apps/yolo-backend/model_registry.py)、[YOLO HTTP 服务](../../apps/yolo-backend/main.py)、[受控分割](../../apps/yolo-backend/managed_segmentation.py)。
- [RapidOCR 目录](../../apps/rapidocr-backend/catalog.py)、[引擎池](../../apps/rapidocr-backend/engine_pool.py)。
- [ONNXTools 服务](../../apps/onnxtools-backend/main.py)、[属性 schema](../../apps/onnxtools-backend/attribute_schema.py)、[句柄池](../../apps/onnxtools-backend/handle_pool.py)。
- [Grounded-SAM2 服务](../../apps/grounded-sam2-backend/main.py)、[视频执行](../../apps/grounded-sam2-backend/video_predictor.py)。
- [SAM3 服务](../../apps/sam3-backend/main.py)、[multiplex](../../apps/sam3-backend/video_predictor.py)、[PVS](../../apps/sam3-backend/pvs_video_predictor.py)。

### LibreYOLO 当前进度不能扩大解释

LibreYOLO 已交付独立 SDK、插件及安装治理，在自有隔离环境完成真实 GPU 训练与原生 checkpoint 回流、受控安装试用及人工发布、当前任务／批量／真实裁剪、CPU 历史冻结与退役，以及三种路由模式的在途排空、升级失败恢复和受控重启。原生 close 两轮使 Torch allocated／reserved 回到零，旧 YOLO 中的 LibreYOLO 适配分支已删除；证据及已知范围限制见[试点 Outcome](1793156248_libreyolo-inference-plugin.md#outcome)。

**尚未完成**：其余五个后端的插件化、全扩展点统一装配、实际拆仓与跨仓发布。单卡通过不证明双卡隔离、模型精度或生产容量；代码、依赖及权重的许可前置也不会因本机可运行而消失。

## 3. 仓库边界

采用一个 `YOAO-Plugins` 源码仓库，多个独立发布的插件和 SDK 包。插件版本、锁文件、基础镜像和 CUDA／ORT／Torch 环境分别维护，不要求六个插件共同升级。

| 内容                                                                                      | 唯一源码归属                       | 跨仓使用方式                                                                   |
| ----------------------------------------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------ |
| 核心 Inference owner、Registry、路由、GPU 仲裁、权限、Jobs／Labeling／Learning 业务与 SQL | `ai-annotation-platform`           | 公共服务／有限 owner 契约；禁止搬入插件仓或让插件直接读取核心表                |
| 公共 wire 类型、manifest schema、任务与受控训练产物契约                                   | 平台维护的便携契约包               | 发布版本化 wheel、JSON Schema 与一致性夹具；插件消费制品，不复制另一份类型真值 |
| 平台 Python 客户端及管理员 CLI                                                            | `packages/python-sdk` 留在平台     | 通过公共 API 管理安装；不成为模型运行依赖                                      |
| Python 插件开发 SDK、算法侧共享运行时、Mask 工具与 LibreYOLO 原生 loader                  | `YOAO-Plugins`                     | 独立 Python distribution；不依赖平台 checkout 或私有应用包                     |
| 六个算法服务、模型适配、vendor、各自镜像与资格工具                                        | `YOAO-Plugins`                     | 独立 OCI 制品，按 digest 安装                                                  |
| 权限与资格证明的解释、准入决定                                                            | 平台宿主／Inference owner          | 插件提供事实与证明，不能自行批准或修改资格标准                                 |
| 模型权重与受控训练产物                                                                    | 各自获准的资产渠道／平台领域 owner | 授权准备、摘要校验、只授予所需访问；不随代码仓任意复制或公开                   |

迁移共享运行时前，需把平台依赖的**便携证明 schema／纯校验规则**与模型侧采样、装载、清理机制分清。当前 API 和管理员 CLI 引用了 `aap_backend_runtime.lifecycle_evidence`；不得因此让宿主的权威校验依赖某个具体模型插件。沿现有公共契约承接纯校验，生产证据的工具留在 SDK／运行时。

Mask 算法工具可随模型代码迁移；平台的几何语义、像素格式与现有一致性夹具仍受核心契约约束，不借拆仓改写编码或增加新的权威格式。

目标布局如下；这是后续目录规划，本轮未在远程创建这些目录：

```text
YOAO-Plugins/
  packages/
    inference-plugin-sdk/
    backend-runtime/
    mask-utils/
    libreyolo-native/
  plugins/
    libreyolo/
    yolo/
    rapidocr/
    onnxtools/
    grounded-sam2/
    sam3/
  conformance/
  examples/
```

暂不创建前端 SDK 占位目录、不拆成六个仓库、不引入平台 Git submodule。跨仓依赖用发布制品；仓内开发可使用 workspace sources，但发布 wheel 与镜像必须不含指向另一个 checkout 的 editable/path 依赖。

## 4. 现有试点需要补齐的接缝

这不是推翻试点，而是把已验证的机制与检测场景的限制分开。当前源码存在以下明确边界：

| 当前限制                                                   | 证据                                                                                    | 扩展要求                                                                                     |
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

1. **新建简单推理插件**：模型作者实现小型 load／predict／close 接口与目录配置，SDK 完成协议、认证、借用和边界转换。现有 `aap-plugin init/dev/check/build` 继续作为检测开发路径；不能把这些命令描述成已支持全部任务。
2. **迁移既有复杂服务或其他语言实现**：保留原 HTTP 服务和模型池，通过 manifest、安装身份、配置、准入与生命周期接入。先证明完整功能不丢失，再提取有实际复用价值的共享机械过程。

打包也允许两条路径：Python 检测作者使用已存在的离线 wheelhouse／hash lock 构建；包含 CUDA 编译、vendor 或其他语言的服务保留明确的独立 Dockerfile。两者都交付固定 digest 的 OCI 制品，并通过相同的宿主安装与行为资格，不强迫使用一份通用基础镜像。

模板与指南须让开发者能在**没有平台源码、数据库、Redis 和内部签名私钥**的环境完成本地开发与协议检查。最终平台联调使用普通安装和项目启用流程，不要求开发者手写内部表或理解 Celery。

## 6. 执行顺序与验收门

### A. 完成 LibreYOLO 纵向切片

保持原计划范围：真实训练 → 原生 checkpoint → GPU 受控加载／试用 → 人工发布 → 当前任务与批量预标注。完成实际制品生命周期、同卡仲裁／隔离、CPU 历史冻结引用与退役证据后，才能删除旧适配器。

修复试点验收发现的具体问题，不在此阶段实现前端 SDK。控制公钥已改为独立只读挂载，并以实际容器写打开返回 `EROFS` 验证；数据目录仍可写，资格报告只读。保留真实失败、不可判定状态和恢复路径，不通过放宽权限或资格校验达成“成功”。

### B. 固定可移植推理契约与分发边界

先完成上一节接缝中的实际前置：多模型／多结果能力匹配、CPU／CUDA 配置、真实多池资格，以及公共契约和运行时的依赖边界。此阶段仍只处理推理扩展点，不建设全平台插件装配器。

验收包括：现有 LibreYOLO 行为不回退；非 bbox／多模型目录不能被错误拒绝，也不能越过许可；CPU 无 GPU 依赖可运行；真实多池不能漏报；契约导入不加载 Torch、模型实现、ORM 或 Celery。已有 HTTP 字段、模型 ID、几何与错误语义保持不变。

### C. 首次实际拆仓：SDK、共享运行时、LibreYOLO

先为公共契约提供能实际获取的版本化制品，再迁入 `YOAO-Plugins`。从新仓干净检出构建 wheel／镜像，在没有平台源码的环境验证开发者路径，再连接平台完成与阶段 A 相同的业务验收。

Python 制品先使用明确的发布渠道与 SHA256 清单，例如 GitHub Releases 的 wheelhouse；需要公共 PyPI 时另行完成名称、发布权限与许可证核验，不假定当前包已在 PyPI。OCI 使用组织的镜像发布渠道，平台安装固定 digest，不使用 `latest` 作为身份。

每份代码确定唯一源码归属。切换平台依赖、Compose、CI、资产准备脚本、文档及所有调用方后，删除已迁出的旧实现；不保留双维护镜像、跨应用 re-export 或运行时回退到旧目录。历史已部署旧镜像在引用排空前仍可运行，这不是保留第二份活跃源码。

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

目标仓库暂设私有。公开前须确认本项目包装代码的许可证与分发权限，逐个保留上游 LICENSE／NOTICE；不能把所有插件统一标成 MIT，也不能认为容器隔离自动消除了许可证义务。

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

## Outcome

- 本轮已完成六个迁移单元、真实共享依赖、检测专用假设、有状态边界与许可证前置的盘点，形成上述顺序与验收门。
- 已按用户指定创建并核验 `YOAO-Labs/YOAO-Plugins` 为私有仓库；未推送代码或权重。
- 统一契约／分运行环境 SDK／前端信任分层为采用的目标方向；本期只推进推理与 Python 开发体验，不宣称通用插件框架或前端 SDK 已实现。
- 正式架构入口：`docs-site/dev/concepts/module-deployment.md`。本轮规划不增加功能发布记录或版本号。
- 实施与验收：LibreYOLO 原计划的限定单卡验收已完成；其余五个后端的插件化、实际拆仓与跨仓发布尚未实施。本轮静态盘点不计作这些后端的运行验收。
