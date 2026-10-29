# YOAO 产品语言与命名优化计划草案

> 状态：草案，待实施审阅。本轮交付命名决策、清理范围、迁移顺序及验收门槛，不修改应用、测试、配置、数据或运行环境。
>
> 日期：2026-10-29。已确认产品名：**YOAO**，全称 **You Only Annotate Once**。
>
> 核心原则：**业务内部名称去品牌前缀；跨项目、跨进程或对外分发的必要命名空间使用 `yoao`；历史事实与安全身份不因品牌变化而重写。** 不做全仓 `AAP → YOAO` 替换，不以搜索结果清零作为目标。

## 1. 目标与范围

### 1.1 目标

1. 用户看到统一的 YOAO 产品身份，以及简洁、准确的业务语言，不再混用产品意义上的 AAP、AI Annotation Platform 和“标注中心”。
2. 删除函数、类、常量、私有包和内部事件中没有区分作用的品牌前缀，不制造一批 `YOAOTask`、`YOAOImportResult`。
3. 对外 CLI、Python 发行包、共享机器配置和部署身份使用一致的 YOAO 命名；内部目录、模块、业务字段和操作名继续描述职责。
4. 不丢失用户草稿、训练请求、标注、文件来源、任务回执、凭据或历史制品；改名不改变权限、最终提交、取消和资源归属规则。
5. 将这次迁移作为[测试治理与 E2E 稳定性计划](1793261328_test-governance-module-plugin-contracts.md)的实际场景：内部重命名不重写业务期望，确实变化的显示名／公开入口单独说明。

### 1.2 与既有架构工作的关系

- [后端模组解耦计划](1792160873_backend-module-decoupling.md)及[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md)继续有效。名称变化不等于独立部署、消费者迁移或插件资格完成。
- [插件能力边界研究](../research/51-plugin-capability-boundaries-and-ecosystem.md)强调核心 owner 和扩展责任；这里的命名应表达责任，而不是把所有代码都称为“插件”。
- [LibreYOLO 首切片](1793156248_libreyolo-inference-plugin.md)继续完成原业务与 GPU 验收；命名迁移不改写其原制品证据。
- [YOAO-Plugins 拆仓计划](1793184138_inference-plugins-repository-extraction.md)负责实际仓库与制品迁移。相关包在迁仓时采用最终名称，避免先以旧名拆仓、再做第二轮整体更名；不能因此跳过首切片门槛。

### 1.3 非目标

本计划不授权改名或搬迁现有 GitHub 仓库、变更 GitHub Pages／生产域名、公开或推送私有仓、发布包或版本、重做 Logo、引入国际化框架、改动第三方产品名，也不修改真实用户配置或生产资源。

当前 `YOAO-Labs/YOAO-Plugins` 的私有仓状态来自原拆仓计划，本轮未独立检查远端。以下 PyPI／其他 registry 新名称是目标命名，不是已注册、已发布或已经可安装的事实；实际分发前核实可用性与权限。

## 2. 盘点结论：同一个前缀承担了不同职责

本轮按源码和现有文档抽查品牌入口、SDK、共享包、原生格式、数据库协议、浏览器草稿和运行资源。没有执行全仓迁移，也不提供未经测量的删除比例。

| 类型               | 当前具体证据                                                                                           | 判断                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------- |
| 用户可见品牌       | README、登录／注册、顶栏、HTML／PWA、文档站仍使用 AI Annotation Platform 或“标注中心”                  | 应改为 YOAO，业务界面不用反复加产品名前缀                  |
| 冗余内部符号       | `AAPTaskMatch`、`AAPAnnotationEntry`、`AAP_SCHEMA_VERSION`；session info 与 ContextVar 中也有 aap 名称 | 优先去前缀，模块已提供上下文；先核对是否泄漏为公共 schema  |
| 对外包和命令       | `ai-annotation-sdk`、`ai_annotation`、`aap`、`aap-inference-plugin-sdk`、`aap-plugin`                  | 需要统一的产品命名空间，不能直接改成泛化的 `sdk`／`plugin` |
| 显示名与机器值混合 | UI 的“AAP JSON”，API 的 `aap_json`，文件的 `format: "aap"`                                             | 三层分别迁移；改显示名不等于修改协议                       |
| 持久状态           | 训练草稿前缀、`_aap_managed` 对象路径、`__aap_reference__`、冻结 Jobs 信封                             | 不能文本替换，必须保持所有权、请求身份和精确引用           |
| 安全身份           | Identity issuer 参与密钥派生；数据库 grants、worktree 标签按 aap 名称做归属验证                        | 不得为了品牌清理降低检查或使旧资源失去保护                 |
| 已经足够清晰的名称 | `Client`、`Task`、`training_contract`、`libreyolo_native`、`default`／`maintenance` 等队列             | 保持，不再补上 YOAO                                        |

