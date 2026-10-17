"""aap batches 子命令。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)
from ai_annotation.batch_operation_models import (
    AdminBatchLockRequest,
    BatchAssignmentPreviewRequest,
    BatchDistributionRequest,
    BatchSplitRequest,
    LidarExportPreflightRequest,
    ReviewedBatchAssignment,
    ReviewedBatchDistribution,
)
from ai_annotation.export_models import ExportRequestBody

app = typer.Typer(
    help="批次: 列出、创建、更新或删除项目批次。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap batches list P-1[/] · [dim]aap batches list P-1 --status active[/]",
)


@app.command("list")
def list_(
    project_id: str = typer.Argument(..., help="项目 ID"),
    status: str | None = typer.Option(None, "--status", help="按批次状态过滤"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目批次。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batches = client.batches.list(project_id, status=status)
    if json_output:
        print_json([b.model_dump(mode="json") for b in batches])
        return
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("状态")
    table.add_column("进度")
    table.add_column("审核")
    table.add_column("退回")
    table.add_column("标注员")
    table.add_column("审核员")
    for b in batches:
        annotator = b.annotator.name if b.annotator else "-"
        reviewer = b.reviewer.name if b.reviewer else "-"
        table.add_row(
            b.display_id,
            b.name,
            b.status,
            f"{b.completed_tasks}/{b.total_tasks}",
            str(b.review_tasks),
            str(b.rejected_tasks),
            annotator,
            reviewer,
        )
    console.print(table)


def _batch_fields(
    description: str | None,
    dataset_id: str | None,
    priority: int | None,
    deadline: str | None,
    annotator_id: str | None,
    reviewer_id: str | None,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "description": description,
            "dataset_id": dataset_id,
            "priority": priority,
            "deadline": deadline,
            "annotator_id": annotator_id,
            "reviewer_id": reviewer_id,
        }.items()
        if value is not None
    }


@app.command("create")
def create(
    project_id: str = typer.Argument(..., help="项目 ID"),
    name: str = typer.Option(..., "--name", help="批次名称"),
    description: str | None = typer.Option(None, "--description", help="描述"),
    dataset_id: str | None = typer.Option(None, "--dataset-id", help="数据集 ID"),
    priority: int | None = typer.Option(
        None, "--priority", min=0, max=100, help="优先级"
    ),
    deadline: str | None = typer.Option(None, "--deadline", help="截止日期 YYYY-MM-DD"),
    annotator_id: str | None = typer.Option(None, "--annotator-id", help="标注员 ID"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id", help="审核员 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建批次。"""
    fields = _batch_fields(
        description, dataset_id, priority, deadline, annotator_id, reviewer_id
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.create(project_id, name, **fields)
    if json_output:
        print_json(batch.model_dump(mode="json"))
    else:
        console.print(f"[green]批次已创建[/green] id={batch.id}")


@app.command("update")
def update(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    name: str | None = typer.Option(None, "--name", help="批次名称"),
    description: str | None = typer.Option(None, "--description", help="描述"),
    priority: int | None = typer.Option(
        None, "--priority", min=0, max=100, help="优先级"
    ),
    deadline: str | None = typer.Option(None, "--deadline", help="截止日期 YYYY-MM-DD"),
    annotator_id: str | None = typer.Option(None, "--annotator-id", help="标注员 ID"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id", help="审核员 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新批次的显式指定字段。"""
    fields = _batch_fields(
        description, None, priority, deadline, annotator_id, reviewer_id
    )
    if name is not None:
        fields["name"] = name
    if not fields:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.update(project_id, batch_id, **fields)
    if json_output:
        print_json(batch.model_dump(mode="json"))
    else:
        console.print(f"[green]批次已更新[/green] id={batch.id}")


@app.command("delete")
def delete(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    force: bool = typer.Option(False, "--force", help="允许删除已有结果的批次"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除批次。"""
    if json_output:
        confirm_destructive("确认删除批次?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.get(project_id, batch_id)
            has_results = any(
                (
                    batch.completed_tasks,
                    batch.review_tasks,
                    batch.approved_tasks,
                    batch.rejected_tasks,
                )
            )
            if has_results and not force:
                raise typer.BadParameter("批次已有结果，删除时必须传 --force")
            if not json_output:
                confirm_destructive(f"确认删除批次 {batch_id}?", yes, json_output)
            client.batches.delete(project_id, batch_id, force=force)
    if json_output:
        print_json(
            {
                "deleted": True,
                "project_id": project_id,
                "batch_id": batch_id,
                "forced": force,
            }
        )
    else:
        console.print(f"[green]批次已删除[/green] {batch_id}")


@app.command("transition")
def transition(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    target_status: str = typer.Option(..., "--status", help="目标状态"),
    reason: str | None = typer.Option(None, "--reason", help="逆向流转原因"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """流转批次状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.transition(
                project_id, batch_id, target_status, reason
            )
    if json_output:
        print_json(batch.model_dump(mode="json"))
    else:
        console.print(f"[green]批次状态已更新[/green] {batch.status}")


@app.command("reject")
def reject(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    feedback: str = typer.Option(..., "--feedback", help="退回反馈"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """退回批次。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.reject(project_id, batch_id, feedback)
    if json_output:
        print_json(batch.model_dump(mode="json"))
    else:
        console.print(f"[green]批次已退回[/green] {batch_id}")


@app.command("reset")
def reset(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    reason: str = typer.Option(..., "--reason", help="重置原因"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将批次重置为 draft。"""
    confirm_destructive(f"确认重置批次 {batch_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            batch = client.batches.reset(project_id, batch_id, reason)
    if json_output:
        print_json(batch.model_dump(mode="json"))
    else:
        console.print(f"[green]批次已重置[/green] {batch_id}")


@app.command("distribute")
def distribute(
    project_id: str = typer.Argument(..., help="项目 ID"),
    annotator_ids: list[str] | None = typer.Option(
        None, "--annotator-id", help="标注员 ID，可重复"
    ),
    reviewer_ids: list[str] | None = typer.Option(
        None, "--reviewer-id", help="审核员 ID，可重复"
    ),
    only_unassigned: bool = typer.Option(
        True,
        "--only-unassigned/--all",
        help="仅分配空缺责任人的批次，或覆盖全部",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """项目级轮询分配批次。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.distribute(
                project_id,
                annotator_ids=annotator_ids or [],
                reviewer_ids=reviewer_ids or [],
                only_unassigned=only_unassigned,
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]已分配 {result.distributed_batches} 个批次[/green]")


def _print_bulk(result, json_output: bool) -> None:
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"succeeded={len(result.succeeded)} "
            f"skipped={len(result.skipped)} failed={len(result.failed)}"
        )
        for item in result.failed:
            console.print(f"[red]{item.batch_id}: {item.reason}[/red]")
    if result.failed:
        raise typer.Exit(code=1)


def _bulk_ids(
    project_id: str, batch_ids: list[str], action: str, json_output: bool, **kwargs
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = getattr(client.batches, action)(project_id, batch_ids, **kwargs)
    _print_bulk(result, json_output)


@app.command("bulk-activate")
def bulk_activate(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(..., "--id", help="批次 ID，可重复"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量激活批次。"""
    _bulk_ids(project_id, batch_ids, "bulk_activate", json_output)


@app.command("bulk-approve")
def bulk_approve(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(..., "--id", help="批次 ID，可重复"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量通过批次。"""
    _bulk_ids(project_id, batch_ids, "bulk_approve", json_output)


@app.command("bulk-reject")
def bulk_reject(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(..., "--id", help="批次 ID，可重复"),
    feedback: str = typer.Option(..., "--feedback", help="退回反馈"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量退回批次。"""
    _bulk_ids(project_id, batch_ids, "bulk_reject", json_output, feedback=feedback)


@app.command("bulk-reassign")
def bulk_reassign(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(..., "--id", help="批次 ID，可重复"),
    annotator_id: str | None = typer.Option(None, "--annotator-id", help="标注员 ID"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id", help="审核员 ID"),
    clear_annotator: bool = typer.Option(False, "--clear-annotator", help="清除标注员"),
    clear_reviewer: bool = typer.Option(False, "--clear-reviewer", help="清除审核员"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量重新分配责任人。"""
    if annotator_id is not None and clear_annotator:
        raise typer.BadParameter("--annotator-id 与 --clear-annotator 不能同时使用")
    if reviewer_id is not None and clear_reviewer:
        raise typer.BadParameter("--reviewer-id 与 --clear-reviewer 不能同时使用")
    assignment = {}
    if annotator_id is not None or clear_annotator:
        assignment["annotator_id"] = annotator_id
    if reviewer_id is not None or clear_reviewer:
        assignment["reviewer_id"] = reviewer_id
    if not assignment:
        raise typer.BadParameter("至少提供一个分配选项")
    _bulk_ids(project_id, batch_ids, "bulk_reassign", json_output, **assignment)


@app.command("export")
def export_batch(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    targets: list[str] | None = typer.Option(None, "--target", help="导出格式，可重复"),
    body_path: Path | None = typer.Option(
        None, "--body", help="视频 scope 或 lidar 选项的 UTF-8 JSON 文件"
    ),
    include_attributes: bool = typer.Option(
        True, "--include-attributes/--no-include-attributes", help="导出属性"
    ),
    axis_frame: str = typer.Option("iso", "--axis-frame", help="3D 坐标帧"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建批次导出 job。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ExportRequestBody) if body_path else None
        with get_client(json_output) as client:
            job_id = client.batches.export(
                project_id,
                batch_id,
                targets=targets,
                include_attributes=include_attributes,
                axis_frame=axis_frame,
                body=body,
            )
    if json_output:
        print_json({"job_id": job_id})
    else:
        console.print(f"[green]批次导出 job 已创建[/green] {job_id}")


@app.command("download-voc")
def download_batch_voc(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    out: Path = typer.Option(..., "--out", help="VOC ZIP 输出路径"),
    include_attributes: bool = typer.Option(
        True, "--include-attributes/--no-include-attributes", help="导出属性"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载批次 VOC ZIP。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            dest = client.batches.download_voc(
                project_id, batch_id, out, include_attributes=include_attributes
            )
    if json_output:
        print_json({"out": str(dest)})
    else:
        console.print(f"[green]批次 VOC 导出完成[/green] → {dest}")


def _print_partial_batch_result(result, json_output: bool) -> None:
    """Always print the complete receipt before returning a failure exit code."""
    print_learning_result(result, json_output)
    if result.failed:
        raise typer.Exit(code=1)


def _print_batch_model_list(result, json_output: bool) -> None:
    print_learning_result(
        [item.model_dump(mode="json", by_alias=True) for item in result],
        json_output,
    )


@app.command("unclassified-count")
def unclassified_count(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取项目中尚未归入批次的任务数。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.unclassified_count(project_id)
    print_learning_result(result, json_output)


@app.command("audit-logs")
def audit_logs(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="最多返回 200 条"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 数组"),
) -> None:
    """读取批次操作历史；批次必须属于指定项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.audit_logs(project_id, batch_id, limit=limit)
    _print_batch_model_list(result, json_output)


@app.command("bulk-archive")
def bulk_archive(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(
        ..., "--id", help="批次 ID，可重复，最多 200 个"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出完整结果 JSON"),
) -> None:
    """批量归档；部分成功会先输出完整回执，存在失败项时以 1 退出。"""
    confirm_destructive(f"确认归档 {len(batch_ids)} 个批次?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.bulk_archive(project_id, batch_ids)
    _print_partial_batch_result(result, json_output)


@app.command("bulk-delete")
def bulk_delete(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_ids: list[str] = typer.Option(
        ..., "--id", help="批次 ID，可重复，最多 200 个"
    ),
    force: bool = typer.Option(False, "--force", help="确认清理进行中任务和 AI 预标"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出完整结果 JSON"),
) -> None:
    """批量删除；默认保护有活动成果的批次，强制清理需再带 --force。"""
    action = "强制删除并清理任务成果" if force else "删除批次"
    confirm_destructive(f"确认{action}，共 {len(batch_ids)} 个?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.bulk_delete(project_id, batch_ids, force=force)
    _print_partial_batch_result(result, json_output)


@app.command("distribution-preview")
def distribution_preview(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(..., "--body", help="分派 JSON；用 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览项目批次分派；batch_ids 省略表示按 only_unassigned 选择。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, BatchDistributionRequest)
        with get_client(json_output) as client:
            result = client.batches.distribution_preview(project_id, body)
    print_learning_result(result, json_output)


@app.command("distribution-apply")
def distribution_apply(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="含 preview_version 的 JSON；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认应用预览分派"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """应用明确提供的预览版本；409 时需重新预览后手动提交。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ReviewedBatchDistribution)
    confirm_destructive("确认应用这份批次分派预览?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.distribution_apply(project_id, body)
    print_learning_result(result, json_output)


@app.command("split")
def split(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(..., "--body", help="切分 JSON；用 - 从 stdin 读取"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建批次并移动任务"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 列表"),
) -> None:
    """按 metadata、id_range、random 或 by_scene 创建批次。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, BatchSplitRequest)
    confirm_destructive(f"确认按 {body.strategy} 策略切分项目任务?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.split(project_id, body)
    _print_batch_model_list(result, json_output)


@app.command("admin-lock")
def admin_lock(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    reason: str = typer.Option(..., "--reason", help="锁定原因，1-500 字"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以项目负责人身份锁定批次并记录原因。"""
    with cli_errors(json_output):
        lock_request = AdminBatchLockRequest(reason=reason)
    confirm_destructive(f"确认锁定批次 {batch_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.admin_lock(
                project_id, batch_id, lock_request.reason
            )
    print_learning_result(result, json_output)


@app.command("admin-unlock")
def admin_unlock(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以项目负责人身份解除批次锁定。"""
    confirm_destructive(f"确认解除批次 {batch_id} 的管理员锁?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.admin_unlock(project_id, batch_id)
    print_learning_result(result, json_output)


@app.command("assignment-preview")
def assignment_preview(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="目标责任人 JSON；必须同时给出 annotator_id/reviewer_id，可传 null 清除",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出预览 JSON"),
) -> None:
    """预览单个批次的完整责任人目标；null 表示清除该角色。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, BatchAssignmentPreviewRequest)
        with get_client(json_output) as client:
            result = client.batches.assignment_preview(project_id, batch_id, body)
    print_learning_result(result, json_output)


@app.command("assignment-apply")
def assignment_apply(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="目标责任人和 preview_version JSON；null 表示清除该角色"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认应用责任人变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """应用完整责任人目标；不自动合并或重试过期预览。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ReviewedBatchAssignment)
    confirm_destructive(f"确认更新批次 {batch_id} 的标注员和审核员?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.assignment_apply(project_id, batch_id, body)
    print_learning_result(result, json_output)


@app.command("lidar-preflight")
def lidar_preflight(
    project_id: str = typer.Argument(..., help="激光雷达项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    body_path: Path = typer.Option(..., "--body", help="预检 JSON；用 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检批次 LiDAR 导出；ready=false 和 issues 是正常结果。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, LidarExportPreflightRequest)
        with get_client(json_output) as client:
            result = client.batches.lidar_preflight(project_id, batch_id, body)
    print_learning_result(result, json_output)


@app.command("submit-review")
def submit_review(
    project_id: str = typer.Argument(..., help="项目 ID"),
    batch_id: str = typer.Argument(..., help="批次 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认整批送审"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """整批送审；仅负责人或已分配的标注员可执行。"""
    confirm_destructive(
        f"确认提交批次 {batch_id} 内符合条件的任务送审?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.batches.submit_review(project_id, batch_id)
    print_learning_result(result, json_output)
    if result.remaining_tasks:
        raise typer.Exit(1)
