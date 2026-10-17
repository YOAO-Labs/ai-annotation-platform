"""aap point-cloud-quality 子命令。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    console,
    confirm_destructive,
    get_client,
    print_json,
    read_json_body,
)
from ai_annotation.point_cloud_quality_models import (
    PointCloudQualityConfig,
    PointCloudQualityIssuePatch,
    PointCloudQualityRunRequest,
)

app = typer.Typer(
    help="点云质量扫描、问题复核与规则治理。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _expected_versions(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        annotation_id, separator, raw_version = value.partition("=")
        if not separator or not annotation_id or not raw_version.isdecimal():
            raise typer.BadParameter(
                "--expected-version 格式应为 annotation-uuid=版本号"
            )
        if annotation_id in result:
            raise typer.BadParameter("同一 annotation 不可重复指定 --expected-version")
        result[annotation_id] = int(raw_version)
    return result


@app.command("issues")
def issues(
    project_id: str = typer.Argument(..., help="项目 ID"),
    status: str | None = typer.Option(
        None, "--status", help="open/resolved/wont_fix/stale"
    ),
    severity: str | None = typer.Option(
        None, "--severity", help="blocker/warning/info"
    ),
    code: str | None = typer.Option(None, "--code", help="规则代码"),
    scene_id: str | None = typer.Option(None, "--scene-id", help="Scene ID"),
    task_id: str | None = typer.Option(None, "--task-id", help="任务 ID"),
    annotation_id: str | None = typer.Option(
        None, "--annotation-id", help="Annotation ID"
    ),
    scene_track_id: str | None = typer.Option(None, "--scene-track-id", help="轨迹 ID"),
    frame: int | None = typer.Option(None, "--frame", min=0, help="帧索引"),
    limit: int = typer.Option(100, "--limit", min=1, max=200),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前项目权限范围内的问题；读取时可能将源数据已变化的问题标为 stale。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.point_cloud_quality.list_issues(
                project_id,
                status=status,
                severity=severity,
                code=code,
                scene_id=scene_id,
                task_id=task_id,
                annotation_id=annotation_id,
                scene_track_id=scene_track_id,
                frame=frame,
                limit=limit,
                offset=offset,
            )
    data = page.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("ID")
    table.add_column("规则")
    table.add_column("级别")
    table.add_column("状态")
    table.add_column("Scene / 帧")
    table.add_column("Task / Annotation")
    for item in page.items:
        locator = item.locator
        table.add_row(
            str(item.id),
            item.code,
            item.severity,
            item.status,
            f"{locator.scene_id} / {locator.frame_index if locator.frame_index is not None else '-'}",
            f"{locator.task_id or '-'} / {locator.annotation_id or '-'}",
        )
    console.print(table)
    console.print(f"总数 {page.total} · 当前页 {len(page.items)} · offset={offset}")


@app.command("run")
def run(
    project_id: str = typer.Argument(..., help="项目 ID"),
    scope: str = typer.Option(
        "scene_ids", "--scope", help="project / scene_ids / task_ids / annotation_ids"
    ),
    scene_ids: list[str] = typer.Option([], "--scene-id", help="Scene ID，可重复"),
    task_ids: list[str] = typer.Option([], "--task-id", help="Task ID，可重复"),
    annotation_ids: list[str] = typer.Option(
        [], "--annotation-id", help="3D Annotation ID，可重复"
    ),
    expected_versions: list[str] = typer.Option(
        [], "--expected-version", help="注释版本预检，格式 UUID=版本号，可重复"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认启动点云扫描"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """启动一次点云扫描；项目/Scene 全量范围仅项目负责人可用。"""
    with cli_errors(json_output):
        body = PointCloudQualityRunRequest(
            scope=scope,
            scene_ids=scene_ids,
            task_ids=task_ids,
            annotation_ids=annotation_ids,
            expected_versions=_expected_versions(expected_versions),
        )
        if body.scope == "project":
            detail = "扫描整个项目并读取关联点云/相机数据"
        elif body.scope == "scene_ids":
            detail = f"扫描 {len(body.scene_ids)} 个 Scene 并读取关联点云/相机数据"
        elif body.scope == "task_ids":
            detail = f"扫描 {len(body.task_ids)} 个任务并读取关联点云/相机数据"
        else:
            detail = f"扫描 {len(body.annotation_ids)} 个标注及相关轨迹/相机数据"
        confirm_destructive(
            f"确认启动项目 {project_id} 的质量任务：{detail}?", yes, json_output
        )
        with get_client(json_output) as client:
            receipt = client.point_cloud_quality.create_run(project_id, body)
    data = receipt.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        reused = "（复用已有运行）" if receipt.reused else ""
        console.print(
            f"[green]运行回执[/green] run={receipt.id} status={receipt.status} "
            f"progress={receipt.progress_pct}% job={receipt.async_job_id or '-'} {reused}"
        )
        if receipt.status in {"pending", "running"}:
            console.print("扫描仍在运行；需要时运行 point-cloud-quality run-get 查询。")
        elif receipt.status == "failed" and receipt.error_message:
            console.print(f"运行失败: {receipt.error_message}")


@app.command("run-get")
def run_get(
    project_id: str = typer.Argument(..., help="项目 ID"),
    run_id: str = typer.Argument(..., help="质量运行 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查询运行状态；命令不会自动轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.point_cloud_quality.get_run(project_id, run_id)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"run={result.id} status={result.status} progress={result.progress_pct}% "
            f"job={result.async_job_id or '-'} reused={result.reused}"
        )
        if result.error_message:
            console.print(f"错误: {result.error_message}")
        console.print_json(data=data["summary"])


@app.command("issue-get")
def issue_get(
    issue_id: str = typer.Argument(..., help="质量问题 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取问题及定位证据；服务端可能同时持久化 stale 状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.point_cloud_quality.get_issue(issue_id)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("issue-update")
def issue_update(
    issue_id: str = typer.Argument(..., help="质量问题 ID"),
    status: str = typer.Option(..., "--status", help="open / resolved / wont_fix"),
    reason: str | None = typer.Option(None, "--reason", help="处理理由；wont_fix 必填"),
    review_verdict: str | None = typer.Option(
        None,
        "--review-verdict",
        help="confirmed / false_positive / accepted_exception / uncertain",
    ),
    review_note: str | None = typer.Option(None, "--review-note", help="复核备注"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新问题状态"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新问题状态和复核结论；409 stale/busy 需要人工处理后重新提交。"""
    with cli_errors(json_output):
        body = PointCloudQualityIssuePatch(
            status=status,
            reason=reason,
            review_verdict=review_verdict,
            review_note=review_note,
        )
        confirm_destructive(
            f"确认将问题 {issue_id} 更新为 {body.status}?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud_quality.patch_issue(issue_id, body)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"[green]已更新[/green] issue={result.id} status={result.status} "
            f"verdict={result.review_verdict or '-'} reviewer={result.reviewed_by_id or '-'}"
        )