**关键修正：Python 名字不一定是私有实现。** `AAPImportResult`／`AAPImportErrorEntry` 是公开端点响应模型，现有 OpenAPI 包含同名组件。去掉类名前缀虽不改变响应 JSON，也会改变生成客户端的 schema 名；必须纳入公开生成物和 SDK 验证，不能按普通局部变量处理。

## 3. 统一命名规则

### 3.1 四类处置

- **DROP**：没有外部或持久消费者的品牌前缀，直接删除，保留必要的领域限定。
- **YOAO**：在全局安装、跨仓分发、共享进程环境等边界确有避碰或产品归属需要，使用 `yoao`。
- **MIGRATE**：已有外部消费者或持久数据；先完成相应协议／状态／配置切换，再使用目标名称。
- **KEEP**：第三方名称、已冻结的历史事实、签名／派生域、历史迁移及仍受支持的旧数据协议，保留精确原值。

`MIGRATE` 不是永久搁置项，须对应阶段、消费者和退出证据；`KEEP` 也不是免审白名单，须说明不可变身份或仍在支持的合同。新增业务代码不得以这些例外为由继续扩散 AAP 品牌命名。

### 3.2 大小写与边界

| 场景                       | 规则                                        | 示例                                                 |
| -------------------------- | ------------------------------------------- | ---------------------------------------------------- |
| 产品名称                   | 大写 `YOAO`                                 | YOAO、YOAO 文档                                      |
| 首次正式介绍               | 产品名加英文展开                            | YOAO — You Only Annotate Once                        |
| 普通业务界面／类型／方法   | 不带产品名；使用领域名称                    | 项目、标注、`TaskMatch`、`ImportResult`              |
| 根私有 workspace           | 产品根名称即可                              | `yoao`                                               |
| 私有子 workspace／源码目录 | 职责名称                                    | `web`、`docs-site`、`apps/jobs`                      |
| 对外 Python distribution   | `yoao-<capability>`，主 SDK 使用 `yoao-sdk` | `yoao-task-contract`、`yoao-inference-plugin-sdk`    |
| 对外 Python import         | 主 SDK 为 `yoao`；独立包沿现有 flat 风格    | `yoao_jobs`、`yoao_inference_plugin`                 |
| 独立安装的命令             | 主命令 `yoao`；专用工具保留明确命名空间     | `yoao-plugin`、`yoao-jobs-queue`                     |
| 跨进程环境／宿主资源命名   | 需要项目归属时 `YOAO_`／`yoao-`             | `YOAO_WORKTREE_MODE`、`yoao-development-api.service` |
| 已隔离的应用内事件、状态键 | 领域命名，不叠加产品名                      | `shell:popover-open`、`training:draft:1`             |
| 第三方能力                 | 原名与原协议保持                            | YOLO、LibreYOLO、COCO、SAM3、CUDA                    |

不把所有 Python 包合并到新的 `yoao.*` namespace package，也不顺便重组领域目录。当前 flat import 路径的独立构建方式继续使用；品牌前缀只留在全局包名这一层，包内类型／函数不再重复。

## 4. 产品语言优化

### 4.1 品牌入口

同步范围包括：

- `README.md` 的产品标题、简介、图标 alt；保留仍然有效的仓库链接和部署地址。
- `apps/web/index.html`、`public/site.webmanifest`、当前图标的可访问描述。
- `LoginPage.tsx`、`RegisterPage.tsx`、`TopBar.tsx` 中的产品身份。
- `docs-site/.vitepress/config.ts`、`docs-site/index.md`、页脚及生成 `llms.txt` 的来源。
- SDK／CLI／TUI help、包 description、插件开发模板的产品说明。

可见标题统一 YOAO；图标可保留原图形。自有公共静态资源名如 `ai-annotation-platform-icon.svg` 可在同批引用迁移中改为 `product-icon.svg`，不再绑定品牌；PWA／缓存更新实际验证。仓库地址、文档站 base、sitemap hostname 和 GitHub 编辑链接不能据此改成尚不存在的新地址。

### 4.2 术语与表达

| 当前或易混表达                                        | 目标表达                                                                   | 约束                                                                              |
| ----------------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| 产品身份意义上的“标注中心”／AI Annotation Platform    | YOAO                                                                       | 几何含义的“标注中心点”等不替换                                                    |
| AAP JSON／AAP JSON（平台无损）                        | 原生 JSON；说明中写“YOAO 原生交换格式”                                     | UI 名称与实际格式标识分离；说明具体保留内容，不暗示凭据／所有外部资产都可无损搬走 |
| 把外部模型预测“灌进”项目                              | 将外部模型预测导入项目，供人工审阅                                         | 不把预测等同于已确认标注                                                          |
| ML Backend 混指模型、服务、插件和安装                 | 面向用户使用“模型服务”；安装管理使用“推理插件”／“插件安装”；算法使用“模型” | 开发协议中的 Backend 术语可以保留，不能合并不同实体                               |
| 内置模块、扩展能力统一叫插件                          | 明确“内置核心模块”与“可安装扩展”                                           | 尚未交付统一装配时不得宣传为已完成的通用插件体系                                  |
| provenance／lineage／runtime 等直接堆在普通操作说明中 | 按场景写“来源”“关联记录”“运行环境”                                         | 开发／运维页面及可复制诊断仍保留精确字段、摘要和设备身份                          |
| 试用／安装成功被描述成已经可用于全项目                | 分别写“安装”“启用”“试用”“发布”                                             | 保持现有权限与人工确认边界                                                        |

