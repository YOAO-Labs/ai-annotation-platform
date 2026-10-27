"""aap datasets 子命令。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

import typer
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table

from ai_annotation.dataset_operation_models import DatasetImportFromConnectionRequest
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    get_error_format,
    print_error,
    print_json,
    print_learning_result,
    progress_console,
    read_json_body,
)
from ai_annotation.cli.jobs import wait_job
from ai_annotation.models import DatasetLifecycleOperation, DatasetLifecyclePreview

app = typer.Typer(
    help="数据集管理: 维护数据集/文件并管理项目关联。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "示例: [dim]aap datasets create --name imgs[/] · "
        "[dim]aap datasets upload D-1 ./images/[/] · "
        "[dim]aap datasets upload D-1 pack.zip --zip[/] · "
        "[dim]aap datasets link D-1 P-1[/]"
    ),
)


class DatasetState(str, Enum):
    active = "active"
    archived = "archived"
    all = "all"


@app.command("list")
def list_(
    state: DatasetState = typer.Option(
        DatasetState.active, "--state", help="数据集状态，默认 active"
    ),
    search: str | None = typer.Option(None, "--search", help="名称搜索"),
    data_type: str | None = typer.Option(None, "--data-type", help="数据类型"),
    has_scenes: bool | None = typer.Option(
        None, "--has-scenes/--no-scenes", help="是否包含场景"
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="分页偏移"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出数据集，默认只显示未归档的数据集。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.datasets.list(
                search=search,
                data_type=data_type,
                has_scenes=has_scenes,
                limit=limit,
                offset=offset,
                state=state.value,
            )
    if json_output:
        print_json(page.model_dump(mode="json"))
        return
    table = Table()
    for label in ("ID", "名称", "类型", "文件数", "状态"):
        table.add_column(label)
    for dataset in page.items:
        table.add_row(
            dataset.display_id,
            dataset.name,
            dataset.data_type,
            str(dataset.file_count),
            "archived" if dataset.archived_at else "active",
        )
    console.print(table)
    console.print(f"共 {page.total} 条 · offset={page.offset} · limit={page.limit}")


def _print_lifecycle_preview(
    preview: DatasetLifecyclePreview, json_output: bool
) -> None:
    output = progress_console if json_output else console
    output.print(
        f"影响预览: dataset={preview.dataset_id} · revision={preview.lifecycle_revision}"
    )
    table = Table("范围", "数量")
    for field, label in (
        ("items", "文件"),
        ("sources", "来源"),
        ("projects", "关联项目"),
        ("tasks", "任务"),
        ("annotations", "标注"),
        ("active_scans", "进行中扫描"),
        ("active_registrations", "进行中注册"),
        ("enabled_schedules", "启用的同步计划"),
        ("pending_jobs", "待完成作业"),
        ("ready_lineage_versions", "已就绪的冻结版本"),
        ("unfinished_lineage_versions", "未完成的冻结版本"),
    ):
        table.add_row(label, str(getattr(preview.counts, field)))
    output.print(table)
    output.print(f"冻结覆盖: {preview.frozen_coverage}")
    output.print("冻结版本统计仅覆盖已记录的数据集成员关系。")
    output.print(f"预览有效期至: {preview.expires_at.isoformat()}")
    if preview.operation == "archive":
        output.print(
            "归档保留文件、来源、关联项目、任务、标注和已有冻结版本；"
            "数据集管理写入停止，现有任务和标注仍按项目权限访问。"
        )
    else:
        output.print(
            "恢复仅改变数据集状态；不会恢复已撤销的权限、来源或同步委托，"
            "也不会自动恢复旧作业。"
        )


def _change_lifecycle(
    dataset_id: str,
    operation: DatasetLifecycleOperation,
    yes: bool,
    json_output: bool,
) -> None:
    label = "归档" if operation == "archive" else "恢复"
    if json_output and not yes:
        confirm_destructive(f"确认{label}数据集?", yes, json_output)
    if not yes and not sys.stdin.isatty():
        raise typer.BadParameter("非交互终端执行生命周期变更时必须传 --yes")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            dataset = client.datasets.get(dataset_id)
            preview = client.datasets.preview_lifecycle(
                dataset.id,
                operation,
                expected_revision=dataset.lifecycle_revision,
            )
            expected_state = "active" if operation == "archive" else "archived"
            if (
                preview.dataset_id != dataset.id
                or preview.lifecycle_revision != dataset.lifecycle_revision
                or preview.operation != operation
                or preview.state != expected_state
            ):
                print_error("影响预览与本次操作不一致，请重新运行命令。", json_output)
                raise typer.Exit(code=1)
            _print_lifecycle_preview(preview, json_output)
            confirm_destructive(
                f"确认{label}数据集 {dataset.display_id}?", yes, json_output
            )
            if preview.expires_at <= datetime.now(timezone.utc):
                print_error("影响预览已过期，请重新运行命令。", json_output)
                raise typer.Exit(code=1)
            result = client.datasets.set_lifecycle(
                preview.dataset_id,
                preview.operation,
                expected_revision=preview.lifecycle_revision,
                impact_token=preview.impact_token,
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]数据集已{label}[/green] {result.dataset_id}")


@app.command("archive")
def archive(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="查看预览后跳过交互确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览影响并确认归档数据集。脚本需显式传 --yes。"""
    _change_lifecycle(dataset_id, "archive", yes, json_output)


