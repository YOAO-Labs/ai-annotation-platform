"""yoao predictions 子命令。"""

from __future__ import annotations

from pathlib import Path

import typer

from yoao.cli._output import (
    cli_errors,
    console,
    confirm_destructive,
    get_client,
    print_import_result,
    print_json,
)
from yoao.management_operation_models import PredictionPurgeRequest

app = typer.Typer(
    help="导入外部预测供人工审阅，或按来源清理项目预测。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        '示例: [dim]yoao predictions import "$PROJECT_ID" preds.json --dry-run[/] · '
        '[dim]yoao predictions purge "$PROJECT_ID" --dry-run[/]'
    ),
)

_SOURCE_LABELS = {
    "external_import": "外部导入",
    "ml_backend": "模型服务",
    "all": "全部来源",
}


@app.command("import")
def import_(
    project_id: str = typer.Argument(..., help="项目 ID"),
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="预测结果文件"),
    format_: str = typer.Option(
        "native_json", "--format", help="native_json / coco / yolo"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="预览待导入的预测，不写入"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """追加待审预测，不修改正式标注。原生 JSON 只读取预测。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.predictions.import_file(
                project_id, file, format=format_, dry_run=dry_run
            )
    print_import_result(result, "待审预测", json_output)


@app.command("purge")
def purge(
    project_id: str = typer.Argument(..., help="项目 ID (需项目负责人或 super_admin)"),
    source_scope: str = typer.Option(
        "external_import",
        "--source-scope",
        help="external_import：外部导入；ml_backend：模型服务；all：全部来源",
    ),
    task_ids: list[str] = typer.Option(
        [], "--task-id", help="限制到指定任务，可重复；缺省覆盖整个项目范围"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只返回匹配数量，不删除"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除匹配预测"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按来源清理预测，不删除正式标注。先用 --dry-run 查看影响范围。"""
    with cli_errors(json_output):
        body = PredictionPurgeRequest(
            source_scope=source_scope,
            task_ids=task_ids or None,
            dry_run=dry_run,
        )
        if not dry_run:
            confirm_destructive(
                f"确认删除项目 {project_id} 所选范围内的预测"
                f"（来源：{_SOURCE_LABELS[body.source_scope]}）？",
                yes,
                json_output,
            )
        with get_client(json_output) as client:
            result = client.predictions.purge(project_id, body)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        action = "预览（未删除）：预计删除" if result.dry_run else "清理结果：已删除"
        console.print(
            f"{action} {result.counts.total} 条预测（范围：{_SOURCE_LABELS[result.source_scope]}；"
            f"模型服务 {result.counts.ml_backend}，外部导入 {result.counts.external_import}，"
            f"其他来源 {result.counts.unknown}）。"
        )