品牌展开表达减少重复劳动的方向，不承诺“一次标注后永不复核”或“AI 输出自动正确”。不借文案优化改变审核、训练质量或产品能力承诺。

遵循现有 UI：操作标签短、帮助放说明区、失败提示说明对象和可执行动作。错误的 machine code、API 字段和日志诊断标识不随中文润色批量改名；敏感数据不进入面向用户的错误详情。本计划不新增翻译键系统或抽象所有文案。

### 4.3 显示名称的完整来源

原生格式名称不只在 Web 常量里：`apps/api/app/services/mask_formats/catalog.py` 也提供 `label="AAP JSON"`；SDK／TUI、任务铃、数据管理批量操作和文档都需要同步。以各自真实数据来源修改，生成物从生成源更新，不另建与服务端目录漂移的前端名称表。

## 5. 优先删除的冗余前缀

以下为目标映射，实施时先查询引用与同作用域冲突，不保留旧符号 alias／re-export：

| 当前                                                       | 目标                                                   | 边界说明                                                                     |
| ---------------------------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `app/schemas/aap_json.py`                                  | `app/schemas/native_json.py`                           | 模块表达原生交换格式，不绑定品牌                                             |
| `AAP_SCHEMA_MAJOR`／`AAP_SCHEMA_VERSION`                   | `SCHEMA_MAJOR`／`SCHEMA_VERSION`                       | 在原生格式模块内已有充分上下文                                               |
| `AAPTaskMatch`／`AAPAnnotationEntry`／`AAPPredictionEntry` | `TaskMatch`／`AnnotationEntry`／`PredictionEntry`      | 包内名称不重复产品；调用处可用模块限定消歧                                   |
| `AAPJsonV1Envelope`                                        | `NativeJsonEnvelope`                                   | 协议版本由现有 schema 合同表达，不因类名改动改变实际可读版本                 |
| `AAPImportResult`／`AAPImportErrorEntry`                   | `ImportResult`／`ImportErrorEntry`                     | 同步 OpenAPI 组件、生成客户端及文档；SDK 已有独立 `ImportResult`，不得误合并 |
| `import_aap_json`／`import_aap_json_annotations`           | `import_native_json`／`import_native_json_annotations` | 先只迁 Python 引用，不顺手变更外部输入参数或文件格式                         |
| `session.info["aap_verified_receipts"]`                    | `session.info["assets_verified_receipts"]`             | 保留领域限定及提交／回滚清理语义                                             |
| `ContextVar("aap_backend_route_request")`                  | `ContextVar("backend_route_request")`                  | 同进程上下文标识，不改 wire 路由合同                                         |
| `aap:shell-popover-open`                                   | `shell:popover-open`                                   | 同一应用 document 内的当前事件，发送和订阅一起切换                           |
| 受本调用管理的 `aap-content-` 等临时前缀                   | `content-` 等用途前缀                                  | 确认无外部清理器按旧名识别；不能套用到 worktree／受控存储                    |
| 私有 `@anno/web`／`@anno/docs-site`                        | `web`／`docs-site`                                     | 同步 pnpm filters、CI、脚本、锁文件和文档；不改成无必要的 `@yoao/*`          |
| SDK `AAPError`                                             | `SDKError`                                             | 是公开异常类型，完整迁移调用者与捕获示例，不当成纯私有更名                   |
| SDK `__aap_target_version__`                               | `__platform_target_version__`                          | 保留 SDK 版本与平台验证基线分离，不扩展兼容承诺                              |

`_validate_aap_mask_objects` 当前只转调已有 resolver。若引用审查确认无需独立语义，直接删除这层包装并迁移调用方，而不是换成 `_validate_yoao_mask_objects`；不得顺手改变原校验／错误行为。

从当前源码到计划中的命名映射要逐项处理：有些包如 `training_contract` 和 `libreyolo_native` 的 import 已经没有 AAP 前缀，不为了发行名一致给它们重新添加前缀。仓内 `tests`／`helpers`／`fixtures` 等名称也保持。

## 6. 应采用 YOAO 命名空间的边界

### 6.1 SDK、CLI 与独立发行包

