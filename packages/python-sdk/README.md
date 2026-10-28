# ai-annotation-sdk

AI 标注平台官方 Python SDK(beta)。同步 Client 覆盖 projects / datasets / tasks / annotations /
feedbacks / predictions / jobs / exports / ml_backends / batches / members / dashboard / api_keys 等稳定自动化工作流；
并提供全局 ML registry 与 service pool 运维；extras 提供 `aap` CLI 与终端监控面板。

在线学习命名空间覆盖冻结训练数据、训练实验与观测、配方和增强预览、逐样本评估、受控模型回流与发布、学习策略、有预算的参数搜索和人工主动学习；CLI 使用 `aap learning`，TUI 提供按需加载的训练监控。

SDK、CLI 与 TUI 使用独立于 AAP 的 SemVer。AAP target 表示该 SDK release 最近一次完成测试与
OpenAPI 对账的平台基线，不代表未经验证的完整兼容范围。主仓库发布新版本时无需同步更新该字段，
也不会仅因 target 落后于当前主仓库版本而让契约测试失败；定期优化 SDK 时再更新它：

```python
from ai_annotation import __aap_target_version__, __version__

print(__version__)
print(__aap_target_version__)
```

```bash
aap --version  # aap <sdk-version>@AAP<aap-target-version>
```

## 安装

```bash
pip install ai-annotation-sdk            # 核心 (httpx + pydantic)
pip install 'ai-annotation-sdk[cli]'     # + aap 命令行 (typer + rich)
pip install 'ai-annotation-sdk[tui]'     # + aap tui 终端面板 (textual, 隐含 cli)
```

TUI 支持 Textual 8.x，启动时只加载 Projects、Jobs 与当前主体；Datasets、ML、看板和绩效按首次访问加载。
列表提供筛选与分页，详情可查看 Dataset items、Batch、项目 Pool，并可确认后重试 failed Job。

## 本机 GPU 推理插件安装

`aap plugins` 由平台超级管理员在 Docker 执行机运行；平台 API 不访问 Docker socket。先从
LibreYOLO 插件发布流程取得真实构建产物的固定 OCI 摘要及生成的 manifest，并将该摘要镜像
加载到执行机的 Docker。执行机必须具备 NVIDIA Container Toolkit、显式授权的单张物理 GPU、
受批准的 Docker 网络，以及平台 API 可访问的插件 URL。不要使用浮动标签、host 网络、平台
数据库凭据或 GPU 签发私钥。请以可访问 Docker 的**非 root**用户运行；容器使用该用户的
UID/GID 读取仅该用户可访问的状态目录。

准备一个管理员审批的 JSON 配置文件：`manifest` 为构建生成的完整 manifest 对象；另含
`name`、`url`（平台可访问的插件 HTTP(S) URL）、`gpu_resource_id`、
`physical_gpu_uuid`（`GPU-...`）、`network`、`host_port`、`cpus`、
`gpu_verify_keyring`（key ID 到 Ed25519 公钥的 unpadded base64url 映射）和
`image_hosts`（允许下载图片的主机名列表）。manifest 的 `allowed_asset_sources` 是批准的
资产源 origin 列表。`host_port` 暴露容器的 8000 端口；确保 URL 指向这个实例，且网络策略
允许核心访问它。配置仅含公钥，不含业务 Token。

```bash
aap plugins prepare --config deployment.json --state-dir ./libreyolo-state --json
# 以下显式步骤会从批准的来源准备并核验默认 t 资产；不会在预测时隐式下载。
aap plugins prepare-assets --state-dir ./libreyolo-state --json
# 先取得与该 Registry、GPU、镜像和 runtime 绑定的完整、通过的 GPU 生命周期证据。
aap plugins install --state-dir ./libreyolo-state --qualification gpu-report.json --yes --json
aap plugins status --state-dir ./libreyolo-state --json
```

