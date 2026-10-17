"""Offline API, SDK, and CLI operation discovery commands."""

from __future__ import annotations

import typer

from ai_annotation.discovery import describe_operation, list_capabilities
from ai_annotation.cli._output import cli_errors, print_learning_result


def capabilities(
    tag: str | None = typer.Option(None, "--tag", help="按 OpenAPI tag 过滤"),
    status: str | None = typer.Option(
        None, "--status", help="covered, excluded, planned 或 uncovered"
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前 API 操作及其 SDK/CLI 覆盖情况。"""
    with cli_errors(json_output):
        try:
            result = list_capabilities(
                tag=tag, status=status, limit=limit, offset=offset
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    print_learning_result(result, json_output)


def describe(
    name: str = typer.Argument(..., help="HTTP 路由、operationId、SDK 名称或 CLI 别名"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """展开一个 API 操作的参数、请求模型、返回类型和约束。"""
    with cli_errors(json_output):
        try:
            result = describe_operation(name)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    print_learning_result(result, json_output)
