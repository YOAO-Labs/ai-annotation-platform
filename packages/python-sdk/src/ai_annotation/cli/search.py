"""CLI for visibility-filtered global search."""

from __future__ import annotations

import typer

from ai_annotation.cli._output import cli_errors, get_client, print_learning_result

app = typer.Typer(
    help="搜索当前账号可见的项目、任务、数据集和成员。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def _search_group() -> None:
    """Group search subcommands."""


@app.command("query")
def query(
    q: str = typer.Option(..., "--q", help="搜索文本，1 至 64 个字符"),
    limit: int = typer.Option(5, min=1, max=20, help="每类最多返回条数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """跨可见项目、任务、数据集和成员进行搜索。"""
    if not 1 <= len(q) <= 64:
        raise typer.BadParameter("--q 必须为 1 至 64 个字符")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.search.query(q, limit=limit)
    print_learning_result(result, json_output)
