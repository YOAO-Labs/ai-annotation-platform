"""aap mask-qc subcommands for review, comparison, and repair workflows."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.mask_qc_models import (
    MaskQCIssuePatch,
    MaskQCRunRequest,
    MaskRepairDryRunRequest,
    MaskRepairExecuteRequest,
    MaskRepairRollbackRequest,
)

app = typer.Typer(
    help="Mask QC issues, version comparisons, and guarded mask repairs.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "Examples: [dim]aap mask-qc issues P-1 --status stale --json[/] · "
        "[dim]aap mask-qc repair-dry-run P-1 --body - --json[/]"
    ),
)


class IssueStatusOption(str, Enum):
    open = "open"
    resolved = "resolved"
    wont_fix = "wont_fix"
    stale = "stale"


class IssuePatchStatusOption(str, Enum):
    open = "open"
    resolved = "resolved"
    wont_fix = "wont_fix"


class IssueSeverityOption(str, Enum):
    info = "info"
    warning = "warning"
    blocker = "blocker"


class CompareBaselineOption(str, Enum):
    previous_version = "previous_version"
    tracker_candidate = "tracker_candidate"
    ai_candidate = "ai_candidate"
    neighbor_keyframe = "neighbor_keyframe"


_REPAIR_FAILURE_STATUSES = {"failed", "partial", "cancelled", "rollback_failed"}


def _print_repair_batch(result, json_output: bool) -> None:
    """Print the complete domain receipt before returning failure for partial work."""
    print_learning_result(result, json_output)
    if result.status in _REPAIR_FAILURE_STATUSES:
        raise typer.Exit(code=1)


@app.command("issues")
def list_issues(
    project_id: str = typer.Argument(..., help="项目 ID"),
    task_id: str | None = typer.Option(None, "--task-id"),
    annotation_id: str | None = typer.Option(None, "--annotation-id"),
    status: IssueStatusOption | None = typer.Option(None, "--status"),
    severity: IssueSeverityOption | None = typer.Option(None, "--severity"),
    code: str | None = typer.Option(None, "--code"),
    frame: int | None = typer.Option(None, "--frame", min=0),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    cursor: str | None = typer.Option(None, "--cursor", help="原样传回的分页游标"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按可见项目任务、状态、规则、严重性和帧筛选 Mask QC issues。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.list_issues(
                project_id,
                task_id=task_id,
                annotation_id=annotation_id,
                status=status.value if status else None,
                severity=severity.value if severity else None,
                code=code,
                frame=frame,
                limit=limit,
                cursor=cursor,
            )
    print_learning_result(result, json_output)


@app.command("task-summary")
def task_summary(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取任务级 Mask QC 运行状态和问题计数。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.task_summary(task_id)
    print_learning_result(result, json_output)


@app.command("run")
def create_run(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="Mask QC 请求 JSON；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认启动异步 Mask QC 运行"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建 project/task_ids/annotation_ids 范围的异步 Mask QC run。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskQCRunRequest)
    confirm_destructive(f"确认在项目 {project_id} 启动 Mask QC?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.create_run(project_id, body)
    print_learning_result(result, json_output)


@app.command("compare")
def compare(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    annotation_version: int = typer.Option(..., "--annotation-version", min=1),
    baseline: CompareBaselineOption = typer.Option(..., "--baseline"),
    frame_index: int | None = typer.Option(None, "--frame-index", min=0),
    candidate_job_id: str | None = typer.Option(None, "--candidate-job-id"),
    candidate_job_revision: int | None = typer.Option(
        None, "--candidate-job-revision", min=1
    ),
    candidate_digest: str | None = typer.Option(None, "--candidate-digest"),
    candidate_instance_id: str | None = typer.Option(None, "--candidate-instance-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """比较指定标注版本和 baseline，返回 metrics、digest 与 content path。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.compare(
                annotation_id,
                annotation_version=annotation_version,
                baseline=baseline.value,
                frame_index=frame_index,
                candidate_job_id=candidate_job_id,
                candidate_job_revision=candidate_job_revision,
                candidate_digest=candidate_digest,
                candidate_instance_id=candidate_instance_id,
            )
    print_learning_result(result, json_output)


