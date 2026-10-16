"""aap learning datasets: 冻结训练版本与数据分析."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.training_models import (
    TrainingDatasetCreate,
    TrainingDatasetSelection,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="冻结训练版本与数据分析。", no_args_is_help=True)


@app.command("preflight")
def preflight(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检请求。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingDatasetSelection)
        with get_client(json_output) as client:
            result = client.training_datasets.preflight(project_id, body)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingDatasetCreate)
        with get_client(json_output) as client:
            result = client.training_datasets.create(project_id, body)
    print_learning_result(result, json_output)


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    status: str | None = typer.Option(None, "--status"),
    task: str | None = typer.Option(None, "--task"),
    task_dataset_id: str | None = typer.Option(None, "--task-dataset-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.list(
                project_id,
                limit=limit,
                offset=offset,
                status=status,
                task=task,
                task_dataset_id=task_dataset_id,
            )
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    include_manifest: bool = typer.Option(
        True, "--include-manifest/--no-include-manifest"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.get(
                project_id, version_id, include_manifest=include_manifest
            )
    print_learning_result(result, json_output)


@app.command("retry")
def retry(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试失败记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 datasets retry?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_datasets.retry(project_id, version_id)
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
            result = client.training_datasets.download(project_id, version_id, out)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 datasets delete?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_datasets.delete(project_id, version_id)
    print_learning_result(result, json_output)


@app.command("insights")
def insights(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看冻结数据统计。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.insights(project_id, version_id)
    print_learning_result(result, json_output)


@app.command("classes")
def classes(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0, max=2147483647),
    search: str = typer.Option("", "--search"),
    order_by: str = typer.Option("index", "--order-by"),
    split: str | None = typer.Option(None, "--split"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看类别统计。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.classes(
                project_id,
                version_id,
                limit=limit,
                offset=offset,
                search=search,
                order_by=order_by,
                split=split,
            )
    print_learning_result(result, json_output)


@app.command("compare")
def compare(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    baseline_id: str = typer.Option(..., "--baseline-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """比较冻结证据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.compare(
                project_id, version_id, baseline_id=baseline_id
            )
    print_learning_result(result, json_output)


@app.command("samples")
def samples(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    limit: int = typer.Option(25, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0, max=2147483647),
    split: str | None = typer.Option(None, "--split"),
    class_index: int | None = typer.Option(None, "--class-index", min=0),
    content_sha256: str | None = typer.Option(None, "--content-sha256"),
    kind: str = typer.Option("all", "--kind"),
    baseline_id: str | None = typer.Option(None, "--baseline-id"),
    change: str = typer.Option("all", "--change"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出样本。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.samples(
                project_id,
                version_id,
                limit=limit,
                offset=offset,
                split=split,
                class_index=class_index,
                content_sha256=content_sha256,
                kind=kind,
                baseline_id=baseline_id,
                change=change,
            )
    print_learning_result(result, json_output)


@app.command("sample")
def sample(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    limit: int = typer.Option(500, "--limit", min=1, max=1000),
    offset: int = typer.Option(0, "--offset", min=0, max=2147483647),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看样本标签。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.sample(
                project_id, version_id, task_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("download-media")
def download_media(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    version_id: str = typer.Argument(...),
    task_id: str = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    preview: bool = typer.Option(False, "--preview/--no-preview"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载冻结样本媒体。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_datasets.download_media(
                project_id, version_id, task_id, out, preview=preview
            )
    print_learning_result(result, json_output)
