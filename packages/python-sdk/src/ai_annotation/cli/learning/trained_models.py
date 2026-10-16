"""aap learning models: 受控模型、部署与候选决策."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.model_management_models import (
    ModelDecisionCreateRequest,
    ModelDeploymentCreateRequest,
    TrainedModelCreateRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="受控模型、部署与候选决策。", no_args_is_help=True)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainedModelCreateRequest)
        with get_client(json_output) as client:
            result = client.trained_models.create(project_id, body)
    print_learning_result(result, json_output)


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    archive: str = typer.Option("exclude", "--archive"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.trained_models.list(
                project_id, limit=limit, offset=offset, archive=archive
            )
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.trained_models.get(project_id, version_id)
    print_learning_result(result, json_output)


@app.command("download")
def download(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载已校验文件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.trained_models.download(project_id, version_id, out)
    print_learning_result(result, json_output)


@app.command("deploy")
def deploy(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """部署受控模型。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ModelDeploymentCreateRequest)
        confirm_destructive("确认执行 models deploy?", yes, json_output)
        with get_client(json_output) as client:
            result = client.trained_models.deploy(project_id, version_id, body)
    print_learning_result(result, json_output)


@app.command("lineage")
def lineage(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    kind: str = typer.Argument(...),
    node_id: str = typer.Argument(...),
    relation: str | None = typer.Option(None, "--relation"),
    cursor: str | None = typer.Option(None, "--cursor"),
    limit: int = typer.Option(20, "--limit", min=1, max=20),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取来源谱系。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.trained_models.lineage(
                project_id, kind, node_id, relation=relation, cursor=cursor, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("decisions")
def decisions(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    before: int | None = typer.Option(None, "--before", min=1),
    limit: int = typer.Option(20, "--limit", min=1, max=20),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出人工决策。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.trained_models.decisions(
                project_id, version_id, before=before, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("decide")
def decide(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交人工决策。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ModelDecisionCreateRequest)
        confirm_destructive("确认执行 models decide?", yes, json_output)
        with get_client(json_output) as client:
            result = client.trained_models.decide(project_id, version_id, body)
    print_learning_result(result, json_output)
