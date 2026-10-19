---
title: SDK Cookbook
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-19
---

# SDK Cookbook

三个可直接拷贝运行的完整片段。凭据均假定已通过 `aap login` 或环境变量 `AAP_BASE_URL` / `AAP_API_KEY` 配置(`Client()` 无参构造时自动回落),也可以显式传 `Client(base_url=..., api_key=...)`。

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