`prepare` 先持久化随机安装身份和独立 Token，再预留不可接流的 Registry/GPU 绑定。
重试必须使用相同状态目录和配置；该目录及其中的收据、Token、公钥文件和资产目录为私有
状态，不能复制给其他安装或丢失。`install` 核验本机镜像摘要、镜像 ID、容器所有权、
GPU/资源及挂载，并向核心提交可信管理员的本机检查收据；这不是远程加密证明。核心仍需
独立验证可达性、健康、setup 和运行事实后才激活；安装不自动启用任何项目。项目负责人仍须
通过现有 `aap ml-backends` 或项目设置显式启用。若激活失败，容器保持原状且不自动接流；
修复原因后重试同一状态目录。已激活实例的容器若消失或停止，不会未经重新资格检查而重启。
真实 GPU 生命周期资格与镜像收据不同：超级管理员须提交完整、通过的共享生命周期报告。
报告必须绑定这次 `prepare` 返回的 Registry ID、GPU 资源、制品和运行时；安装器将其规范
JSON 内容及 SHA-256 提交核心，并以只读文件挂载同一内容供插件验证。报告经首次持久化后，
重试可省略 `--qualification`；不能用另一个报告覆盖原身份。本命令不会自行声明资格。

升级时用**新**状态目录和新制品先运行 `prepare` 与 `prepare-assets`，取得绑定新身份的
完整报告，再执行 `aap plugins upgrade --from-state ./old-state --state-dir ./new-state
--qualification new-gpu-report.json --yes --json`。
这会验证并激活新身份，保留旧实例与已有冻结引用；模型需重新部署、试用并由负责人发布。
`aap plugins disable --state-dir ./libreyolo-state --yes --json` 先关闭核心准入，再等待
核心证明签名排空及全部运行结束，只有证明成功才停止容器。卸载使用
`aap plugins uninstall --state-dir ./libreyolo-state --yes --json`；活动依赖或未知资源
状态会阻止操作。退役保留历史 Registry、部署与发布记录，不删除历史数据；成功停止后移除
本地业务 Token，但保留用于查询历史身份的收据。失败后保留状态目录并重试，不要手工接管
名称相同但所有权不匹配的容器。`--yes` 只跳过确认，不覆盖授权、依赖或并发修订检查。

SDK core 仍只依赖 httpx 和 pydantic；`client.plugins` 提供类型化 prepare/list/get/activate/
disable/quiesce/retire，部署命令使用现有 CLI extra，不安装 Torch 或模型运行时。

## 快速上手

```python
from ai_annotation import Client

# base_url / api_key 也可走环境变量 AAP_BASE_URL / AAP_API_KEY
# 或 ~/.config/ai-annotation/config.toml (显式参数优先)
with Client(base_url="http://localhost:8000", api_key="ak_...") as client:
    # 建项目 + 数据集并关联
    project = client.projects.create(name="demo", data_type="image")
    dataset = client.datasets.create(name="demo-ds", data_type="image")
    client.datasets.upload_files(dataset.id, ["./imgs/a.jpg", "./imgs/b.jpg"])
    client.datasets.link_project(dataset.id, project.id)

    # 资源维护: 只发送显式给出的字段
    client.projects.update(project.id, name="demo-renamed")
    client.datasets.update(dataset.id, description="training images")
    batch = client.batches.create(project.id, "round-1", dataset_id=dataset.id)

    # 领任务并写标注
    task = client.tasks.next(project.id)
    client.annotations.create(
        task.id, "bbox",
        geometry={"type": "bbox", "x": 10, "y": 10, "width": 100, "height": 50},
        class_name="car",
    )

    # 导入外部预测
    client.predictions.import_file(project.id, "preds.json", format="aap_json")

    # 异步导出: 创建 → 等待 → 下载
    job_id = client.exports.create(project.id, targets=["coco"])
    job = client.exports.wait(job_id, timeout=600)
    client.exports.download(job, "./export.zip")
```

### 持久媒体回填

图片尺寸、缩略图和项目任务缩略图回填返回可回放的作业回执，而不是已处理条数或 Celery task ID。需要在响应丢失后重试时，先生成并保存 UUID 幂等键，重试时复用同一个值；省略该参数时 SDK 会为本次调用生成新键。`operation_id`、`job_id` 与 `status` 是回执字段；`pending` 表示作业已接受，不表示处理完成，SDK 和 CLI 都不会等待作业。尺寸回填的 `batch` 参数只为旧调用保留，服务端现在忽略它并自行有界分页。

```python
from uuid import uuid4

from ai_annotation import Client

# 假设 dataset 是上方快速入门中创建的数据集。
with Client(base_url="http://localhost:8000", api_key="ak_...") as client:
    key = uuid4()
    receipt = client.datasets.backfill_dimensions(
        dataset.id, idempotency_key=key
    )
    print(receipt.operation_id, receipt.job_id, receipt.status)
    # 响应不确定时，复用 key 调用同一操作。
```