| 当前                                                             | 目标                                                                | 使用者／约束                                              |
| ---------------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------- |
| `ai-annotation-sdk`／`ai_annotation`／`aap`                      | `yoao-sdk`／`yoao`／`yoao`                                          | 平台客户端、管理员 CLI 与 TUI；公共 API 使用者需要迁移    |
| `aap-inference-plugin-sdk`／`aap_inference_plugin`／`aap-plugin` | `yoao-inference-plugin-sdk`／`yoao_inference_plugin`／`yoao-plugin` | 模型作者；当前窄检测 SDK，不宣传为全任务万能 SDK          |
| `aap-libreyolo-plugin`／`aap_libreyolo_plugin`                   | `yoao-libreyolo-plugin`／`yoao_libreyolo_plugin`                    | 自有包装包改名，上游 LibreYOLO 名称不改                   |
| `aap-libreyolo`／`aap-libreyolo-evidence`                        | `yoao-libreyolo`／`yoao-libreyolo-evidence`                         | 独立安装工具；仍区分证据采集和平台批准                    |
| `aap-jobs`／`aap_jobs` 等实际跨包模块                            | `yoao-jobs`／`yoao_jobs`                                            | 包名防冲突，包内 `QueueClient` 等名称不补品牌             |
| `aap-backend-runtime`、`aap-…-contract` 等跨仓发行名             | 对应 `yoao-…`                                                       | 按现有依赖图迁移，不复制 schema，也不把平台权威搬到插件仓 |
| `aap-jobs-queue` 等独立可执行入口                                | `yoao-jobs-queue` 等                                                | shell 全局命令需要产品归属；迁移启动器与镜像入口          |

`yoao plugins` 是平台管理员操作；`yoao-plugin` 是作者开发工具。保持两者安装依赖和权限分离，不为了命令统一让客户端核心依赖 Torch，也不把核心 Inference owner 与推理插件合并。

包迁移包含 pyproject、源码包目录、resource lookup、entry points、生成目录、模板、锁文件、wheelhouse、hash lock、Docker build context、manifest 及所有消费方。发布名称改了不代表 wheel 中的包路径已经迁完；生成 JSON 的源脚本也必须更新。

新发行包完整切换后不保留旧 import shim、双 CLI 别名或两个可维护源码包。对仍需管理的历史不可变镜像，先使用其原工具安全排空／退役，再切新控制工具；若业务必须跨代管理，必须以明确的制品版本契约表达其能力，不得“新命令失败就试旧命令”。这不能演变成无限期维护两个活跃实现。

### 6.2 环境变量和本机配置

| 当前                                                                                      | 目标原则                                                                                         | 迁移要求                                                                               |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `AAP_BASE_URL`／`AAP_API_KEY`                                                             | `YOAO_BASE_URL`／`YOAO_API_KEY`                                                                  | 保持显式参数 > 环境变量 > 配置文件，逐字段解析；不得打印凭据                           |
| `~/.config/ai-annotation/config.toml`                                                     | `~/.config/yoao/config.toml`                                                                     | 明确一次性迁移，目标冲突不覆盖，权限保持 `0600`；不自动删除旧凭据文件                  |
| `AAP_WORKTREE_MODE` 等工作树安全配置                                                      | `YOAO_WORKTREE_MODE` 等                                                                          | 启动器、测试 DB 守卫、seed／验收工具和环境生产者同时切换                               |
| `AAP_IMAGE_TAG`／`AAP_SHARED_NETWORK`／`AAP_PRODUCTION_STATE_DIR`                         | 对应 `YOAO_…`                                                                                    | 这些是部署级入口，命名空间有作用                                                       |
| `AAP_BACKEND_AUTH_DIR`、`AAP_GPU_PHYSICAL_DEVICE_TOKEN`、`AAP_ROUTE_CONTROL_TOKEN(_FILE)` | 专用后端进程中使用 `BACKEND_AUTH_DIR`、`GPU_PHYSICAL_DEVICE_TOKEN`、`ROUTE_CONTROL_TOKEN(_FILE)` | 领域限定已经明确；先核对整组 Compose 注入与真实读取者，保留 file-secret 和 fail-closed |
| 已清楚的 `DATABASE_URL`、`REDIS_URL`、`MINIO_BUCKET` 等                                   | 保持                                                                                             | 不增加新的品牌前缀或新一层环境映射                                                     |

旧环境变量应在受控部署切换时退出；不能静默读取新旧变量导致来源不明。迁移前置检查只报告旧键名和冲突，不暴露值；旧键未迁完整时阻止对应切换，不让默认值造成未认证服务或错误资源接管。

`.env`、依赖目录可能链接主 checkout；先核对实际目标，配置变更只在当前授权环境执行。修改容器环境要重新创建受影响实例，依赖或镜像内源码变化还须重建对应镜像，不能只重启 API；Celery 挂载源码也要刷新相应消费者。

### 6.3 运维资源和项目工具

- 新建的项目级服务／资源按 `yoao-development-*`、`yoao-production`、`yoao-wt-*` 等有归属的名称命名；独立 Compose service key 如 `api`、`web`、`worker` 保持简短。
- systemd 单元、timer、恢复器、wrapper 与状态路径一起迁移，先停止旧调度再启新调度，避免两套同时操作同一资源。
- 私有工作区 skill 的 `aap-*` 名称属于跨项目发现命名空间，适合采用 `yoao-*`，不是去前缀后变成泛用 `runtime`。实施时只修改单一指令来源及其符号链接／引用，在新会话验证发现入口；不改当前会话的工具配置或用户全局安装目录。
- 不改用户 checkout 目录、远端 URL 或本机服务，除非进入已批准的对应环境采用阶段。

