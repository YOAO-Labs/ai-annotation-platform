"""aap learning releases: 项目模型发布与回退."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.model_management_models import (
    ModelReleaseCreateRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="项目模型发布与回退。", no_args_is_help=True)


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.model_releases.list(project_id, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ModelReleaseCreateRequest)
        confirm_destructive("确认执行 releases create?", yes, json_output)
        with get_client(json_output) as client:
            result = client.model_releases.create(project_id, body)
    print_learning_result(result, json_output)
