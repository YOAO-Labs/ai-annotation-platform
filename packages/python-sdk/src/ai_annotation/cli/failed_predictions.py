"""aap failed-predictions 子命令。"""

from __future__ import annotations

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    console,
    confirm_destructive,
    get_client,
    print_json,
)

app = typer.Typer(
    help="管理项目范围内的失败预测记录。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("list")
def list_(
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(50, "--page-size", min=1, max=200),
    include_dismissed: bool = typer.Option(
        False, "--include-dismissed", help="包含已放弃记录"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出失败预测 (project_admin 只见自己负责项目)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.failed_predictions.list(
                page=page,
                page_size=page_size,
                include_dismissed=include_dismissed,
            )
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("ID")
    table.add_column("项目")
    table.add_column("任务")
    table.add_column("错误")
    table.add_column("重试")
    table.add_column("已放弃")
    for item in result.items:
        table.add_row(
            str(item.id),
            item.project_name or str(item.project_id),
            item.task_display_id or (str(item.task_id) if item.task_id else "-"),
            item.error_type,
            str(item.retry_count),
            "是" if item.dismissed_at else "否",
        )
    console.print(table)
    console.print(f"第 {result.page} 页 · 共 {result.total} 条")


@app.command("retry")
def retry(
    failed_id: str = typer.Argument(..., help="失败预测 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认排队一次模型重试"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """排队一次模型重试；HTTP 202 只确认已入队，不等待执行完成。"""
    confirm_destructive(
        f"确认对失败预测 {failed_id} 再次调用模型?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            receipt = client.failed_predictions.retry(failed_id)
    data = receipt.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(f"[green]已排队[/green] failed_id={receipt.failed_id}")


@app.command("dismiss")
def dismiss(
    failed_id: str = typer.Argument(..., help="失败预测 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认从默认列表隐藏"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软删除失败记录；可用 restore 恢复。"""
    confirm_destructive(
        f"确认放弃并隐藏失败预测 {failed_id}?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            receipt = client.failed_predictions.dismiss(failed_id)
    data = receipt.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(f"[green]已放弃[/green] failed_id={receipt.failed_id}")


@app.command("restore")
def restore(
    failed_id: str = typer.Argument(..., help="失败预测 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认恢复到默认列表"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """恢复已放弃的失败记录。"""
    confirm_destructive(
        f"确认恢复失败预测 {failed_id}?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            receipt = client.failed_predictions.restore(failed_id)
    data = receipt.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(f"[green]已恢复[/green] failed_id={receipt.failed_id}")
