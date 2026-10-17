"""aap learning active-learning: 人工主动学习轮次与复核."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.learning_automation_models import (
    ActiveLearningDecisionCreate,
    ActiveLearningFeedbackCreate,
    ActiveLearningRoundCreate,
    ActiveLearningSelection,
)
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_learning_result,
    read_json_body,
    confirm_destructive,
)

app = typer.Typer(help="人工主动学习轮次与复核。", no_args_is_help=True)


@app.command("pool")
def pool(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(32, "--limit", min=2, max=32),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看候选池。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.pool(project_id, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("preview")
def preview(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览计划。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ActiveLearningSelection)
        with get_client(json_output) as client:
            result = client.active_learning.preview(project_id, body)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建记录。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ActiveLearningRoundCreate)
        confirm_destructive("确认执行 active-learning create?", yes, json_output)
        with get_client(json_output) as client:
            result = client.active_learning.create(project_id, body)
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
            result = client.active_learning.list(project_id, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.get(project_id, round_id)
    print_learning_result(result, json_output)


@app.command("report")
def report(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    attempt_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取冻结报告。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.report(project_id, round_id, attempt_id)
    print_learning_result(result, json_output)


@app.command("candidates")
def candidates(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出冻结候选。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.candidates(project_id, round_id)
    print_learning_result(result, json_output)


@app.command("download-image")
def download_image(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    ordinal: int = typer.Argument(...),
    out: Path = typer.Option(..., "--out", help="下载目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载证据图片。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.download_image(
                project_id, round_id, ordinal, out
            )
    print_learning_result(result, json_output)


@app.command("decisions")
def decisions(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出人工决策。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.decisions(
                project_id, round_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("decide")
def decide(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交人工决策。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ActiveLearningDecisionCreate)
        confirm_destructive("确认执行 active-learning decide?", yes, json_output)
        with get_client(json_output) as client:
            result = client.active_learning.decide(project_id, round_id, body)
    print_learning_result(result, json_output)


@app.command("feedback")
def feedback(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出后续反馈。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.active_learning.feedback(
                project_id, round_id, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("create-feedback")
def create_feedback(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    body_path: Path = typer.Option(..., "--body", help="UTF-8 JSON 请求文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交后续事实反馈。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ActiveLearningFeedbackCreate)
        confirm_destructive(
            "确认执行 active-learning create-feedback?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.active_learning.create_feedback(project_id, round_id, body)
    print_learning_result(result, json_output)


@app.command("cancel")
def cancel(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求停止执行。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 active-learning cancel?", yes, json_output)
        with get_client(json_output) as client:
            result = client.active_learning.cancel(project_id, round_id)
    print_learning_result(result, json_output)


@app.command("retry")
def retry(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    round_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行变更"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试失败记录。"""
    with cli_errors(json_output):
        confirm_destructive("确认执行 active-learning retry?", yes, json_output)
        with get_client(json_output) as client:
            result = client.active_learning.retry(project_id, round_id)
    print_learning_result(result, json_output)
