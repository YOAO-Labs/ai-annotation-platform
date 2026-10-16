"""aap learning training: 全局训练目录与队列."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result

app = typer.Typer(help="全局训练目录与队列。", no_args_is_help=True)


@app.command("catalog")
def catalog(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看目录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training.catalog()
    print_learning_result(result, json_output)


@app.command("projects")
def projects(
    search: str = typer.Option("", "--search"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出可管理项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training.projects(search=search, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("runs")
def runs(
    state: str = typer.Option("active", "--state"),
    project_id: str | None = typer.Option(None, "--project-id"),
    framework: str | None = typer.Option(None, "--framework"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看全局训练队列。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training.runs(
                state=state,
                project_id=project_id,
                framework=framework,
                limit=limit,
                offset=offset,
            )
    print_learning_result(result, json_output)
