"""aap feedbacks 子命令。"""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)
from ai_annotation.models import AnnotationFeedbackPatch, AnnotationFeedbackReply

app = typer.Typer(help="反馈线程与问题状态。", no_args_is_help=True)


@app.command("thread")
def thread(
    root_feedback_id: str = typer.Argument(..., help="根反馈 ID"),
    cursor: str | None = typer.Option(None, "--cursor", help="上一页返回的游标"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取反馈根项和一页活跃回复。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.feedbacks.thread(
                root_feedback_id, cursor=cursor, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("reply")
def reply(
    feedback_id: str = typer.Argument(..., help="父反馈 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 请求文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """回复反馈；请求 JSON 包含 body，可选 attachments。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AnnotationFeedbackReply)
        with get_client(json_output) as client:
            result = client.feedbacks.reply(feedback_id, body)
    print_learning_result(result, json_output)


@app.command("patch")
def patch(
    feedback_id: str = typer.Argument(..., help="反馈 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 补丁文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """修改显式提供的反馈字段。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AnnotationFeedbackPatch)
        with get_client(json_output) as client:
            result = client.feedbacks.patch(feedback_id, body)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    feedback_id: str = typer.Argument(..., help="反馈 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认软删除"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软删除反馈；删除权限由服务端按作者和项目角色判定。"""
    confirm_destructive(f"确认软删除反馈 {feedback_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.feedbacks.delete(feedback_id)
    if json_output:
        print_json({"deleted": True, "feedback_id": feedback_id})
    else:
        console.print(f"[green]反馈已软删除[/green] {feedback_id}")
