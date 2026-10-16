"""aap learning previews: 有界增强预览."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.training_models import (
    TrainingPreviewCreate,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="有界增强预览。", no_args_is_help=True)


@app.command("capabilities")
def capabilities(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看增强预览能力。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_previews.capabilities(project_id)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingPreviewCreate)
        with get_client(json_output) as client:
            result = client.training_previews.create(project_id, body)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    preview_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_previews.get(project_id, preview_id)
    print_learning_result(result, json_output)


@app.command("download-image")
def download_image(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    preview_id: str = typer.Argument(...),
    index: int = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载证据图片。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_previews.download_image(
                project_id, preview_id, index, out
            )
    print_learning_result(result, json_output)


@app.command("cancel")
def cancel(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    preview_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求停止执行。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 previews cancel?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_previews.cancel(project_id, preview_id)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    preview_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 previews delete?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_previews.delete(project_id, preview_id)
    print_learning_result(result, json_output)
