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
    read_json_body,
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
