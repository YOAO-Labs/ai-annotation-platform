"""Read annotation and task lifecycle histories."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result

app = typer.Typer(
    help="读取标注生命周期和任务审计历史。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("annotation")
def annotation(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取标注历史、相关任务流转和评论记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.annotation_history.annotation(annotation_id)
    print_learning_result(result, json_output)


@app.command("task")
def task(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取任务和其标注项的审计历史。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.annotation_history.task(task_id)
    print_learning_result(result, json_output)
