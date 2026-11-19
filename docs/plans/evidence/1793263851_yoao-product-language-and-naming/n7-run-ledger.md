# N7 产品语言定向验收账本

## 2026-11-19：CLI 导入与清理回执

从 `feat/backend-opt@2df1c6286` 分出 `feat/product-language-n7`；没有更改 T1／T2 的冻结候选 `3d45da957`。范围是 `yoao predictions import`、`yoao annotations import` 和 `yoao predictions purge` 的帮助与文本回执，不改变服务端写入、API、确认、JSON 或退出码。

- **SDK / CLI：** `sdk.xml` 与 `annotation-preview.xml` 合计 14 个不同用例通过，无失败、无跳过。覆盖预览与实际计数、任务范围、未确认时不发请求、逐项错误和完整 JSON 回执；SDK 调用与 OpenAPI 快照匹配。HTTP 响应由 SDK 契约测试提供，不是本轮实库写入。
- **终端：** 独立工作树安装的 `yoao` 实际启动三个命令的 `--help`。另用阶段 C 的 `image-import-readback.json` 在本地回放同一组真实预测预览／确认回执；命令分别显示「预计写入 1 条待审预测」和「已写入 1 条待审预测」。这是当前客户端对历史回执的读取，不是重做数据导入。
- **指南：** 在实际 VitePress 页面从[预测导入指南](../../../../docs-site/user-guide/datasets/prediction-import-export.md)点击进入 [CLI 参考](../../../../docs-site/dev/sdk/cli.md#yoao-predictions)。两页均检查明暗主题及 1280／390 像素宽度，共 8 张截图，无页面横向溢出或浏览器脚本异常。开发预览的 `/favicon.ico` 返回 404；首次只准备文档依赖时，预生成缺少 Web 的 TypeScript，使用本树冻结依赖后预览成功。

本地 JUnit、命令输出、回放来源摘要、浏览器记录和截图位于本工作树 `.artifacts/n7-cli-receipts/`。临时回放服务、浏览器和文档预览均已停止；没有启动后端验收环境，也不计入 J1、N6 环境采用或发布资格。

## 2026-11-19：原生 JSON 导入说明与诊断

在 `feat/product-language-n7@aeb066a78` 上继续。原接口将当前文件称为旧格式，且预测与标注两条路径遇到结构错误均会回显上传值及校验器链接。修正两条 API 的说明、离线 CLI 目录、用户指南和错误提示，没有改选择器、文件身份、权限、必填条件、回执字段或状态码。

- **实库行为：** 用已提交的项目和操作者，通过真实 ASGI 路由分别上传同一份含 2 条预测和 1 条正式标注的文件。预览回执分别为 2／1，另开数据库连接读回均为 0；实际导入后读回 2／1。单个坏文件两条接口均返回 422，指出 `tasks[0].task_match` 而不带测试备注；坏文件与有效文件一起预览时，预测回执为预计写入 2、跳过 1，并指出坏文件名。
- **读取与契约：** 两条导入服务与 Mask 暂存预检定位到相同字段。UTF-8、语法行列、旧／新版本身份、跨字段约束、前三个位置与超出数量均有定向记录。实际 `/openapi.json` 与版本化快照及文档镜像一致；相对基线，只有两个导入操作的 `summary`、`description` 改变。两个 `yoao describe` 输出与快照一致，离线目录同时补齐上个切片三个命令的参数帮助。
- **文档：** 从[用户指南](../../../../docs-site/user-guide/datasets/prediction-import-export.md)点击到[导入响应](../../../../docs-site/api/guides/import.md#响应)，实际检查两页在 1280／390 像素及明暗主题下的 8 张截图，无页面横向溢出。开发预览的 `/favicon.ico` 仍为 404，没有脚本异常。
- **定向回归：** 18 个 HTTP、11 个文件身份／Mask 预检及 3 个 SDK 目录用例通过，共 32 个，无跳过。首次只启 PostgreSQL 的调用超时，未计入结果；单独定位到已有身份测试在同一事务内连续提交坏文件，真实回滚令下一次请求失去测试账号（401）。改为每个身份组合独立建夹具，保留全部 200／422 断言；没有修改鉴权或回滚逻辑。独立 PostgreSQL、Redis 就绪后，最终 HTTP 组用时 15.50 秒。

JUnit、首次失败、实库读回、快照差异及截图保存在 `.artifacts/n7-native-import/`。一次性数据库、Redis、文档预览、浏览器和额外的契约生成虚拟环境已退役。没有触碰 T1／T2 的冻结环境、日常数据库或生产资源。

## 2026-11-19：导出产物与插件仓库复核

**验收边界。** 本节记录平台功能分支 `fef2aace8` 上的初次复核：当时未改两个独立仓库或发布制品。本地候选、公开默认分支和正式发行分别核对；后续本地修复和复验结果见下一节。

**作者工具 Plugin-SDK**

- 本地 `8daeb0cf0` 的三个 wheel 与 manifest 所列大小及 SHA-256 一致。作者发行名 `yoao-inference-plugin-sdk`，导入 `yoao_inference_plugin`，命令 `yoao-plugin`；运行时 `yoao-backend-runtime`，Mask 发行名 `yoao-mask-utils`（领域导入仍为 `mask_utils`）。
- [公开默认分支](https://github.com/YOAO-Labs/Plugin-SDK/blob/46ae0c4f5f03bd2e5fe1078bc462620f63d81df4/README.md#L15-L42)和 [Release manifest](https://github.com/YOAO-Labs/Plugin-SDK/releases/download/python-sdk-46ae0c4f5f03/manifest.json) 仍对应 `aap-inference-plugin-sdk`、`aap-backend-runtime`、`anno-mask-utils`。实际旧命令是 `aap-inference-plugin`，不是 `aap-plugin`；公开脚手架仍固定旧制品。

**官方实现 YOAO-Plugins**

- 本地 `7167a2f3b` 包含 N5 已验的 `daf31e0c7`。六个发行名均为 `yoao-<模型>-plugin`；LibreYOLO 的 native 包及独立工具命令也已更名。
- [公开默认分支](https://github.com/YOAO-Labs/YOAO-Plugins/tree/e489ff1d4b7c2ec7acabdbe4b10f3663bbd706a0/plugins/inference)仍使用 `yolo-backend`、`sam3-backend`、`aap-libreyolo-plugin` 等旧名。GitHub 标记 [Latest 的 Grounded-SAM2](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/python-245bed115c68) 和[发布时间更晚的 SAM3](https://github.com/YOAO-Labs/YOAO-Plugins/releases/tag/python-e489ff1d4b7c) 均是旧制品，不是六插件的新名发行。

**实测、保留项与验收门**

- **作者真实路径：** 从已核验的第一方 wheel 和匹配 CPython 3.11 的本地依赖离线安装，未使用源码导入。`yoao-plugin init` 生成七个 `yoao-*` 第一方依赖；`build`、安装示例 wheel、`dev --device cpu`、`check --url` 均成功。独立红／绿图及空图的类别、坐标与分数通过；仅设置 `YOAO_PLUGIN_TOKEN` 会拒绝，按指南设置现行 `AAP_PLUGIN_TOKEN` 才成功。没有测试 GPU、构建 OCI 或证明公共下载可用。
- **跨仓仍待做：** 两仓根 README 尚未展开品牌全称。SDK 的 README 把候选契约称为已发布，运行时说明称可以省略实际必装的 HTTPX／Pillow，Mask 指南仍将两个官方插件归为平台实现；`build --base-image` 的帮助将仅 pip 层离线说成整个镜像离线。官方 ONNXTools 指南把旧独立部署称为“内置服务”。两边候选文档不能用对方公开旧指引代替同修订说明；旧公开控制头与当前 `X-YOAO-*` 不可混装。`AAP_PLUGIN_TOKEN[_FILE]`、`AAP_PLUGIN_DATA_DIR` 和挂载路径仍有读者，要改须协调运行时；已签发 `typ`／`aud` 及历史 Release 保留原值。
- **本次平台补漏：** 新生成的图像／视频 YOLO `data.yaml`、四种点云导出说明及多相机 COCO 描述不再把产品写作 AAP；Mask 错误直接指明对象或摘要。导出指南区分选择器 `native_json`、当前文件 `format: "yoao"` 和历史 `format: "aap"`；没有改旧文件、COCO 扩展键、nuScenes `v1.0-aap/` 目录或点云身份。仓库地图纠正 Grounded-SAM-2／SAM3 的归属，作者指南补全无隔离构建依赖；部署指南区分源码、经核验的候选制品与需另行取得的镜像、权重。平台 API 客户端 `yoao-sdk`／`yoao plugins` 与作者 CLI 分开说明。
- **平台验证与限制：** 实际生成并解析图像／视频的类别配置，读取四种点云包说明；3 个序列化／Mask 用例通过，另外 14 个导出用例中 3 通过、11 失败。失败用例都向主点云打包路径传入 ORM `DatasetItem`，其 `metadata` 是 SQLAlchemy `MetaData`，在 `packaging.py:231` 调用 `.get()` 失败，未到达本次更改的产物文案。未修改读写契约来绕过失败。`pnpm docs:build` 通过；导出 API 和仓库地图在 1280／390 像素、明暗主题共 8 张实页截图无横向溢出，历史文件身份锚点可跳转。JUnit、截图及作者安装、构建、CPU HTTP 记录在 `.artifacts/n7-export-alignment/`。
- **联合门另算：** `t1-gate/joint-prereqs-3d45da957-3/joint-verdict.json`（SHA-256 `d473b82c268ac44d8c2c253d4f1dabd16dd59e6265d7dba0c17052424900bdbe`）记录默认运行在 `profile-ordinary-cleanup` 以 `CleanupError` 收尾，`strict_default_settled=false`。其中 T2 的 44 项为 **28 通过、14 错误、2 未运行**；此前 `2206f0722` 的 44/44 属于另一候选的三次定向调用，不是这轮通过。N6-S／N6-E 等待 J1 和各自专项批准；当前存储分配前缀 `_aap_managed/export/` 与工作树模式变量 `AAP_WORKTREE_MODE` 不是可单改文案的漏项。

## 2026-11-19：修复跨仓作者说明与点云 ZIP 夹具

- **Plugin-SDK（本地基线 `8daeb0cf0`）：** 两类命令和仓库职责在入口分开说明。运行时指南按 wheel 声明列出 HTTPX、Pillow；Mask 指南指向官方插件仓。作者指南要求把无隔离构建工具装入当前环境，使用现行的 `AAP_PLUGIN_TOKEN`，并与实际 `build --help` 一致说明仅 pip 层禁网、APT 使用配置的软件源。
- **安装验证：** 从修改后的工作树另建作者 wheel（SHA-256 `3b46bdff06ecaef7d8694cf94f82b7c33bdb40825b4e62743d68c528db0e02d3`），不覆盖 N5 已验 wheel。在独立 CPython 3.11 环境从 34 个带哈希的本地 wheel 安装；新 CLI 的 `init`／`build` 生成并安装了含七条第一方依赖的作者插件。通过 loopback 的 8 项 HTTP 检查，包括多目标、空图、类别／坐标／分数、鉴权和无效输入拒绝；未安装 Torch，未验证 OCI 或 GPU。
- **YOAO-Plugins（本地基线 `7167a2f3b`）：** 入口给出品牌全称、六个官方实现、同修订 SDK 说明及哈希锁安装命令；区分第一方发布包与含第三方 wheel 的私有候选。ONNX Tools 指南区分独立部署、原生 CPU 和原生 CUDA，保留实际读取的配置键。按文档用两个 N5 manifest 取回其四个第一方 wheel，摘要与 `uv.lock` 一致；镜像的 `requirements-firstparty.txt` 单独锁定其中的运行时，其余三包由平台契约 manifest 校验。没有替换锁、镜像或权重。
- **点云 ZIP：** 旧夹具把 ORM `DatasetItem` 直接交给冻结打包器。现在与生产一致，用 `row_value` 冻结、用 `_detached_item` 还原供打包读取的 `metadata`；保存快照的字段仍叫 `metadata_`。历史签名用例按真实 Workspace 读取入口提供项目，并在签名前核对每个对象经过归属检查。`test_lidar_media_packaging.py` 和 `test_export_contracts.py` 共 **31 通过，0 失败／跳过**，含初次失败的全部 11 项；覆盖真实 ZIP、逻辑路径、旧传输、无主点云与失效回执。未改生产数据读取或签名路径。
- **读者入口与边界：** 两仓六份 README 的相对链接已核对；浏览器从平台模块部署页实际打开了 `/dev/ml-backend/starter`。JUnit、构建输出、安装包摘要、HTTP 报告和页面截图见 `.artifacts/n7-plugin-repair/`。两仓仅本地修改，未发布；已签发身份和 `AAP_PLUGIN_*` 运行键不改。本轮不重跑、不覆盖 T1／T2 冻结裁决，不放行 J1、N6 的存储／环境切换或 J2。

## 2026-11-19：提交身份、作者制品与服务认证入口

- **三仓本地提交：** 平台 `15de574fce`（导出用语、点云冻结夹具），Plugin-SDK `549726f3a0`（作者工具与依赖说明），YOAO-Plugins `ab3d30e9e5`（官方实现与安装说明）。上一节的 CPU HTTP 验收使用修复工作树构建的作者 wheel，不是下面的提交制品。
- **从提交构建：** 在独立 CPython 3.11 环境以官方 `build-sdk-artifacts.py --revision 549726f3a0328ccf6073e3ae203af5fe47d53810` 生成源码归档、三个 wheel 和三个 sdist。按 manifest 核对七个文件的大小与 SHA-256，另校验 manifest 本身。逐个从 sdist 再造 wheel，与对应的直接构建逐成员比较，48 个成员内容全部相同；唯一的命令入口是作者包的 `yoao-plugin`。直接构建的作者 wheel SHA-256 为 `00f66573a4419419108ddf86c885879f3c223d70650d0ce36ac1213536cf4e90`。未覆盖 N5 冻结输入、改官方插件依赖锁或上传发布资产。
- **管理员操作：** 设置页按 `ML_MANAGED_BACKEND_URLS` 区分可信地址与已安装插件；注册表不可新选未配置的托管方式，不静默改写既有实例。手动 Token、无认证、受控安装实例自己的凭据仍是不同路径。对照宿主机 runner 修正了界面、用户指南和运维手册：包括两个 SAM 在内的五种 Compose 服务，都先拉取并核验固定镜像，失败不停止原容器。
- **定向回归：** 设置页 15 项、注册表最终 13 项、项目模型服务认证与角色入口 5 项通过；项目模型文件其余 52 项不在选择内。凭据与维护另有 35 项通过，合计 68 个不同用例。包含可信地址读取失败时保留手动认证、旧实例不被静默改写、非可信地址拒绝、SAM 镜像拒绝及停止顺序。维护测试拦截 Docker 命令，未停真实服务。Web 主题 token 检查、Web 和文档构建通过；浏览器从协议页跳入实际 `/dev/ml-backend/starter#使用自定义推理-sdk` 标题。
- **真实登录与 API：** 在独立 PostgreSQL（`bo0474`）、Redis 和 MinIO 上用超管账号登录。仅配置 YOLO 时，设置页只列一行；点击初始化得到 HTTP 200，没有新建实例。直接指定 SAM 3 返回 422，前后状态相同。注册弹窗只有 YOLO 托管选项可用，使用部署地址后 URL 与返回值一致；切换到手动方式出现令牌输入框，切到无认证后消失。将同一验收 API 的可信映射改为空，设置页显示配置方法并禁用批量按钮；刷新仅发 GET，之前的 YOLO 也被 API 以 422 拒绝。
- **浏览器边界：** 设置页与注册弹窗均在 1280／390 像素、明暗主题实测，共 8 个最终视图，无横向溢出。此部署未接 GPU authority，`gpu-resources` 和实例列表 `/all` 返回 503；实际从「注册管理 → 服务池 → 注册实例」进入，未伪造可用的 GPU 或维护器。初次开发服务器导航出现动态模块加载错误，切换配置重启 API 时旧页有代理错误；保留原始记录。最终构建产物的设置页无失败请求，注册页仍如实提示 GPU 不可用。未向远端模型服务握手或执行真实容器维护。
- **证据与后续：** `.artifacts/n7-trusted-services/` 保留 JUnit、HTTP 回执、浏览器矩阵、SDK manifest 和 sdist 对照。一次性服务、数据库与凭据已退役。公开默认分支、Release、OCI 和插件锁未变；此批不重跑 T1／T2，不放行 J1、N6-S／E、J2 或 N7 整体。

## 2026-11-19：账号邮件与原生 JSON 的当前入口

- **实际收信：** 从当前源码构建 Identity wheel（SHA-256 `61da8d1003838f67cbfcd08ffdb76bf58bfc2a01d09addb79b3a3c0da2fde1d2`），通过普通 API 数据库账号启动本地 HTTP 服务。管理员保存 SMTP 设置后，回环收件箱收到测试、注册验证、密码重置、账号邀请和项目邀请共五封 UTF-8 邮件。主题均以 `[YOAO]` 开头；`From` 是保存的发件地址，`To` 是实际目标邮箱。没有连接外部邮件服务。
- **链接与身份：** 四封带链接的邮件各自保留站点子路径，UTC 到期时间和 `Message-ID` 均与对应 outbox 记录相符。验证和重置首次使用均返回 200，再用返回 400；重置后用新密码登录成功。两个邀请都能从邮件里的原 token 读回；只有项目邀请有项目职责，用户自定项目名中的旧称未被替换。API 角色能执行两套领取函数，不能直接读取 outbox；比对由独立的验收连接完成。
- **原生 JSON：** 当前说明与新作业样例使用 `native_json`；历史作业仍可显示原生 JSON 并下载。真实 HTTP 预览：当前 `yoao` 文件和未改写的历史 `aap` 文件均为 200，把历史文件身份改成 `yoao` 为 422。另用实库验证项目导出、批次范围及历史预测导入后从当前导出读回，已有正式标注不变。冻结的 API 测试文件名、pytest 选择器和历史文件样本未改动。
- **证据与边界：** Identity 两套 worker 回归 13 项、原生文件 3 项、任务铃新旧作业 2 项通过。文档构建通过，浏览器从构建站点打开了快速开始、用户与权限、系统设置三份指南。`.artifacts/n7-account-mail/` 留存脱敏 HTTP／SMTP 回执、JUnit、wheel 和退役记录；一次性 PostgreSQL、Redis、匿名卷、测试凭据、浏览器与预览均已退役。未重新拍摄正式媒体，也未重跑默认 T1／T2，不改变 J1、N6-S／E、J2 或发布状态。
- **提交工件：** 提交钩子调整了邀请目标表达式的换行。重新构建的 wheel（SHA-256 `672cb400693dd2d0b18e55bd33c4c3e0058cd6532caf8e2b507be438326df83c`）与收信验收工件逐成员比较，仅该 Python 文件及其 `RECORD` 摘要不同；两份 worker 的语法树相同。
