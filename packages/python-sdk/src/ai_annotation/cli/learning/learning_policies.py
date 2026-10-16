"""aap learning policies: 学习策略和触发历史."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.model_management_models import (
    LearningPolicySaveRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="学习策略和触发历史。", no_args_is_help=True)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.learning_policies.get(project_id)
    print_learning_result(result, json_output)


@app.command("save")
def save(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """保存显式策略。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, LearningPolicySaveRequest)
        confirm_destructive("确认执行 policies save?", yes, json_output)
        with get_client(json_output) as client:
            result = client.learning_policies.save(project_id, body)
    print_learning_result(result, json_output)


@app.command("check")
def check(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """检查策略并允许触发训练。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 policies check?", yes, json_output)
        with get_client(json_output) as client:
            result = client.learning_policies.check(project_id)
    print_learning_result(result, json_output)


@app.command("disable")
def disable(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    expected_revision: int = typer.Option(..., "--expected-revision", min=1),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """停用策略。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 policies disable?", yes, json_output)
        with get_client(json_output) as client:
            result = client.learning_policies.disable(
                project_id, expected_revision=expected_revision
            )
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 policies delete?", yes, json_output)
        with get_client(json_output) as client:
            result = client.learning_policies.delete(project_id)
    print_learning_result(result, json_output)


@app.command("triggers")
def triggers(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出策略触发记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.learning_policies.triggers(
                project_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)
