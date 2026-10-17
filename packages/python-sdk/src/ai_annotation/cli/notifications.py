"""aap notifications commands."""

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
from ai_annotation.notification_models import NotificationPreferenceUpdate

app = typer.Typer(help="通知与通知偏好。", no_args_is_help=True)


@app.command("list")
def list_(
    unread_only: bool = typer.Option(False, "--unread-only", help="只列出未读通知"),
    limit: int = typer.Option(30, "--limit", min=1, max=100),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号的通知。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.notifications.list(
                unread_only=unread_only, limit=limit, offset=offset
            )
    print_learning_result(result, json_output)


@app.command("unread-count")
def unread_count(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """返回当前账号的未读数量。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.notifications.unread_count()
    print_learning_result(result, json_output)


@app.command("mark-read")
def mark_read(
    notification_id: str = typer.Argument(..., help="通知 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """标记单条通知为已读。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.notifications.mark_read(notification_id)
    print_learning_result(result, json_output)


@app.command("mark-all-read")
def mark_all_read(
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将当前账号的所有未读通知标记为已读。"""
    with cli_errors(json_output):
        confirm_destructive("确认将所有通知标记为已读?", yes, json_output)
        with get_client(json_output) as client:
            result = client.notifications.mark_all_read()
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    notification_id: str = typer.Argument(..., help="通知 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除单条通知。"""
    with cli_errors(json_output):
        confirm_destructive(f"确认删除通知 {notification_id}?", yes, json_output)
        with get_client(json_output) as client:
            result = client.notifications.delete(notification_id)
    print_learning_result(result, json_output)


@app.command("clear-read")
def clear_read(
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除当前账号的所有已读通知。"""
    with cli_errors(json_output):
        confirm_destructive("确认删除所有已读通知?", yes, json_output)
        with get_client(json_output) as client:
            result = client.notifications.clear_read()
    print_learning_result(result, json_output)


@app.command("preferences")
def preferences(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号的通知偏好。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.notifications.preferences()
    print_learning_result(result, json_output)


@app.command("update-preference")
def update_preference(
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 请求文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新一种通知类型的一项或多项偏好。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, NotificationPreferenceUpdate)
        with get_client(json_output) as client:
            result = client.notifications.update_preference(body)
    print_learning_result(result, json_output)