`client.datasets.backfill_media()` 返回图片缩略图作业回执，同时保留数据集视频元信息回填入口；`client.files.backfill_thumbnails(project.id)` 返回项目任务缩略图作业回执。

错误统一抛 `ai_annotation.errors` 下的异常(`AuthenticationError` / `NotFoundError` / `JobFailedError` 等);响应模型 `extra="allow"`,容忍服务端新增字段。

### 项目角色与成员

平台把**账号身份(平台角色)**与**项目职责(项目角色)**分开:同一个账号可以在 A 项目标注、B 项目质检。项目访问来自成员关系,`client.projects.access()` 读取当前账号在某个项目上的解析后权限与能力集;`client.projects.list()` 的 `Project.my_project_role` 是当前账号的项目角色。

```python
with Client(base_url="http://localhost:8000", api_key="ak_...") as client:
    access = client.projects.access(project.id)
    print(access.platform_role, access.project_role, access.capabilities)

    # 成员输出是扁平的: role=项目角色, platform_role=平台角色
    for member in client.members.list(project.id):
        print(member.user_name, member.role, member.platform_role, member.version)

    # 角色变更: 先只读预检, 再按版本 + token 原子提交
    preview = client.members.preview_role_change(project.id, member.id, "reviewer")
    if not preview.blockers:
        client.members.change_role(
            project.id, member.id, "reviewer",
            expected_version=preview.current_version,
            preview_token=preview.preview_token,
            reason="rebalance workload",
        )
```

`role` 取值是项目角色 `annotator` / `reviewer` / `viewer`(平台 `viewer` 账号只能是只读项目角色);`Me.role` 是平台角色 `super_admin` / `project_admin` / `employee` / `viewer`。自审拒绝与全项目导出限制等完整规则见仓库文档站的可见性与权限说明。

## Issue 与反馈

`client.feedbacks.create()` 创建反馈，`client.feedbacks.list()` 按 `items` / `next_cursor` 翻页；`root_only=True` 只读根反馈，`include_counts=True` 同时读取可见范围的 `total` 和 `status_counts`。
任务级 Issue 使用 `anchor_type="task"`，不绑定像素或帧；图片像素锚继续只需 x/y。

视频像素 Issue 的上下文使用严格 V1 模型，并保留旧 `frame`。源帧范围是闭区间，必须包含
锚点帧；时间窗可用分数帧。viewport 中心允许在画面外，zoom 是当前缩放与适配缩放的比值。
服务端检查任务权限、视频边界及对象归属；`annotation_version` 保存捕获时版本，不要求
等于创建时的最新版本。非法上下文在发送前触发 Pydantic 校验错误；服务端错误沿用 SDK 异常映射。
下面的 `project_id`、`task_id`、`annotation_id` 分别使用目标项目、视频任务和已捕获标注的 ID。

```python
from ai_annotation import Client, FeedbackAnchorPosition, FeedbackVideoContext
from ai_annotation import FeedbackVideoFrameRange, FeedbackVideoTimelineWindow, FeedbackVideoViewport

with Client(base_url="http://localhost:8000", api_key="ak_...") as client:
    issue = client.feedbacks.create(
        project_id,
        "检查 F120–F160 的遮挡边界",
        task_id=task_id,
        annotation_id=annotation_id,
        anchor_type="pixel",
        anchor_position=FeedbackAnchorPosition(
            x=0.4, y=0.6, frame=140,
            video_context=FeedbackVideoContext(
                schema_version=1,
                track_id="car-track/left",
                annotation_version=2,
                frame_range=FeedbackVideoFrameRange(from_frame=120, to_frame=160),
                viewport=FeedbackVideoViewport(center_x=0.5, center_y=0.5, zoom=2),
                timeline_window=FeedbackVideoTimelineWindow(from_=110.5, to=170.25),
            ),
        ),
    )
    page = client.feedbacks.list(project_id, kind="issue", limit=50)
    while page.next_cursor:
        page = client.feedbacks.list(project_id, kind="issue", cursor=page.next_cursor)
```

