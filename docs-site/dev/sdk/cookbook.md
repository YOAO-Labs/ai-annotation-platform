---
title: SDK Cookbook
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-19
---

# SDK Cookbook

以下片段展示上传、标注、导出和在线学习流程。凭据均假定已通过 `aap login` 或环境变量 `AAP_BASE_URL` / `AAP_API_KEY` 配置(`Client()` 无参构造时自动回落),也可以显式传 `Client(base_url=..., api_key=...)`。

## 1. 上传数据并创建项目

创建数据集 → 逐文件上传(带进度回调)→ 创建项目 → 关联;关联走异步建任务时等待 job 完成。

```python
from pathlib import Path

from ai_annotation import Client

IMAGE_DIR = Path("./imgs")

with Client() as client:
    dataset = client.datasets.create(name="demo-ds", data_type="image")

    files = sorted(p for p in IMAGE_DIR.rglob("*") if p.is_file())
    items = client.datasets.upload_files(
        dataset.id,
        files,
        on_progress=lambda done, total, name: print(f"[{done}/{total}] {name}"),
    )
    print(f"上传完成: {len(items)} 个文件")

    project = client.projects.create(name="demo", data_type="image")
    link = client.datasets.link_project(dataset.id, project.id)
    if link.async_job_id is not None:
        # 大数据集走异步建任务, 轮询到终态
        client.jobs.wait(link.async_job_id)
        print("异步建任务完成")
    else:
        print(f"关联完成: created_tasks={link.created_tasks}")
```

## 2. 批量导入预测(先 dry_run 校验)

`dry_run=True` 只校验不落库;校验干净后再正式导入。注意 SDK 的 `overwrite_existing` 缺省 `False`(不覆盖已有预测),要覆盖需显式传 `True`。

```python
import sys

from ai_annotation import Client

PROJECT_ID = "<project-id>"
PREDS_FILE = "preds.json"

with Client() as client:
    # 第一遍: 只校验
    check = client.predictions.import_file(
        PROJECT_ID, PREDS_FILE, format="aap_json", dry_run=True
    )
    if check.errors:
        print(f"dry-run 发现 {len(check.errors)} 个错误, 中止导入:")
        for err in check.errors[:10]:
            print(f"  - {err}")
        sys.exit(1)
    print(f"dry-run 通过: 可导入 {check.imported} 条, 跳过 {check.skipped} 条")

    # 第二遍: 正式落库
    result = client.predictions.import_file(
        PROJECT_ID, PREDS_FILE, format="aap_json"
    )
    print(f"导入完成: imported={result.imported} skipped={result.skipped}")
```

## 3. 等待导出完成并下载(带进度回调)

`exports.create` 返回 job_id;`exports.wait` 每 2 秒轮询一次,每轮先调 `on_progress(job)`;job 失败抛 `JobFailedError`,超时抛 `JobTimeoutError`。

```python
from ai_annotation import Client, JobFailedError, JobTimeoutError
from ai_annotation.models import Job

PROJECT_ID = "<project-id>"


def show_progress(job: Job) -> None:
    print(f"\r{job.kind} · {job.status} · {job.progress_pct}%", end="", flush=True)


with Client() as client:
    job_id = client.exports.create(PROJECT_ID, targets=["aap_json"])
    print(f"导出 job 已创建: {job_id}")
    try:
        job = client.exports.wait(job_id, timeout=600, on_progress=show_progress)
    except JobFailedError as e:
        print(f"\n导出失败: {e.job.error_message}")
        raise
    except JobTimeoutError:
        print("\n导出超时, 稍后可用 client.jobs.get(job_id) 继续查询")
        raise
    print()
    dest = client.exports.download(job, "./export.zip")
    print(f"导出包已下载 → {dest}")
```

## 4. 批次分配、提交、审核与导出

下面的 Python 和 CLI 使用同一组 project / dataset / user / task ID，依次完成创建批次 → 分配 → 提交 → 审核 → 导出 → 等待 job。