@app.command("restore")
def restore(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="查看预览后跳过交互确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览影响并确认恢复数据集状态，旧权限、委托及作业不会自动恢复。"""
    _change_lifecycle(dataset_id, "restore", yes, json_output)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="数据集名称"),
    data_type: str = typer.Option("image", "--data-type", help="数据类型"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建数据集。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    with cli_errors(json_output):
        with get_client(json_output) as client:
            dataset = client.datasets.create(
                name=name, data_type=data_type, idempotency_key=idempotency_key
            )
    if json_output:
        print_json(dataset.model_dump(mode="json"))
    else:
        console.print(
            f"[green]数据集已创建[/green] id={dataset.id} display_id={dataset.display_id}"
        )


@app.command("update")
def update(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    name: str | None = typer.Option(None, "--name", help="数据集名称"),
    description: str | None = typer.Option(None, "--description", help="描述"),
    axis_convention: str | None = typer.Option(
        None, "--axis-convention", help="点云坐标系约定"
    ),
    clear_axis_convention: bool = typer.Option(
        False, "--clear-axis-convention", help="清除坐标系约定"
    ),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新数据集的显式指定字段。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    if axis_convention is not None and clear_axis_convention:
        raise typer.BadParameter(
            "--axis-convention 与 --clear-axis-convention 不能同时使用"
        )
    fields = {
        key: value
        for key, value in {"name": name, "description": description}.items()
        if value is not None
    }
    if axis_convention is not None or clear_axis_convention:
        fields["axis_convention"] = axis_convention
    if not fields:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            dataset = client.datasets.update(
                dataset_id, idempotency_key=idempotency_key, **fields
            )
    if json_output:
        print_json(dataset.model_dump(mode="json"))
    else:
        console.print(f"[green]数据集已更新[/green] id={dataset.id}")


@app.command("items")
def items(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="起始偏移"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出数据集文件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.datasets.list_items(dataset_id, limit=limit, offset=offset)
    if json_output:
        print_json(page.model_dump(mode="json"))
        return
    table = Table()
    table.add_column("ID")
    table.add_column("文件名")
    table.add_column("类型")
    table.add_column("大小")
    for item in page.items:
        table.add_row(
            str(item.id), item.file_name, item.file_type, str(item.file_size or 0)
        )
    console.print(table)


@app.command("delete-item")
def delete_item(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    item_id: str = typer.Argument(..., help="文件 ID"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除数据集文件。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(f"确认删除文件 {item_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.datasets.delete_item(
                dataset_id, item_id, idempotency_key=idempotency_key
            )
    if json_output:
        print_json({"deleted": True, "dataset_id": dataset_id, "item_id": item_id})
    else:
        console.print(f"[green]文件已删除[/green] {item_id}")


@app.command("projects")
def projects(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出数据集已关联的项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            linked = client.datasets.list_projects(dataset_id)
    if json_output:
        print_json([project.model_dump(mode="json") for project in linked])
        return
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("状态")
    for project in linked:
        table.add_row(project.display_id, project.name, project.status)
    console.print(table)


@app.command("preview-unlink")
def preview_unlink(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览取消关联将删除的数据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            preview = client.datasets.preview_unlink(dataset_id, project_id)
    if json_output:
        print_json(preview.model_dump(mode="json"))
    else:
        console.print(
            "将删除: "
            f"tasks={preview.will_delete_tasks} "
            f"annotations={preview.will_delete_annotations} "
            f"batches={preview.will_delete_batches}"
        )


@app.command("unlink")
def unlink(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    project_id: str = typer.Argument(..., help="项目 ID"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """取消数据集与项目的关联。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    if json_output:
        confirm_destructive("确认取消关联?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            if not json_output:
                preview = client.datasets.preview_unlink(dataset_id, project_id)
                console.print(
                    "将删除: "
                    f"tasks={preview.will_delete_tasks} "
                    f"annotations={preview.will_delete_annotations} "
                    f"batches={preview.will_delete_batches}"
                )
                confirm_destructive("确认取消关联?", yes, json_output)
            result = client.datasets.unlink_project(
                dataset_id, project_id, idempotency_key=idempotency_key
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            "[green]已取消关联[/green]: "
            f"tasks={result.deleted_tasks} "
            f"annotations={result.deleted_annotations} "
            f"batches={result.deleted_batches}"
        )


@app.command("delete")
def delete(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除数据集。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(f"确认删除数据集 {dataset_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.datasets.delete(dataset_id, idempotency_key=idempotency_key)
    if json_output:
        print_json({"deleted": True, "dataset_id": dataset_id})
    else:
        console.print(f"[green]数据集已删除[/green] {dataset_id}")


@app.command("upload")
def upload(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    path: Path = typer.Argument(..., exists=True, help="文件 / 目录 / ZIP 包路径"),
    zip_: bool = typer.Option(False, "--zip", help="按 ZIP 整包上传 (后端解压入库)"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传文件到数据集: 目录逐文件上传, --zip 则整包上传。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
        if not zip_:
            raise typer.BadParameter("--idempotency-key 仅支持 --zip")
    if zip_:
        if not path.is_file():
            print_error(
                f"--zip 需要 ZIP 文件路径, 而不是目录: {path}",
                json_output,
                detail="--zip requires a ZIP file rather than a directory.",
            )
            raise typer.Exit(code=1)
        with cli_errors(json_output):
            with get_client(json_output) as client:
                result = client.datasets.upload_zip(
                    dataset_id, path, idempotency_key=idempotency_key
                )
        if json_output:
            print_json(result.model_dump(mode="json"))
        else:
            console.print(
                f"[green]ZIP 上传完成[/green]: added={result.added} "
                f"deduped={result.deduped} skipped={result.skipped} "
                f"errors={len(result.errors)}"
            )
        return

    files = (
        [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    )
    if not files:
        print_error(
            f"目录中没有可上传的文件: {path}",
            json_output,
            detail="Directory contains no files to upload.",
        )
        raise typer.Exit(code=1)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            if json_output:
                items = client.datasets.upload_files(dataset_id, files)
            else:
                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=progress_console,
                    disable=get_error_format() == "json",
                ) as progress:
                    task = progress.add_task("上传", total=len(files))
                    items = client.datasets.upload_files(
                        dataset_id,
                        files,
                        on_progress=lambda done, total, name: progress.update(
                            task, completed=done, description=f"上传 {name}"
                        ),
                    )
    if json_output:
        print_json([i.model_dump(mode="json") for i in items])
    else:
        console.print(f"[green]上传完成[/green]: {len(items)} 个文件")


@app.command("link")
def link(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """关联数据集到项目; 异步建任务时自动等待 job 完成。"""
    job = None
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.datasets.link_project(dataset_id, project_id)
            if result.async_job_id is not None:
                if not json_output:
                    console.print(f"异步建任务中 (job {result.async_job_id}) ...")
                job = wait_job(client, str(result.async_job_id), json_output)
    if json_output:
        print_json(
            {
                "link": result.model_dump(mode="json"),
                "job": job.model_dump(mode="json") if job is not None else None,
            }
        )
    elif job is None:
        console.print(f"[green]关联完成[/green]: created_tasks={result.created_tasks}")


@app.command("sniff-axis")
def sniff_axis(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """根据相机标定建议点云坐标系。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.datasets.sniff_axis_convention(dataset_id)
    print_learning_result(result, json_output)


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc


@app.command("backfill-dimensions")
def backfill_dimensions(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    batch: int = typer.Option(
        50,
        "--batch",
        min=1,
        max=200,
        help="已弃用兼容参数；服务端忽略并自行有界分页",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认回填尺寸"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受持久尺寸回填作业并返回可回放回执。"""
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    with cli_errors(json_output):
        confirm_destructive("确认回填数据集图像尺寸?", yes, json_output)
        with get_client(json_output) as client:
            result = client.datasets.backfill_dimensions(
                dataset_id,
                batch=batch,
                idempotency_key=idempotency_key,
            )
    print_learning_result(result, json_output)
    if result.status in {"failed", "canceled"}:
        raise typer.Exit(1)


@app.command("backfill-media")
def backfill_media(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认排队媒体回填"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受持久图片缩略图作业，并保留视频元数据回填。"""
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    with cli_errors(json_output):
        confirm_destructive("确认排队数据集媒体回填?", yes, json_output)
        with get_client(json_output) as client:
            result = client.datasets.backfill_media(
                dataset_id, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
    if result.status in {"failed", "canceled"}:
        raise typer.Exit(1)


@app.command("backfill-scenes")
def backfill_scenes(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    mode: str = typer.Option("auto", "--mode", help="single | per_subdirectory | auto"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只返回推断结果，不写入"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认回填场景"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """推断 scene 与 frame_index；dry-run 可先检查结果。"""
    if mode not in {"single", "per_subdirectory", "auto"}:
        raise typer.BadParameter("--mode 必须是 single、per_subdirectory 或 auto")
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    with cli_errors(json_output):
        if not dry_run:
            confirm_destructive("确认写入场景和帧索引?", yes, json_output)
        with get_client(json_output) as client:
            result = client.datasets.backfill_scenes(
                dataset_id,
                mode=mode,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
    print_learning_result(result, json_output)


@app.command("scan-items")
def scan_items(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认扫描并导入文件"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """扫描数据集存储前缀并导入新文件。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    with cli_errors(json_output):
        confirm_destructive("确认扫描并导入数据集新文件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.datasets.scan_items(
                dataset_id, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)


@app.command("import-from-connection")
def import_from_connection(
    dataset_id: str = typer.Argument(..., help="数据集 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 导入请求文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认排队导入任务"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """从已授权存储连接器排队导入，返回 job_id。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    with cli_errors(json_output):
        body = read_json_body(body_path, DatasetImportFromConnectionRequest)
        confirm_destructive("确认排队外部存储导入?", yes, json_output)
        with get_client(json_output) as client:
            result = client.datasets.import_from_connection(
                dataset_id, body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
