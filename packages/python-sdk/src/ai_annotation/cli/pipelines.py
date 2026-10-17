"""aap pipelines: named pipeline management and preannotation submission."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.pipeline_models import (
    PreannotateRequest,
    ProjectPipelineApplyRequest,
    ProjectPipelineCreate,
    ProjectPipelineUpdate,
)

app = typer.Typer(help="命名编排与项目预标注。", no_args_is_help=True)


@app.command("list")
def list_(
    scope: str | None = typer.Option(
        None, "--scope", help="private、organization 或 public"
    ),
    project_id: str | None = typer.Option(None, "--project", help="项目 UUID"),
    organization_id: str | None = typer.Option(
        None, "--organization", help="组织 UUID"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号可见的编排。"""
    with cli_errors(json_output):
        if scope is not None and scope not in {"private", "organization", "public"}:
            raise typer.BadParameter(
                "scope must be private, organization or public", param_hint="--scope"
            )
        with get_client(json_output) as client:
            result = client.pipelines.list(
                scope=scope,
                project_id=project_id,
                organization_id=organization_id,
            )
    print_learning_result(result, json_output)


@app.command("get")
def get(
    pipeline_id: str = typer.Argument(..., help="编排 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看编排。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.pipelines.get(pipeline_id)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 请求文件，或 - 读标准输入"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建命名编排。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectPipelineCreate)
        with get_client(json_output) as client:
            result = client.pipelines.create(body)
    print_learning_result(result, json_output)


@app.command("update")
def update(
    pipeline_id: str = typer.Argument(..., help="编排 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 局部更新，或 - 读标准输入"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新编排；请求体只发送显式提供的字段。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectPipelineUpdate)
        with get_client(json_output) as client:
            result = client.pipelines.update(pipeline_id, body)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    pipeline_id: str = typer.Argument(..., help="编排 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除编排。"""
    with cli_errors(json_output):
        confirm_destructive(f"确认删除编排 {pipeline_id}?", yes, json_output)
        with get_client(json_output) as client:
            client.pipelines.delete(pipeline_id)
    print_learning_result(None, json_output)


@app.command("apply")
def apply(
    project_id: str = typer.Option(..., "--project", help="目标项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 请求文件，或 - 读标准输入"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认复制到项目"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """把可见编排复制为项目私有编排；可选设为默认，不会启动推理。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectPipelineApplyRequest)
        confirm_destructive("确认将编排复制到项目并应用默认设置?", yes, json_output)
        with get_client(json_output) as client:
            result = client.pipelines.apply(project_id, body)
    print_learning_result(result, json_output)


@app.command("preannotate")
def preannotate(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 请求文件，或 - 读标准输入"
    ),
    idempotency_key: str | None = typer.Option(
        None, "--idempotency-key", help="可选的重试幂等键"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认提交预标注作业"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交预标注作业并返回 job id；不会隐式等待完成。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, PreannotateRequest)
        confirm_destructive("确认提交预标注作业?", yes, json_output)
        with get_client(json_output) as client:
            result = client.pipelines.preannotate(
                project_id, body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