```python
from ai_annotation import Client

PROJECT_ID = "<project-id>"
DATASET_ID = "<dataset-id>"
ANNOTATOR_ID = "<annotator-user-id>"
REVIEWER_ID = "<reviewer-user-id>"
TASK_ID = "<task-id>"

with Client() as client:
    batch = client.batches.create(PROJECT_ID, "round-1", dataset_id=DATASET_ID)
    client.batches.distribute(
        PROJECT_ID,
        annotator_ids=[ANNOTATOR_ID],
        reviewer_ids=[REVIEWER_ID],
        only_unassigned=True,
    )

    client.tasks.submit(TASK_ID)
    client.tasks.claim_review(TASK_ID)
    client.tasks.approve_review(TASK_ID)

    job_id = client.batches.export(
        PROJECT_ID, batch.id, targets=["aap_json"]
    )
    job = client.jobs.wait(job_id)
    client.exports.download(job, "./batch-export.zip")
```

```bash
batch_id=$(aap batches create "$PROJECT_ID" --name round-1 \
  --dataset-id "$DATASET_ID" --json | jq -r '.id')
aap batches distribute "$PROJECT_ID" \
  --annotator-id "$ANNOTATOR_ID" --reviewer-id "$REVIEWER_ID" --json

aap tasks submit "$TASK_ID" --json
aap tasks review-claim "$TASK_ID" --json
aap tasks review-approve "$TASK_ID" --json

job_id=$(aap batches export "$PROJECT_ID" "$batch_id" \
  --target aap_json --json | jq -r '.job_id')
aap jobs wait "$job_id" --json
```

## 5. 从 echo 示例改出一个 OCR backend

接入自定义模型推理服务属于 ML Backend 范畴,完整教程(echo 示例 → OCR backend → 注册到平台)见 [ML Backend 接入教程](/dev/ml-backend/starter)。

## 6. 冻结数据并提交训练

准备已有审核图片与矩形框类别的项目，将 `PROJECT_ID` 改为项目 UUID。脚本持久保存两次创建请求：网络响应不确定时重新运行会重放原请求，不会用新 ID 重复创建。修改来源或参数时使用新的请求文件；文件与项目绑定，不要跨项目复用。

该示例明确提交一次单轮训练，需要项目管理权限、相关 read/write scope 和就绪执行器。等待使用领域状态，超时不会取消远端任务；用资源 ID 继续查看详情。

```python
from pathlib import Path
import time
from uuid import uuid4

from ai_annotation import (
    Client, TrainingConfiguration, TrainingDatasetCreate,
    TrainingDatasetSelection, TrainingRunCreate,
)

PROJECT_ID = "<project-id>"
dataset_request_file = Path(f"{PROJECT_ID}.dataset-request.json")
run_request_file = Path(f"{PROJECT_ID}.run-request.json")

with Client() as client:
    if dataset_request_file.exists():
        data_request = TrainingDatasetCreate.model_validate_json(
            dataset_request_file.read_text(encoding="utf-8")
        )
    else:
        selection = TrainingDatasetSelection(task="detect", mode="reviewed")
        checked = client.training_datasets.preflight(PROJECT_ID, selection)
        if not checked.ready:
            raise RuntimeError(f"数据预检未通过: {checked.issues}")
        data_request = TrainingDatasetCreate(
            **selection.model_dump(mode="json", exclude_unset=True),
            name="reviewed-detection", request_id=uuid4(),
            preflight_digest=checked.digest,
        )
        dataset_request_file.write_text(
            data_request.model_dump_json(exclude_unset=True), encoding="utf-8"
        )
    version = client.training_datasets.create(PROJECT_ID, data_request)
    print("训练数据版本:", version.id)
    deadline = time.monotonic() + 600
    while version.status != "ready":
        if version.status in {"failed", "cancelled"}:
            raise RuntimeError(f"构建终止: {version.status}: {version.error_message}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"稍后继续查询数据版本 {version.id}")
        time.sleep(2)
        version = client.training_datasets.get(PROJECT_ID, version.id)
    client.training_datasets.download(PROJECT_ID, version.id, "./dataset.zip")

    if run_request_file.exists():
        run_request = TrainingRunCreate.model_validate_json(
            run_request_file.read_text(encoding="utf-8")
        )
    else:
        schema = client.training_runs.parameter_schema(
            PROJECT_ID, framework="ultralytics", base_model="yolo11n"
        )
        values = {
            field.key: field.default for field in schema.fields
            if field.status == "editable"
        }
        values.update(epochs=1, batch=1)
        draft = TrainingRunCreate(
            name="one-epoch", task="detect", dataset_id=version.id,
            framework="ultralytics", base_model="yolo11n", request_id=uuid4(),
            configuration=TrainingConfiguration(
                schema_id=schema.schema_id, schema_digest=schema.schema_digest,
                values=values,
            ),
        )
        checked = client.training_runs.preflight(PROJECT_ID, draft)
        run_request = draft.model_copy(update={"preflight_digest": checked.config_digest})
        run_request_file.write_text(
            run_request.model_dump_json(exclude_unset=True), encoding="utf-8"
        )
    run = client.training_runs.create(PROJECT_ID, run_request)
    print("训练实验:", run.id, run.status)
    deadline = time.monotonic() + 1800
    while True:
        detail = client.training_runs.get(PROJECT_ID, run.id)
        print(detail.run.status, detail.run.metrics)
        if detail.run.status == "succeeded":
            break
        if detail.run.status in {"failed", "cancelled", "lost"}:
            raise RuntimeError(f"实验需人工处理: {detail.run.status}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"稍后继续查询实验 {run.id}")
        time.sleep(2)
    attempt = next(a for a in detail.attempts if a.id == detail.run.current_attempt_id)
    weights = next(name for name, item in attempt.artifacts.items() if item["role"] == "weights")
    client.training_runs.download_artifact(PROJECT_ID, run.id, attempt.id, weights, "./weights.pt")
```