## 7. 持久状态与公开合同：不能混入普通重命名

### 7.1 原生 JSON：显示、接口选择器、文件身份分三步

**第一步显示名：** 页面／目录／CLI 说明使用“原生 JSON”。在迁移指南中明确旧名称与当前接口标识；常规 UI 不继续重复旧品牌，也不伪称机器值已切换。

**第二步 API 选择器：** 目标为品牌无关的 `native_json`，而不是 `yoao_json`。这属于公开契约迁移，需同时迁移 OpenAPI、Web、SDK、CLI、导入／导出、Mask format catalog 和各格式路由。

现有图像／视频／点云导出会冻结 targets，Job payload、缓存和多目标 ZIP 目录也可能使用 `aap_json`。在切换前停止相关新准入并收敛旧执行，旧制品按原样保留；不得修改已冻结目标、重写 Job 历史或把旧 ZIP 目录当作展示文案替换。新活跃 API 不保留永久 selector alias，历史产物读取按其原格式处理。

**第三步新文件身份：** 目标的新原生文档使用 YOAO 产品身份；变更 `format`／`exported_from.platform` 时按现有格式版本规则显式升级，并验证新旧文件导入。旧文件仍记录 `aap`，来源不能篡改；旧 reader 不能因品牌替换默默接受不支持的新版本。具体 schema 版本在该合同实施时确定，不在本计划分配产品发布版本。

类名／模块名清理与上述两次合同切换可分开验证。旧文件解析能力是一项仍受支持的用户数据能力，不是旧源码 re-export；只保留必要的旧格式解码，后续业务走同一内部模型和 owner。

### 7.2 浏览器草稿去品牌化

目标使用同源内的领域键：

- `aap-training-draft:1` → `training:draft:1`。
- `aap-training-dataset-request:1:` → `training:dataset-request:1:`。
- `aap:import-text:` → `datasets:import-text:`。

这不是普通常量替换。训练草稿含 user／project owner、request ID、revision、pending run／study；训练数据请求的 `ownerKey` 还必须等于完整存储键。

在所属 owner 的现有读取／保存边界完成一次性数据升级：校验原拥有者和版本，保留原请求身份及未确认内容，显式转换包含键名的 owner 字段；先可靠写入并验证新记录，再移除准确旧记录。新旧冲突、损坏、配额不足或跨账号状态均不得覆盖／清空原草稿，更不能自动重新提交训练。

升级入口不建立永久双写和“每次失败再读旧键”的隐式回退。支持旧草稿的期限按产品数据保留要求确定，不能为了删掉旧字符串而让暂时离线的用户回来后丢稿。新建草稿只写目标键；`localStorage.clear()` 不是迁移方式。

### 7.3 数据库命名空间与最小权限

对公开 owner schema 目标使用 `yoao_workspace_v1`、`yoao_jobs_v1` 等必要命名空间；函数名 `append_dataset_items`、`query_events` 等不补产品名。数据库角色／PostgreSQL 自定义 GUC 在共享命名空间中保留 YOAO 归属。

这部分必须在对应 owner 消费者与当前资格明确之后单独交付：

1. 新增前向迁移，不改写历史 Alembic revision、冻结 SQL、JSON Schema 或历史语句分隔符。
2. 核对函数体显式 schema 引用、视图、触发器、固定 search_path、角色、grants、安装包和 Python／Node 消费者；不能把单个 `ALTER SCHEMA` 当作迁移完成。
3. 当前安全脚本会按 `^aap_…`、前缀长度和已知 namespace 识别权限；迁移期间必须识别两类资源，旧谱系拒绝仍须识别旧数据库，不能把检查替换得只看见新名称。
4. 原子事务、锁顺序、PUBLIC 撤权及 owner／LOGIN 分离保持，不能恢复私表权限以兼容改名。
5. 完成消费者切换和旧写者退出后移除旧活跃 namespace／别名；历史迁移链仍可从空库安装，再应用新迁移到当前状态。

复用现有部署 grants 和迁移工具；`scripts/verify_module_data_boundary.py` 是隔离环境验收器，不当作生产安装器。

### 7.4 凭据、协议和锁的保留／切换

**本品牌计划明确保留以下身份，不为追求字符串清零强行更改：**

- Identity issuer／密钥派生域 `aap-identity/v1`、API key derivation 等；它们决定旧 JWT 验签、refresh 和同 operation 的密钥重放，不是登录页品牌名。
- 已冻结回执中的 `aap-task-append/v1`、已持久化队列信封和历史训练格式／实现身份。
- 已签名／加密的请求、回执、安装资格、模型来源、Lua／内容摘要和派生 UUID 的原始输入。

