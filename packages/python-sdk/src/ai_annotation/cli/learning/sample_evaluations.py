"""aap learning evaluations: 冻结逐样本评估."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.model_management_models import (
    SampleEvaluationCreateRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="冻结逐样本评估。", no_args_is_help=True)


@app.command("qualification")
def qualification(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    model_version_id: str = typer.Option(..., "--model-version-id"),
    deployment_id: str = typer.Option(..., "--deployment-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """检查逐样本评估资格。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.qualification(
                project_id,
                model_version_id=model_version_id,
                deployment_id=deployment_id,
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
        body = read_json_body(body_path, SampleEvaluationCreateRequest)
        with get_client(json_output) as client:
            result = client.sample_evaluations.create(project_id, body)
    print_learning_result(result, json_output)


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
            result = client.sample_evaluations.list(
                project_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.get(project_id, evaluation_id)
    print_learning_result(result, json_output)


@app.command("compare")
def compare(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    left_id: str = typer.Option(..., "--left-id"),
    right_id: str = typer.Option(..., "--right-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """比较冻结证据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.compare(
                project_id, left_id=left_id, right_id=right_id
            )
    print_learning_result(result, json_output)


@app.command("report")
def report(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取冻结报告。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.report(
                project_id, evaluation_id, attempt_id
            )
    print_learning_result(result, json_output)


@app.command("samples")
def samples(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    class_index: int | None = typer.Option(None, "--class-index", min=0),
    error: str | None = typer.Option(None, "--error"),
    confidence_min: float | None = typer.Option(None, "--confidence-min", min=0, max=1),
    confidence_max: float | None = typer.Option(None, "--confidence-max", min=0, max=1),
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(12, "--limit", min=1, max=32),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出样本。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.samples(
                project_id,
                evaluation_id,
                attempt_id,
                class_index=class_index,
                error=error,
                confidence_min=confidence_min,
                confidence_max=confidence_max,
                offset=offset,
                limit=limit,
            )
    print_learning_result(result, json_output)


@app.command("source")
def source(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    ordinal: int = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """核对当前样本来源。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.source(
                project_id, evaluation_id, attempt_id, ordinal
            )
    print_learning_result(result, json_output)


@app.command("download-image")
def download_image(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    ordinal: int = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载证据图片。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.sample_evaluations.download_image(
                project_id, evaluation_id, attempt_id, ordinal, out
            )
    print_learning_result(result, json_output)


@app.command("cancel")
def cancel(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求停止执行。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 evaluations cancel?", yes, json_output)
        with get_client(json_output) as client:
            result = client.sample_evaluations.cancel(project_id, evaluation_id)
    print_learning_result(result, json_output)


@app.command("retry")
def retry(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试失败记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 evaluations retry?", yes, json_output)
        with get_client(json_output) as client:
            result = client.sample_evaluations.retry(project_id, evaluation_id)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    evaluation_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 evaluations delete?", yes, json_output)
        with get_client(json_output) as client:
            result = client.sample_evaluations.delete(project_id, evaluation_id)
    print_learning_result(result, json_output)