暂停与恢复是独立操作。先查看 `client.training_runs.recovery(PROJECT_ID, run_id)` 的 `eligible` 和 `reasons`，确认后再调用 `resume`；失联或停止中的实验可能仍占用资源。服务端返回冲突时保留结构化 `detail`，重新预检或处理来源，不能自动替换成基础权重。

## 7. 模型回流、评估和发布分开执行

训练成功后先核对 `registration_eligibility`。注册请求固定 `run_id`、`attempt_id` 和项目类别绑定；部署指定已认证的物理 `registry_id`。候选决策、部署成功和项目发布是不同动作，任何一步都不隐式完成下一步。

```python
from uuid import uuid4
from ai_annotation import Client, TrainedModelCreateRequest, ModelDeploymentCreateRequest

PROJECT_ID = "<project-id>"
RUN_ID = "<run-id>"
ATTEMPT_ID = "<attempt-id>"
REGISTRY_ID = "<registry-id>"

with Client() as client:
    detail = client.training_runs.get(PROJECT_ID, RUN_ID)
    eligibility = detail.registration_eligibility
    if eligibility is None or not eligibility.qualified:
        raise RuntimeError("请先核对实验的模型回流资格")
    request = TrainedModelCreateRequest(
        request_id=uuid4(), name="review-candidate", run_id=RUN_ID,
        attempt_id=ATTEMPT_ID, bindings=["car"],
    )
    # 首次提交前持久保存 request；重放同一注册时使用原对象。
    model = client.trained_models.create(PROJECT_ID, request)
    deployment = client.trained_models.deploy(
        PROJECT_ID, model.id, ModelDeploymentCreateRequest(registry_id=REGISTRY_ID)
    )
    print(model.id, deployment.id, deployment.status)
    qualification = client.sample_evaluations.qualification(
        PROJECT_ID, model_version_id=model.id, deployment_id=deployment.id
    )
    print(qualification.model_dump(mode="json"))
```

`bindings` 必须按真实项目类别填写。逐样本评估使用 `SampleEvaluationCreateRequest`，明确选定冻结数据版本、样本 ID、候选部署及阈值；随后按 evaluation/attempt 读取 `report`、`samples`、`source`。评估对照需同口径资格，未知指标不能用零替代。

发布时读取 `model_releases.list()` 的当前 generation，再显式提交 `ModelReleaseCreateRequest(expected_generation=..., model_version_id=..., deployment_id=..., sample_confirmed=True, request_id=...)`。409 表示当前发布已改变，需要人工刷新并重新确认。回退使用同一创建接口的 `rollback_from_id`，保留新的审计记录；候选归档不会替换当前发布。

## 8. 参数搜索与人工主动学习

两类流程都先预览，再带回原摘要提交；预算来自负责人明确选择。保存下列请求文件后再提交，重复运行创建步骤时加载相同文件。

