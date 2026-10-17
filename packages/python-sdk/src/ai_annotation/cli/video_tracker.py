"""CLI for video tracking jobs and candidate review."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.video_tracker_models import (
    VideoMaskCorrectionRequest,
    VideoTrackerCreateRequest,
    VideoTrackerDecisionRequest,
)

app = typer.Typer(
    help="视频追踪任务、候选恢复与审核。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
jobs_app = typer.Typer(help="追踪作业与候选审核。", no_args_is_help=True)
tasks_app = typer.Typer(help="任务级追踪与结果恢复。", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(tasks_app, name="tasks")

_STATUSES = {
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "pending_review",
    "partially_reviewed",
    "accepted",
    "discarded",
}


@jobs_app.command("list")
def list_jobs(
    project_id: str | None = typer.Option(None, "--project", help="项目 UUID"),
    status: str | None = typer.Option(None, "--status", help="作业状态"),
    model_key: str | None = typer.Option(None, "--model-key", help="追踪模型键"),
    cursor: str | None = typer.Option(None, "--cursor", help="下一页游标"),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="每页条数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出追踪作业；管理员权限和项目过滤由 API 执行。"""
    if status is not None and status not in _STATUSES:
        raise typer.BadParameter("--status 不是有效的视频追踪状态")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.list_jobs(
                project_id=project_id,
                status=status,
                model_key=model_key,
                cursor=cursor,
                limit=limit,
            )
    print_learning_result(result, json_output)


@jobs_app.command("get")
def get_job(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.get_job(job_id)
    print_learning_result(result, json_output)


@jobs_app.command("cancel")
def cancel_job(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认取消作业"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """取消计算或清理过时作业。"""
    with cli_errors(json_output):
        confirm_destructive(
            f"确认执行 video-tracker cancel_job {job_id}?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.video_tracker.cancel_job(job_id)
    print_learning_result(result, json_output)


@jobs_app.command("accept")
def accept_job(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认接受所有候选"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受作业中的所有追踪候选。"""
    with cli_errors(json_output):
        confirm_destructive(
            f"确认执行 video-tracker accept_job {job_id}?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.video_tracker.accept_job(job_id)
    print_learning_result(result, json_output)


@jobs_app.command("discard")
def discard_job(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认丢弃所有候选"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """丢弃作业中的所有候选。"""
    with cli_errors(json_output):
        confirm_destructive(
            f"确认执行 video-tracker discard_job {job_id}?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.video_tracker.discard_job(job_id)
    print_learning_result(result, json_output)


@jobs_app.command("decide")
def decide_candidates(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    body_file: Path = typer.Option(
        ..., "--body", "--body-file", help="决策 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认应用候选决策"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按作业修订号和源标注版本接受或拒绝所选候选。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, VideoTrackerDecisionRequest)
        confirm_destructive("确认应用视频追踪候选决策?", yes, json_output)
        with get_client(json_output) as client:
            result = client.video_tracker.decide_candidates(job_id, body)
    print_learning_result(result, json_output)


@jobs_app.command("preview")
def get_preview(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前暂存候选；本命令不会轮询计算状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.get_preview(job_id)
    print_learning_result(result, json_output)


@jobs_app.command("mask-content")
def get_mask_content(
    job_id: str = typer.Argument(..., help="追踪作业 UUID"),
    sha256: str = typer.Argument(..., help="候选掩码 SHA-256"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取 COCO RLE JSON 掩码内容。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.get_mask_content(job_id, sha256)
    print_learning_result(result, json_output)


@tasks_app.command("active")
def list_active(
    task_id: str = typer.Argument(..., help="视频任务 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """刷新后找回仍在排队或运行的作业。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.list_active(task_id)
    print_learning_result(result, json_output)


@tasks_app.command("reviewable")
def list_reviewable(
    task_id: str = typer.Argument(..., help="视频任务 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """刷新后找回仍待审核的候选作业。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_tracker.list_reviewable(task_id)
    print_learning_result(result, json_output)


@tasks_app.command("track")
def track(
    task_id: str = typer.Argument(..., help="视频任务 UUID"),
    body_file: Path = typer.Option(
        ..., "--body", "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建任务级追踪作业；不确定结果时用 active/reviewable 找回。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, VideoTrackerCreateRequest)
        with get_client(json_output) as client:
            result = client.video_tracker.track(task_id, body)
    print_learning_result(result, json_output)


@tasks_app.command("propagate")
def propagate(
    task_id: str = typer.Argument(..., help="视频任务 UUID"),
    annotation_id: str = typer.Argument(..., help="源轨迹标注 UUID"),
    body_file: Path = typer.Option(
        ..., "--body", "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """通过保留的单轨迹传播路由创建作业。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, VideoTrackerCreateRequest)
        with get_client(json_output) as client:
            result = client.video_tracker.propagate(task_id, annotation_id, body)
    print_learning_result(result, json_output)


@tasks_app.command("correct")
def create_correction(
    task_id: str = typer.Argument(..., help="视频任务 UUID"),
    annotation_id: str = typer.Argument(..., help="目标轨迹标注 UUID"),
    body_file: Path = typer.Option(
        ..., "--body", "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建修正作业"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """用源版本和掩码摘要创建视频掩码修正作业。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, VideoMaskCorrectionRequest)
        confirm_destructive("确认创建视频掩码修正作业?", yes, json_output)
        with get_client(json_output) as client:
            result = client.video_tracker.create_correction(
                task_id, annotation_id, body
            )
    print_learning_result(result, json_output)
