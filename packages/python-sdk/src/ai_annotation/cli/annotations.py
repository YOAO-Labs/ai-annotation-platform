"""aap annotations 命令。"""

import json

import typer
from rich.table import Table

from ai_annotation.cli._output import cli_errors, console, get_client, print_json

app = typer.Typer(
    help="标注批量操作。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("list")
def list_(
    task_id: str = typer.Argument(..., help="任务 ID"),
    video_segment_id: str | None = typer.Option(
        None, "--video-segment-id", help="视频片段 ID"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出任务标注，可按视频片段筛选。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            annotations = client.annotations.list(
                task_id, video_segment_id=video_segment_id
            )
    if json_output:
        print_json([annotation.model_dump(mode="json") for annotation in annotations])
        return
    table = Table()
    table.add_column("ID")
    table.add_column("类型")
    table.add_column("类别")
    for annotation in annotations:
        table.add_row(
            str(annotation.id), annotation.annotation_type, annotation.class_name
        )
    console.print(table)


@app.command("bulk-update")
def bulk_update(
    annotation_ids: list[str] = typer.Option(..., "--id", help="标注 ID，可重复"),
    class_name: str | None = typer.Option(None, "--class-name", help="类别"),
    attributes_json: str | None = typer.Option(
        None, "--attributes-json", help="属性 JSON 对象"
    ),
    z_order: int | None = typer.Option(None, "--z-order", help="层级"),
    locked: bool | None = typer.Option(None, "--locked/--unlocked", help="锁定状态"),
    hidden: bool | None = typer.Option(None, "--hidden/--visible", help="隐藏状态"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量修改标注的类别、属性或显示状态。"""
    patch = {
        key: value
        for key, value in {
            "class_name": class_name,
            "z_order": z_order,
            "is_locked": locked,
            "is_hidden": hidden,
        }.items()
        if value is not None
    }
    if attributes_json is not None:
        try:
            attributes = json.loads(attributes_json)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(
                f"--attributes-json 不是有效 JSON: {exc.msg}"
            ) from None
        if not isinstance(attributes, dict):
            raise typer.BadParameter("--attributes-json 必须是 JSON 对象")
        patch["attributes"] = attributes
    if not patch:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.annotations.bulk_update(annotation_ids, **patch)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]已更新 {result.updated_count} 个标注[/green]")