响应的 `anchor_position` 保留完整原始字典；`issue.video_context` 只返回可识别的有效 V1，
未知版本返回 `None`，仍可读取旧 x/y/frame 做兼容定位。`feedbacks.thread()` 读取完整线程的一页后代；`reply()` 与 `patch()` 接收类型化请求，`delete()` 执行软删除。线程游标绑定根反馈，回复没有幂等键，不自动重试；补丁保留省略与显式 null。

## 通知与预标注

`client.notifications` 提供当前账号的有界通知列表、未读计数、已读处理、清理和偏好更新。偏好只修改明确给出的频道，保留其它设置。

通知的 `payload` 保留服务端 JSON 原值，可以是对象、数组、字符串、数字、布尔值或 `null`；省略该字段时仍默认为空对象。REST 列表、typed WebSocket 流和 `aap notifications list --json` 均不把旧值强制转换成对象。

`client.pipelines` 管理可见编排库；`apply()` 将模板复制到项目，`preannotate()` 才提交推理。使用明确的范围和稳定的 `Idempotency-Key`，通过现有 `jobs.get/wait` 查询受理后的状态。服务器继续校验项目／组织权限、backend 启用、模型能力、任务归属与锁状态，不提供独立的 dry-run。

```bash
aap notifications list --unread-only --limit 20 --json
aap feedbacks thread "$ROOT_ID" --limit 50 --json
aap pipelines list --scope private --project "$PROJECT_ID" --json
aap pipelines preannotate --project "$PROJECT_ID" --body request.json --idempotency-key "$IDEMPOTENCY_KEY" --yes --json
aap jobs get "$JOB_ID" --json
```

详见 `docs-site/dev/sdk/workflows.md`；所有新操作也可通过随包离线目录读取请求 schema 与限制。

## Data Explorer 与保存视图

`client.data_manager` 读取项目 schema、聚合、对象／轨迹与定位，提供选中任务的分配预览／应用和导出；`client.task_views` 保存／复制筛选配置并执行任务查询。筛选保留动态字段与分组，不在客户端执行；查询、分配应用和导出各有独立权限与范围。

```bash
aap data-manager schema --project "$PROJECT_ID" --entity-scope objects --json
aap data-manager query-objects --project "$PROJECT_ID" --body query.json --json
aap task-views query --project "$PROJECT_ID" --body tasks.json --json
aap data-manager assignment-preview --project "$PROJECT_ID" --body selection.json --json
aap data-manager assignment-apply --project "$PROJECT_ID" --body apply.json --yes --json
aap data-manager export-tasks --project "$PROJECT_ID" --body export.json --idempotency-key "$IDEMPOTENCY_KEY" --yes --json
```

任务查询用 offset，实体查询用不透明 cursor；内置视图的 id 为空，不能传给保存视图 UUID 路由。分配提交保留 preview_version，选中任务导出保留精确任务集合与幂等键，不隐式等待或扩大范围。详见 `docs-site/dev/sdk/data-explorer.md`。

## 视频、项目模板与存储

使用 `client.videos`／`task_videos` 读取素材或任务媒体及管理分段租约，`video_tracker` 创建、恢复与审阅追踪，`video_review` 操作 Mask 内容、关键帧、质量与轨迹。frame/chunk 的 202 是 JSON 待就绪状态，读取可能触发生成；下载和轮询分别明确执行。候选接纳保留 revision/digest/source versions，Mask 写入携带 If-Match，变更不自动重试。

`client.project_templates` 管理模板；`projects.create(name, template_id=...)` 或 `source_project_id=...` 继承服务端配置与类型，明确覆盖优先。`client.storage` 提供 bucket 诊断及管理员视频失败查询／重试，queued 只代表已排队。对应 CLI 为 `videos`、`task-videos`、`video-tracker`、`video-review`、`templates`、`storage`。详见 `docs-site/dev/sdk/video-and-templates.md`。

公开 REST 覆盖路线包括运维与管理员操作，仅内部执行器训练协议排除；尚未实现的公开接口继续明确列为 uncovered。

项目、批次、数据集和看板补齐分页／维护、预览分派、送审／锁定、回填与导入；`project_performance`、`search`、`guide_assets`、`failed_predictions` 提供绩效证据、搜索、指引图片三步上传和失败恢复。LiDAR 项目／批次预检共用模型，标注导入保留同步部分回执。完整 CLI 和限制见 `docs-site/dev/sdk/project-workflows.md`。