它们仍可能出现在当前兼容读取与验证代码中。只改 Python 标识或文档显示，不重新编码历史 payload；如以后发生真实协议／安全升级，再按该协议独立版本化，新协议命名采用 YOAO。这里的 KEEP 不许可继续新增同义 AAP 品牌接口。

**属于本计划可迁移的当前边界：**

- `X-AAP-GPU-*`、`x-aap-route-admission`、`/v1/aap-routing/control` 等产品私有控制入口，目标采用对应 YOAO namespace；必须先具备平台、仍在运行的全部 provider 和新插件制品的匹配合同，不能只改发送端。签名 type／audience 等若与旧证明绑定，按凭据／协议升级单独处理，不和 header 文本改名混为一件事。
- `aap:task-edit-lock:<UUID>` 必须在所有旧写者退出后协调 Python 与 SQL 同时切换，目标可采用领域限定的 `task-edit-lock:<UUID>`；新旧锁域同时存在会失去互斥，不能用双锁名 fallback 掩盖。
- relay、短期媒体许可／cookie、GPU 在途请求等先按实际 owner 证明收尾，再切键名／当前入口。取消、TTL 到期或 broker accepted 不等于远端 I/O 或计算已经停止。

现有无品牌队列 `default`、`maintenance`、`ml`、`ml.cpu`、`gpu`、`gpu.control`、`export`、`image-pyramid` 及 `gpu-arbiter` 命名保持。普通 API 路径和业务字段不会为了品牌统一增加 `/yoao` 或 `yoao_`。

### 7.5 存储、引用和工作树资源

`_aap_managed` 被数据库 CHECK、对象键、IAM、引用验证和恢复流程共同约束；`__aap_reference__` 是不可当作普通对象路径读取的哨兵。它们不是可随意删除的临时目录。

- 已保存对象键、version ID、位置 UUID、冻结引用及对象 metadata 保持原值。不得只替换 DB 中的字符串；也不为品牌迁移搬运所有旧对象。
- 新托管写入的目标前缀采用 `_yoao_managed/<purpose>/`，新非物理引用哨兵采用 `__yoao_reference__/`；在所属 owner 的新写入合同、CHECK、解析、清理与恢复规则全部支持后切换。旧对象仍按原位置及准确版本读写／保留，不把旧引用改指当前桶。
- 派生已有位置身份的 `aap:storage-connection:<UUID>` 和 `aap:platform-storage:legacy` 保持，不能重算成新 UUID；新名称不是一个新存储位置。
- `aap-prediction-lease` 等旧 metadata 继续按原 lease／nonce 校验，SHA 相同不代表资源归属相同。
- worktree 的 owner 标签、桶、数据库和 Redis label 是拒绝接管／删除外部资源的依据。新资源可使用 YOAO 标识，旧环境按清单升级或在精确清理后重建；不把“标签改成新名”当作取得所有权。
- Compose project 改名会改变隐式 volume／network 身份。必须显式核对原卷、对象与状态路径，完成一致恢复后再退役旧实例；新建空卷后服务能启动不能算成功迁移。

原则是新写入使用目标命名、旧数据保持真实身份，由现有 owner 维护合法的历史读写规则，不保留两个活跃实现。确需物理搬迁时复用现有 inventory → copy／verify → fence → cutover 流程，并按独立存储迁移授权执行；本次品牌计划不默认授权复制或销毁数据。

## 8. 阶段与验收门

### N0：冻结命名决策与影响清单

- 按 DROP／YOAO／MIGRATE／KEEP 对当前活跃源码、公开 API／包、生成源、配置、持久身份和历史证据分类，区分 `AAP`、`aap_`、`aap-`、`aap:`、`aap.`、`@anno` 和旧完整产品名。
- 发现侧排除 vendor、依赖、构建产物与历史原始证据；生成物跟踪其源，不独立改写。历史计划、ADR、CHANGELOG 只在当前使用说明或链接确实变化时补说明，不重写当年的事实与文件名。
- 迁移记录至少包含：当前名、目标名、所有者、消费者、是否已持久化、生成源、验证入口和保留依据。记录重点变更面，不建立另一套产品模块完成台账。
- 对受影响导出符号使用语言服务器查询引用；对结构性重命名使用已有 refactor／AST 工具，字符串命中必须人工分类，不能 regex 一把梭。

**退出门：** 每个首批目标有明确新名和边界；外部分发名称的可用性标记准确；没有把安全／持久字符串误归为局部重命名。

### N1：用户可见品牌与语言

**依赖：** N0 的可见名称范围。

同步第 4 节入口、原生格式 label、SDK／TUI 文案和当前文档；保留实际 API 值、文件结构和部署 URL。只在已有组件／文档所有者中修改，不先抽象“品牌系统”。图标语义、页面标题和 manifest 一并核对。

**验收：** 登录、注册、顶栏、导入／导出、后台任务名称和文档站真实呈现一致；按受影响页面检查明暗主题、桌面／窄屏、溢出、请求和控制台。实际导入／导出仍可往返，旧格式 machine value 未被误改。涉及正式媒体时按现有媒体 provenance／审核流程更新，不批量重录所有素材。

