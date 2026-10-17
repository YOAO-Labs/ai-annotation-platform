"""aap learning runs: 训练实验、尝试和观测."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.training_models import (
    TrainingRunCreate,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="训练实验、尝试和观测。", no_args_is_help=True)


@app.command("catalog")
def catalog(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看目录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.catalog(project_id)
    print_learning_result(result, json_output)


@app.command("parameter-schema")
def parameter_schema(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    framework: str = typer.Option(..., "--framework"),
    base_model: str = typer.Option(..., "--base-model"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取参数定义。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.parameter_schema(
                project_id, framework=framework, base_model=base_model
            )
    print_learning_result(result, json_output)


@app.command("initialization-source")
def initialization_source(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    model_id: str = typer.Argument(...),
    framework: str = typer.Option(..., "--framework"),
    base_model: str = typer.Option(..., "--base-model"),
    dataset_id: str | None = typer.Option(None, "--dataset-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """检查初始化来源资格。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.initialization_source(
                project_id,
                model_id,
                framework=framework,
                base_model=base_model,
                dataset_id=dataset_id,
            )
    print_learning_result(result, json_output)


@app.command("preflight")
def preflight(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检请求。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingRunCreate)
        with get_client(json_output) as client:
            result = client.training_runs.preflight(project_id, body)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TrainingRunCreate)
        with get_client(json_output) as client:
            result = client.training_runs.create(project_id, body)
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
            result = client.training_runs.list(project_id, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.get(project_id, run_id)
    print_learning_result(result, json_output)


@app.command("events")
def events(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str | None = typer.Option(None, "--attempt-id"),
    after: int = typer.Option(0, "--after", min=0),
    latest: bool = typer.Option(False, "--latest/--no-latest"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取训练事件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.events(
                project_id, run_id, attempt_id=attempt_id, after=after, latest=latest
            )
    print_learning_result(result, json_output)


@app.command("evaluation")
def evaluation(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取最终评估。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.evaluation(
                project_id, run_id, attempt_id, offset=offset, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("telemetry")
def telemetry(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    after: int | None = typer.Option(None, "--after", min=0, max=2147483647),
    limit: int = typer.Option(120, "--limit", min=1, max=120),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取观测页。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.telemetry(
                project_id, run_id, attempt_id, after=after, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("logs")
def logs(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    after: int | None = typer.Option(None, "--after", min=0, max=2147483647),
    before: int | None = typer.Option(None, "--before", min=1, max=2147483647),
    limit: int = typer.Option(120, "--limit", min=1, max=120),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取日志页。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.logs(
                project_id, run_id, attempt_id, after=after, before=before, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("download-artifact")
def download_artifact(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    name: str = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载执行产物。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.download_artifact(
                project_id, run_id, attempt_id, name, out
            )
    print_learning_result(result, json_output)


@app.command("recovery")
def recovery(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看恢复资格。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.training_runs.recovery(project_id, run_id)
    print_learning_result(result, json_output)


@app.command("cancel")
def cancel(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求停止执行。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 runs cancel?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_runs.cancel(project_id, run_id)
    print_learning_result(result, json_output)


@app.command("resume")
def resume(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求恢复执行。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 runs resume?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_runs.resume(project_id, run_id)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    run_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 runs delete?", yes, json_output)
        with get_client(json_output) as client:
            result = client.training_runs.delete(project_id, run_id)
    print_learning_result(result, json_output)
