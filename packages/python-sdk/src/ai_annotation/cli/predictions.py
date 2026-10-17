"""aap predictions 子命令。"""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    console,
    confirm_destructive,
    get_client,
    print_json,
)
from ai_annotation.management_operation_models import PredictionPurgeRequest

app = typer.Typer(
    help="预测结果导入: 把外部模型产出 (aap_json / coco / yolo) 导入项目。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "示例: [dim]aap predictions import P-1 preds.json[/] · "
        "[dim]aap predictions import P-1 preds.json --format coco --dry-run[/]"
    ),
)


@app.command("import")
def import_(
    project_id: str = typer.Argument(..., help="项目 ID"),
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="预测结果文件"),
    format_: str = typer.Option("aap_json", "--format", help="aap_json / coco / yolo"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验不落库"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """导入外部预测结果到项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.predictions.import_file(
                project_id, file, format=format_, dry_run=dry_run
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    prefix = "[yellow](dry-run)[/yellow] " if result.dry_run else ""
    console.print(
        f"{prefix}导入完成: imported={result.imported} "
        f"skipped={result.skipped} errors={len(result.errors)}"
    )
    for err in result.errors[:5]:
        console.print(f"[red]- {err}[/red]")


@app.command("purge")
def purge(
    project_id: str = typer.Argument(..., help="项目 ID (需项目负责人或 super_admin)"),
    source_scope: str = typer.Option(
        "external_import", "--source-scope", help="external_import / ml_backend / all"
    ),
    task_ids: list[str] = typer.Option(
        [], "--task-id", help="限制到指定任务，可重复；缺省覆盖整个项目范围"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只返回匹配数量，不删除"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除匹配预测"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按来源删除项目预测；先用 --dry-run 查看预计数量。"""
    with cli_errors(json_output):
        body = PredictionPurgeRequest(
            source_scope=source_scope,
            task_ids=task_ids or None,
            dry_run=dry_run,
        )
        if not dry_run:
            confirm_destructive(
                f"确认清理项目 {project_id} 中来源为 {source_scope} 的预测?",
                yes,
                json_output,
            )
        with get_client(json_output) as client:
            result = client.predictions.purge(project_id, body)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        prefix = "[yellow](dry-run)[/yellow] " if result.dry_run else ""
        console.print(
            f"{prefix}范围 {result.source_scope}: 删除 {result.counts.total} 条 "
            f"(ML backend={result.counts.ml_backend}, 外部导入={result.counts.external_import}, "
            f"未知={result.counts.unknown})"
        )