```python
from pathlib import Path
from ai_annotation import Client, TrainingStudyCreate

PROJECT_ID = "<project-id>"
path = Path("study-request.json")
draft = TrainingStudyCreate.model_validate_json(path.read_text(encoding="utf-8"))
with Client() as client:
    preview = client.training_studies.preview(PROJECT_ID, draft)
    print(preview.plan)
    # 人工核对所有组合、目标和预算后，再保存并提交这个对象。
    request = draft.model_copy(update={"preview_digest": preview.preview_digest})
    Path("study-confirmed.json").write_text(
        request.model_dump_json(exclude_unset=True), encoding="utf-8"
    )
```

Study 请求包含完整配置的 `base`、有限 `grid`、框架实际提供的 `objective`、`direction="maximize"` 以及 `budgets.max_trials`、`max_planned_epochs`、`max_native_seconds`。创建调用为 `client.training_studies.create(PROJECT_ID, request)`，幂等 ID 使用 `base.request_id`；结果只在服务端认定可比的组内解释。

主动学习依次调用 `pool` → `preview(ActiveLearningSelection)` → `create(ActiveLearningRoundCreate)`，必须指定模型与部署、独立候选任务、随机种子、推理／审核预算和 `batch_size`。读取轮次及 candidates 后，由负责人通过 `decide(ActiveLearningDecisionCreate)` 明确接纳或拒绝序号；接纳只创建普通人工批次，不自动标注或发布。后续 `create_feedback(ActiveLearningFeedbackCreate)` 保存事实反馈与精确 evaluation/attempt 引用，未知工时或效率不会补算。

CLI 使用相同请求对象的 JSON 字段：

```bash
aap learning studies preview --project "$PROJECT_ID" --body study-request.json --json
aap learning studies create --project "$PROJECT_ID" --body study-confirmed.json --yes --json
aap learning active-learning preview --project "$PROJECT_ID" --body selection.json --json
aap learning active-learning create --project "$PROJECT_ID" --body round-request.json --yes --json
aap learning active-learning candidates "$ROUND_ID" --project "$PROJECT_ID" --json
aap learning active-learning decide "$ROUND_ID" --project "$PROJECT_ID" --body decision.json --yes --json
```

## 9. 范围导出与同步 VOC

视频范围与点云选项属于 JSON 请求体。以下示例选定一个视频任务的闭区间源帧范围；服务器继续核对任务归属、边界与格式资格。异步导出返回 Job ID，等待后再下载。

```python
from ai_annotation import (
    Client, ExportRequestBody, VideoExportScopeRequest, VideoExportFrameSelection,
)

PROJECT_ID = "<video-project-id>"
TASK_ID = "<video-task-id>"

with Client() as client:
    body = ExportRequestBody(scope=VideoExportScopeRequest(
        task_id=TASK_ID,
        selection=VideoExportFrameSelection(from_frame=120, to_frame=240),
    ))
    job_id = client.exports.create(PROJECT_ID, targets=["video_json"], body=body)
    job = client.exports.wait(job_id, timeout=600)
    client.exports.download(job, "./video-range.zip")
```

片段范围用 `VideoExportSegmentSelection(start_segment_id=..., end_segment_id=...)`；点云用 `ExportRequestBody(lidar=LidarExportOptions(kitti_camera_role="camera_front"))`。不要将 `scope` / `lidar` 当成普通 query kwargs。按批次导出时，将相同请求体传给 `client.batches.export(project_id, batch_id, body=body)`。

VOC 为同步 ZIP，使用专用方法；以下 ID 应属于服务器支持 VOC 的图片项目与批次。

```python
from ai_annotation import Client

PROJECT_ID = "<image-project-id>"
BATCH_ID = "<image-batch-id>"

with Client() as client:
    client.exports.download_voc(PROJECT_ID, "./project-voc.zip")
    client.batches.download_voc(PROJECT_ID, BATCH_ID, "./batch-voc.zip")
```

```bash
aap export project "$VIDEO_PROJECT_ID" --target video_json --body export-scope.json --out ./video-range.zip --json
aap export project "$IMAGE_PROJECT_ID" --target voc --out ./project-voc.zip --json
aap batches download-voc "$IMAGE_PROJECT_ID" "$BATCH_ID" --out ./batch-voc.zip --json
```

同步 VOC JSON 输出为 `{"out":"..."}`，没有 Job ID；不能混选 VOC 与其他格式或使用 `--no-wait`。下载采用带认证的 POST 流式请求，不自动重试；中断、HTTP 错误或落盘失败保留旧目标。CLI 请求文件同样支持 `--body -` 从 stdin 读取。
