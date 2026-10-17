"""aap learning task-datasets: 项目任务数据集版本流."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result

app = typer.Typer(help="项目任务数据集版本流。", no_args_is_help=True)


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_datasets.list(project_id)
    print_learning_result(result, json_output)


@app.command("unrecognized-versions")
def unrecognized_versions(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出未识别版本。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_datasets.unrecognized_versions(
                project_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)