账户与运维入口提供 `auth`、`account`、`avatars`、`users`、`groups`、`invitations`、`health`、`ml_capabilities`、`system_settings`、`audit_logs`、`storage_connections`、`admin_operations`、`ml_diagnostics` 和 `project_ml`；`api_keys.patch` 保留显式 null 清除有效期。对应 CLI 使用同名 kebab-case 命令组。先用 `describe` 查看精确参数；敏感输出、部分失败退出码、权限与真实网络／邮件边界见 `docs-site/dev/sdk/account-and-operations.md`。

`client.files` 提供初始化、无平台凭据的签名字节上传、明确完成和任务文件 URL；`client.scenes` 读取数据集场景／有序位姿并执行管理员元数据更新。对应命令为 `aap files` 和 `aap scenes`，权限、分步恢复及 null 语义见 `docs-site/dev/sdk/files-and-scenes.md`。

## CLI

需要 `[cli]` extras。首次使用先登录(验证连通后写入 `~/.config/ai-annotation/config.toml`,权限 0600):

```bash
aap login --url http://localhost:8000 --api-key ak_...   # 省略 --api-key 则隐藏输入

aap projects list                                  # rich 表格;加 --json 输出裸 JSON
aap projects create --name demo --type image       # image|video|lidar
aap projects update <project-id> --name demo-v2

aap datasets create --name demo-ds
aap datasets upload <dataset-id> ./imgs            # 目录/单文件逐个上传(进度条)
aap datasets upload <dataset-id> ./data.zip --zip --idempotency-key <saved-uuid>
aap datasets scan-items <dataset-id> --idempotency-key <saved-uuid> --yes
aap datasets link <dataset-id> <project-id>        # 异步建任务时自动等待 job
aap datasets preview-unlink <dataset-id> <project-id>
aap datasets unlink <dataset-id> <project-id> --yes --idempotency-key <saved-uuid>

aap batches create <project-id> --name round-1 --dataset-id <dataset-id>
aap members list <project-id>                    # 项目角色 / 平台角色分列
aap members add <project-id> --user-id <user-id> --role annotator   # 项目角色: annotator|reviewer|viewer
aap members preview-role <project-id> <member-id> --role reviewer   # 只读预检, 返回 token 与阻塞项
aap members change-role <project-id> <member-id> --role reviewer \
  --expected-version 2 --preview-token <tok> --reason "重新分配职责" --yes

aap predictions import <project-id> result.json --format aap_json [--dry-run]
aap jobs wait <job-id>                             # 进度条跟随到终态
aap export project <project-id> --target aap_json --out out.zip

aap ml-backends list --project <project-id>        # 列本项目已启用的 ML Backend + 健康状态
aap ml-backends get <backend-id> --project <project-id>
aap ml-backends available --project <project-id>   # 列全局 backend 及本项目启用态

aap ml-registry list                               # super-admin: 物理 backend registry
aap service-pools list                             # super-admin: 逻辑 service pool
aap service-pools topology --json                  # 路由拓扑的机器可读快照

aap tui        # 按需加载的终端监控与轻运维台；需要 [tui] extras
```

ZIP 上传和存储扫描均为同步操作。调用前生成并保存一个 UUID；响应不确定时继续传入相同的
`--idempotency-key`。SDK 和 CLI 不会自动生成键，也不会自动重试这两个 POST。

所有命令支持 `--json`:输出裸 JSON、无 rich 装饰/进度条,退出码非 0 表示失败,供 CI/脚本使用。破坏性命令的 JSON 模式必须显式传 `--yes`；本地确认不会绕过服务端权限。错误默认写到 stderr 文本提示；根选项 `--error-format json` 可选择结构化错误，成功输出与退出码约定不变。

`ml-backends list` 只返回本项目已启用的 backend；`MLBackend.id` 是全局物理 registry ID，同一实例
在不同项目中保持同一 ID。service pool 是独立的逻辑路由身份，使用 `service-pools` 命令管理，不与
registry ID 混用。删除、卸载、drain 和能力漂移接受等保护操作需要显式确认，且不会绕过服务端的
409/503 静默守卫。

## 在线学习自动化

项目级接口要求项目管理权限以及 `annotations:read`、`datasets:read` scope，写接口还需要 `datasets:write`。全局 `client.training` 还要求平台角色为 `project_admin` 或 `super_admin`。