### N2：内部去前缀与私有 workspace 清理

**依赖：** 对应 N0 引用清单；可与 N1 独立推进。

按第 5 节逐 slice 迁移模型／常量／函数、session key、ContextVar、内部事件、临时名称和私有 workspace。删除无意义的转发包装；更新每个真实调用者、包发现、脚本、测试路径和文档引用，不留新旧兼容层。

OpenAPI 暴露的类名单独生成和审阅组件差异，JSON 行为保持；不把生成客户端类型破坏藏在“内部 refactor”提交里。私有 pnpm 包名变动同步根脚本、CI、工作树启动与锁文件，确认共享 node_modules 链接没有被跨 checkout 安装修改。

**验收：** 受影响代码编译／类型检查、现有行为回归及真实入口 smoke 通过；内部符号不再含冗余产品前缀；无旧 import／re-export；保存、权限与 E2E 业务期望不变。

### N3：客户端、作者 SDK 与跨仓发行名

**依赖：** N0 的公共名称决策；插件包同时遵守原拆仓阶段门槛。

迁移第 6 节发行包、import、CLI、异常、元数据和模板。先准备可获取的新契约制品，再切消费方；包名／代码路径／resource lookup／console script／锁定制品一次迁完。配置使用明确迁移步骤，完整保留优先级和凭据保护。

平台客户端与模型作者 SDK 分批验证；六插件按实际依赖和拆仓单元迁移，不要求无关算法环境同时升级。历史实例先安全处置或按明确协议维护，不能用新工具碰运气尝试旧镜像入口。

**验收：** 干净目录从 wheel／sdist 安装，实际 import、help／version、真实只读 API、必要 CLI／TUI、模板生成／构建和插件 HTTP 检查通过；core-only 不要求 CLI/TUI/Torch；仓外不依赖 checkout 或 editable。新发行物与当前客户端不残留旧命名入口；真实发布和远端权限单独确认。

### N4：公开格式和浏览器持久状态

**依赖：** 对应 N1／N2，公开 SDK 消费者具备 N3 目标入口。

分别交付原生 JSON selector／新文档身份，以及各类浏览器草稿升级。每个 slice 都验证历史输入、当前写入、错误和恢复；不能一次改所有 fixture 后把旧格式支持假装验证过。

**验收：** 历史 AAP 文件、当前新文件和实际导入／导出完整往返；旧来源保持；冻结旧导出执行已按范围收敛，历史产物仍可读取；草稿原 owner、revision、request ID、pending 内容均不变，冲突／损坏／写入失败不丢稿或重复提交。所有活跃消费者使用新的 selector 和命名，旧数据解析明确标记为历史能力而非活跃 alias。

### N5：按 owner 迁移数据库及当前控制边界

**依赖：** 对应模块已有清楚的消费者／运行资格；相关服务、插件、契约与制品已能按目标名协同。不要求全项目同时完成解耦。

分别处理 schema／grants／GUC，当前 header／控制路径，锁和短期状态；明确维护边界和仍需保留的历史协议。先停相关新写入并证明旧写者／执行收尾，再执行协调切换，不做永久双命名兼容。模型服务共享包或镜像变化须刷新实际运行服务。

**验收：** 空库升级、已有数据前向迁移、受支持的恢复／回退、真实受限 LOGIN／Node 消费者、调用方事务、重复回执和旧谱系拒绝通过；权限未放宽。签名／设备／代际／重放守卫及新制品实际业务路径通过，不把品牌迁移记作 GPU 或完整模块资格。

### N6：新资源命名与环境采用

**依赖：** 对应运行边界已完成 N5；精确备份、资源清单、采用授权和恢复点可用。

按环境迁移 worktree／systemd／Compose、目标状态路径与 owner 标签；先验证可销毁环境。新存储分配命名按所属用途切换，历史精确位置和已有资源继续受保护。旧调度器、worker 和实例按原运行流程退役，不凭进程名批量 kill 或删除。

**验收：** 新旧资源清单、卷／桶／角色／网络身份可核对；未知资源仍拒绝接管；真实启动、请求、队列消费和恢复通过；旧 timer 不再运行；带新写入的状态仍能按批准策略恢复。生产与用户日常环境未经授权不执行，也不宣称已采用。

### N7：文档、测试和残留收口

**依赖：** 本次批准实施的各 slice。

- 当前产品说明和新操作示例使用目标名称；README／DEV、API／SDK／插件开发／运维说明同步，OpenAPI、客户端、环境变量参考、操作目录等从正确来源重新生成。
- 用户可见品牌变化及公开迁移说明进入 CHANGELOG Unreleased，不擅自 bump 版本；历史 CHANGELOG／ADR 保留当时名称。
- 对活跃源码剩余 AAP 命中逐项检查：只允许明确的历史协议／安全身份、旧数据 reader／迁移或第三方事实；新的冗余 AAP 命名不得继续产生。
- 静态检查仅做有范围、可解释的新增命名守卫，不扫描历史档案全仓报错，也不增加“文件文字等于新品牌”的无意义测试。
- 清除本任务临时探针、任务自有资源和旧活跃代码入口；证据与限制写本计划 Outcome，模块／插件状态仍写原权威入口。

