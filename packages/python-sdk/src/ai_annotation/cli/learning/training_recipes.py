"""aap learning recipes: 训练配方与复用配置."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.training_models import (
    TrainingConfigurationResolve,
    TrainingRecipeWrite,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="训练配方与复用配置。", no_args_is_help=True)


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    include_archived: bool = typer.Option(
        False, "--include-archived/--no-include-archived"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_recipes.list(
                project_id,
                limit=limit,
                offset=offset,
                include_archived=include_archived,
            )
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingRecipeWrite)
        with get_client(json_output) as client:
            result = client.training_recipes.create(project_id, body)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    recipe_id: str = typer.Argument(...),
    revision_id: str | None = typer.Option(None, "--revision-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_recipes.get(
                project_id, recipe_id, revision_id=revision_id
            )
    print_learning_result(result, json_output)


@app.command("revisions")
def revisions(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    recipe_id: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出配方修订。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_recipes.revisions(
                project_id, recipe_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("create-revision")
def create_revision(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    recipe_id: str = typer.Argument(...),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建配方修订。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingRecipeWrite)
        with get_client(json_output) as client:
            result = client.training_recipes.create_revision(
                project_id, recipe_id, body
            )
    print_learning_result(result, json_output)


@app.command("resolve")
def resolve(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """校验完整配置。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingConfigurationResolve)
        with get_client(json_output) as client:
            result = client.training_recipes.resolve(project_id, body)
    print_learning_result(result, json_output)


@app.command("clone-candidate")
def clone_candidate(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str | None = typer.Option(None, "--attempt-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取实验复用候选。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_recipes.clone_candidate(
                project_id, run_id, attempt_id=attempt_id
            )
    print_learning_result(result, json_output)


@app.command("archive")
def archive(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    recipe_id: str = typer.Argument(...),
    expected_head_revision: int = typer.Option(..., "--expected-head-revision", min=1),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """归档记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 recipes archive?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_recipes.archive(
                project_id, recipe_id, expected_head_revision=expected_head_revision
            )
    print_learning_result(result, json_output)