新建接口接收顶层导出的 Pydantic 请求对象，例如 `TrainingDatasetCreate`、`TrainingRunCreate`、`TrainedModelCreateRequest`、`TrainingStudyCreate` 和 `ActiveLearningRoundCreate`。预检／预览与提交分开执行；重放同一请求时复用原 `request_id` 和原始请求体。响应 `202` 只表示已受理，训练状态从 run/attempt 读取，不能把 `async_job_id` 当作训练完成证明。

```bash
aap learning training catalog --json
aap learning task-datasets list --project "$PROJECT_ID" --json
aap learning datasets preflight --project "$PROJECT_ID" --body selection.json --json
aap learning datasets create --project "$PROJECT_ID" --body dataset-request.json --json
aap learning runs create --project "$PROJECT_ID" --body run-request.json --json
aap learning runs get "$RUN_ID" --project "$PROJECT_ID" --json
aap learning runs logs "$RUN_ID" "$ATTEMPT_ID" --project "$PROJECT_ID" --after 0 --json
```

`--body` 必须是 UTF-8 JSON 对象，包含接口要求的幂等 ID、预检摘要、修订或显式预算；`--body -` 从 stdin 读取，应用相同校验。删除、取消、重试、恢复、部署、发布、策略变更、参数搜索与人工决策需要交互确认；JSON 模式需传 `--yes`。下载使用 `--out`，校验可用的 SHA-256／大小后原子替换目标文件，中断和校验失败不会覆盖已有文件。

方法、分页和完整示例见 `docs-site/dev/sdk/python-client.md`、`cli.md` 与 `cookbook.md`。执行器 claim、heartbeat、uploads 等内部协议不属于用户 SDK。

## Agent 能力发现与导出

```bash
aap capabilities --status covered --limit 20 --json
aap describe learning.runs.create --json
aap --error-format json learning runs create --project "$PROJECT_ID" --body - --json < run-request.json
```

核心 Python 包提供 `list_capabilities()` 和 `describe_operation()`，无需连接 API 或安装 CLI/TUI extras。目录随 wheel 分发，全量记录 API 的已覆盖、主动排除和尚未接入状态；描述包含参数、请求 schema 与操作级要求。覆盖路由不等于每个业务分支都完整实现，具体限制和服务器资格仍需核对。

项目与批次异步导出使用 `body=ExportRequestBody(...)` 传视频范围和点云选项，不能将 `scope` / `lidar` 塞入 query kwargs。同步 VOC 使用 `exports.download_voc()` / `batches.download_voc()` 或对应 CLI 下载命令，没有异步 Job ID。完整操作说明见 `docs-site/dev/sdk/agents.md`；安装包还附带英文 `ai_annotation/AGENT_GUIDE.md`。

## 测试

```bash
pip install -e '.[test]'
pytest
```

其中 `tests/test_openapi_contract.py` 会从 SDK 资源模块的真实 HTTP/流式调用点提取 method/path，与
`apps/api/openapi.snapshot.json` 对账，并检查 `api-coverage.toml` 中全量 API 的端点分类
(仅 monorepo 内生效)。

## 更多文档

参见仓库文档站 `docs-site/dev/sdk/`。

新增 `comments`、`annotation_history`、`bug_reports`、`mask_formats`、`mask_qc`、`point_cloud_quality`。讨论与历史沿用实际任务权限，格式导入／修复保留精确回执和摘要，点云评估晋升校验基线版本。完整工作流见 `docs-site/dev/sdk/annotation-quality.md`。

SDK 覆盖全部 558 项稳定公开 REST（含运维），十项执行器私有操作排除，另提供六条公开 WebSocket。任务、租约、相机／轨迹和几何提交见 [任务工作流](../../docs-site/dev/sdk/task-workflows.md)；可选 `streams` extra 与有界 NDJSON 监听见 [公开事件流](../../docs-site/dev/sdk/streams.md)。

上传需要自行处理响应中断时，可使用 `client.datasets.upload_init(dataset_id, file_name, content_type, idempotency_key=init_key)` 与 `client.datasets.upload_complete(dataset_id, item_id, idempotency_key=complete_key)`。两个阶段分别保存自己的 UUID，完成前复用初始化键、完成重试复用原条目与完成键；SDK 不自动重试这些写入。`upload_files()` 继续提供初始化、裸签名 PUT、完成的便利流程，并为每个阶段生成 UUID。