@app.command("compare-content")
def compare_content(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    annotation_version: int = typer.Option(..., "--annotation-version", min=1),
    digest: str = typer.Option(..., "--digest", help="Compare side 的 SHA-256"),
    frame_index: int | None = typer.Option(None, "--frame-index", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 COCO RLE JSON"),
) -> None:
    """读取 exact annotation version/digest 的 COCO RLE 内容。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.compare_content(
                annotation_id,
                annotation_version=annotation_version,
                digest=digest,
                frame_index=frame_index,
            )
    print_learning_result(result, json_output)


@app.command("issue-region")
def issue_region(
    issue_id: str = typer.Argument(..., help="Mask QC issue ID"),
    digest: str = typer.Option(..., "--digest", help="issue.region_digest"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 COCO RLE JSON"),
) -> None:
    """读取 issue 区域内容；服务端验证 issue/task scope 和 digest。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.issue_region_content(issue_id, digest=digest)
    print_learning_result(result, json_output)


@app.command("issue")
def get_issue(
    issue_id: str = typer.Argument(..., help="Mask QC issue ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取可见 issue 的当前来源版本与 effective_status。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.get_issue(issue_id)
    print_learning_result(result, json_output)


@app.command("patch-issue")
def patch_issue(
    issue_id: str = typer.Argument(..., help="Mask QC issue ID"),
    status: IssuePatchStatusOption = typer.Option(..., "--status"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认修改 issue 状态"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新非 stale issue；过期状态或资源占用冲突由服务端拒绝。"""
    with cli_errors(json_output):
        MaskQCIssuePatch(status=status.value)
    confirm_destructive(
        f"确认将 issue {issue_id} 状态设为 {status.value}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.patch_issue(issue_id, status.value)
    print_learning_result(result, json_output)


@app.command("repair-dry-run")
def dry_run_repairs(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="Repair actions JSON；用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出完整 plan/receipt JSON"
    ),
) -> None:
    """构造无 annotation 写入的 repair plan；保留每项可执行/跳过结果。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskRepairDryRunRequest)
        with get_client(json_output) as client:
            result = client.mask_qc.dry_run_repairs(project_id, body)
    print_learning_result(result, json_output)


@app.command("repair-execute")
def execute_repairs(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="receipt + plan_digest JSON；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行精确的 dry-run plan"),
    json_output: bool = typer.Option(
        False, "--json", help="输出 repair batch/job 回执"
    ),
) -> None:
    """单次提交 dry-run receipt；过期或 digest 冲突时需重新 dry-run。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskRepairExecuteRequest)
    confirm_destructive("确认执行此 Mask repair plan?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.execute_repairs(project_id, body)
    _print_repair_batch(result, json_output)


@app.command("repair-get")
def get_repair_batch(
    repair_id: str = typer.Argument(..., help="Mask repair batch ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取 repair domain status、result digest 和 async job IDs。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.get_repair_batch(repair_id)
    _print_repair_batch(result, json_output)


@app.command("repair-resume")
def resume_repairs(
    repair_id: str = typer.Argument(..., help="Mask repair batch ID"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认恢复 failed/partial repair"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出 repair batch/job 回执"
    ),
) -> None:
    """恢复服务端判定为 failed/partial 的 batch；不会改写 repair plan。"""
    confirm_destructive(f"确认恢复 repair batch {repair_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.resume_repairs(repair_id)
    _print_repair_batch(result, json_output)


@app.command("repair-rollback")
def rollback_repairs(
    repair_id: str = typer.Argument(..., help="Mask repair batch ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="expected_result_digest JSON；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认按当前 result digest 回滚"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出 repair batch/job 回执"
    ),
) -> None:
    """仅提交显式 expected_result_digest；回滚窗口/资格由服务端校验。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, MaskRepairRollbackRequest)
    confirm_destructive(f"确认回滚 repair batch {repair_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.mask_qc.rollback_repairs(repair_id, body)
    _print_repair_batch(result, json_output)