@app.command("evaluations")
def evaluations(
    project_id: str = typer.Argument(..., help="项目 ID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出冻结评估；gate_status=promote 仅表示可晋级。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.point_cloud_quality.list_evaluations(
                project_id, limit=limit, offset=offset
            )
    data = page.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("ID")
    table.add_column("Gate")
    table.add_column("样本")
    table.add_column("基线版本")
    table.add_column("已晋级")
    table.add_column("创建时间")
    for item in page.items:
        table.add_row(
            str(item.id),
            item.gate_status,
            str(item.sample_count),
            str(item.baseline_config_revision),
            str(item.promoted_config_revision or "否"),
            item.created_at.isoformat(),
        )
    console.print(table)
    console.print(f"总数 {page.total} · 当前页 {len(page.items)} · offset={offset}")


@app.command("evaluation-create")
def evaluation_create(
    project_id: str = typer.Argument(..., help="项目 ID (需项目负责人或 super_admin)"),
    config_file: Path = typer.Argument(
        ..., help="完整候选配置 JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建冻结评估"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按候选配置 revision 冻结已复核样本；创建评估不会晋级配置。"""
    with cli_errors(json_output):
        candidate = read_json_body(config_file, PointCloudQualityConfig)
        confirm_destructive(
            f"确认用 config revision {candidate.config_revision} 创建项目 {project_id} 的冻结评估?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud_quality.create_evaluation(project_id, candidate)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"[green]评估已冻结[/green] id={result.id} gate={result.gate_status} "
            f"samples={result.sample_count} baseline=r{result.baseline_config_revision}"
        )
        console.print(
            "只有 gate=promote 的评估才可单独提交晋级；评估本身没有更改项目配置。"
        )


@app.command("evaluation-get")
def evaluation_get(
    project_id: str = typer.Argument(..., help="项目 ID"),
    evaluation_id: str = typer.Argument(..., help="评估 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取候选/基线快照、样本指标和 promotion 状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.point_cloud_quality.get_evaluation(
                project_id, evaluation_id
            )
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print_json(data=data)


@app.command("evaluation-promote")
def evaluation_promote(
    project_id: str = typer.Argument(..., help="项目 ID (需项目负责人或 super_admin)"),
    evaluation_id: str = typer.Argument(..., help="评估 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更改项目质量配置"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将通过门禁的候选配置晋级为项目当前配置；服务端校验基线 revision/digest。"""
    confirm_destructive(
        f"确认晋级项目 {project_id} 的质量评估 {evaluation_id}?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.point_cloud_quality.promote_evaluation(
                project_id, evaluation_id
            )
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"[green]配置已晋级[/green] evaluation={result.id} "
            f"promoted_revision={result.promoted_config_revision} "
            f"promoted_by={result.promoted_by_id} at={result.promoted_at}"
        )