**退出门：** 每个迁移项可追溯，活跃名称统一，保留项有原因；未采用环境、未验发行渠道与仍待迁移边界准确列出，不能只凭替换脚本或 grep 数量宣称完成。

## 9. 验证要求：不让更名再次破坏测试的独立性

| 变更面         | 必须观察的结果                                         | 不能作为替代                       |
| -------------- | ------------------------------------------------------ | ---------------------------------- |
| 品牌与产品语言 | 真实页面／CLI／文档呈现、可访问名称、实际操作可完成    | 只断言源码包含 YOAO                |
| 内部重命名     | 原业务测试及 E2E 期望保持、真实调用与 import 成功      | 同时重写 expected 迁就实现         |
| 公共 SDK／包   | 仓外发行物、资源文件、核心／extras、命令与真实 API     | 只在源码 PYTHONPATH 下跑通         |
| 原生格式       | 已存在旧文件与新格式的独立往返、来源和几何一致         | 用新 writer 重新生成全部旧 fixture |
| 浏览器草稿     | 切换前后 owner／请求／pending 保持，失败不丢数据       | 清空 storage 后只测新建            |
| 数据库与控制面 | 真实角色、最小权限、事务／锁／重放、匹配制品           | 改写旧迁移或放开私表使测试通过     |
| 运行资源       | 原卷和桶正确使用、真实进程／消费者／恢复、外部资源拒绝 | 新空环境启动成功或标签字符串一致   |

显示名称改变可以更新相应定位与可见文案断言，但标注内容、权限、审核结果、训练发布及历史可读性不应改变。复用现有测试套件与实际 smoke，新增回归只覆盖这次迁移的真实边界：schema 名与 wire 值分离、归属冲突、旧文件读取、草稿升级、跨包资源、锁域切换等。

UI 验证使用实际构建和隔离环境；文档／计划阶段不虚构浏览器或 GPU 结果。每个集中验收记录源码、契约、制品摘要、选定范围、pass／fail／skip／not-run、环境和限制，遵循原测试治理计划的完整结果核对。

## 10. 风险、恢复与明确保留项

### 10.1 不能用更名掩盖的问题

- 更换 Identity 签名域会影响现有登录和确定性 key 回放；品牌迁移不默认轮换密钥或注销所有用户。
- 改锁键但未排空旧写者会产生两个互不排斥的锁；不能以最终状态偶尔正确证明安全。
- 改 Compose project、owner tag 或存储前缀可能丢失资源归属；新名称不授权接管已有对象。
- 类名变动可能改变 OpenAPI 组件，包名变动可能影响模板、资源加载和 manifest；都不只是文字润色。
- 只改页面“AAP JSON”不会迁移文件协议；反过来，为改显示名先改协议会扩大破坏面。

### 10.2 恢复规则

内部代码／文案可按独立提交回退。进入持久迁移后，回退需考虑已经写入的新状态，不得仅回滚代码、覆盖新草稿或重启旧 reader。数据库、对象位置、凭据与持久作业使用同一受控恢复点，保持历史身份不变。

任何无法可靠证明的旧执行、数据归属或迁移输入都阻断该 slice，不以宽权限、静默 fallback、清库／清桶或重新生成摘要解决；其他独立品牌／内部清理工作继续推进。

### 10.3 完成时允许保留的 AAP

历史迁移原文、旧导出与历史回执中的来源、仍受支持的旧文件解码、签名／密钥派生域、已有对象定位及标签证明、已归档计划／ADR／CHANGELOG／媒体证据可以保留。当前文档只在历史导入／迁移说明或精确协议参考中解释这些值，不把旧品牌作为新用户操作名称。

完成标准是**大部分无意义命名消失，必要 namespace 统一，历史与安全身份可信**，不是让仓库永远搜不到 `aap`。不设没有调查依据的删除百分比，也不靠把旧字符串搬到通用映射表来假装清理完成。

## 11. 建议启动顺序

首批先执行 **N0＋N1＋N2**：建立 YOAO 产品身份、精简内部命名和私有 workspace；公开 schema 名变化单独审阅。它们不等待全后端解耦，也不要求先搬迁数据。

随后 **N3 与现有插件拆仓对齐**，保证新制品从第一次独立分发就使用最终名称；**N4、N5、N6 按真实状态边界分别验收**，不能把高风险持久迁移混进一个“全局 rename”PR。

本轮仅交付这份草案。后续实施不保留无必要的旧代码兼容层，同时保留用户数据与安全协议应有的历史解释能力；每一类前缀都按它实际承担的职责决定，而不是按大小写机械替换。
