"""aap invitations subcommands for administrator-managed invitations."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
)

app = typer.Typer(
    help="管理员邀请查询、导出、撤销和重发。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap invitations query --scope me --status pending --json[/]",
)


class InvitationStatusOption(str, Enum):
    pending = "pending"
    accepted = "accepted"
    expired = "expired"
    revoked = "revoked"
    all = "all"


class InvitationScopeOption(str, Enum):
    me = "me"
    all = "all"


class InvitationExportFormat(str, Enum):
    csv = "csv"
    json = "json"


def _filters(
    status: InvitationStatusOption,
    scope: InvitationScopeOption,
    project_id: str | None,
    role: str | None,
    email: str | None,
    search: str | None,
    created_from: str | None,
    created_to: str | None,
) -> dict[str, object]:
    return {
        "status": status.value,
        "scope": scope.value,
        "project_id": project_id,
        "role": role,
        "email": email,
        "search": search,
        "created_from": created_from,
        "created_to": created_to,
    }


@app.command("list")
def list_(
    status: InvitationStatusOption = typer.Option(
        InvitationStatusOption.all, "--status", help="邀请状态"
    ),
    scope: InvitationScopeOption = typer.Option(
        InvitationScopeOption.me, "--scope", help="me 或 super_admin 专用 all"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 数组"),
) -> None:
    """使用旧式全量列表；大集合请使用 query。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.list(status=status.value, scope=scope.value)
    print_learning_result(result, json_output)


@app.command("query")
def query(
    page: int = typer.Option(1, "--page", min=1),
    page_size: int = typer.Option(50, "--page-size", min=1, max=500),
    status: InvitationStatusOption = typer.Option(
        InvitationStatusOption.all, "--status"
    ),
    scope: InvitationScopeOption = typer.Option(InvitationScopeOption.me, "--scope"),
    project_id: str | None = typer.Option(None, "--project-id"),
    role: str | None = typer.Option(None, "--role"),
    email: str | None = typer.Option(None, "--email"),
    search: str | None = typer.Option(None, "--search"),
    created_from: str | None = typer.Option(
        None, "--created-from", help="ISO-8601 日期或时间"
    ),
    created_to: str | None = typer.Option(
        None, "--created-to", help="ISO-8601 日期或时间"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出分页 JSON 信封"),
) -> None:
    """按状态、范围、项目、角色、邮箱、名称或创建时间分页查询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.query(
                page=page,
                page_size=page_size,
                **_filters(
                    status,
                    scope,
                    project_id,
                    role,
                    email,
                    search,
                    created_from,
                    created_to,
                ),
            )
    print_learning_result(result, json_output)


@app.command("stats")
def stats(
    status: InvitationStatusOption = typer.Option(
        InvitationStatusOption.all, "--status"
    ),
    scope: InvitationScopeOption = typer.Option(InvitationScopeOption.me, "--scope"),
    project_id: str | None = typer.Option(None, "--project-id"),
    role: str | None = typer.Option(None, "--role"),
    email: str | None = typer.Option(None, "--email"),
    search: str | None = typer.Option(None, "--search"),
    created_from: str | None = typer.Option(None, "--created-from"),
    created_to: str | None = typer.Option(None, "--created-to"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """统计符合筛选条件的邀请状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.stats(
                **_filters(
                    status,
                    scope,
                    project_id,
                    role,
                    email,
                    search,
                    created_from,
                    created_to,
                )
            )
    print_learning_result(result, json_output)


@app.command("export")
def export(
    out: Path = typer.Option(..., "--out", help="输出文件路径"),
    format_: InvitationExportFormat = typer.Option(
        InvitationExportFormat.csv, "--format", help="导出格式: csv 或 json"
    ),
    status: InvitationStatusOption = typer.Option(
        InvitationStatusOption.all, "--status"
    ),
    scope: InvitationScopeOption = typer.Option(InvitationScopeOption.me, "--scope"),
    project_id: str | None = typer.Option(None, "--project-id"),
    role: str | None = typer.Option(None, "--role"),
    email: str | None = typer.Option(None, "--email"),
    search: str | None = typer.Option(None, "--search"),
    created_from: str | None = typer.Option(None, "--created-from"),
    created_to: str | None = typer.Option(None, "--created-to"),
    json_output: bool = typer.Option(False, "--json", help="输出文件回执 JSON"),
) -> None:
    """流式下载全部筛选结果到文件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.export(
                out,
                format=format_.value,
                **_filters(
                    status,
                    scope,
                    project_id,
                    role,
                    email,
                    search,
                    created_from,
                    created_to,
                ),
            )
    print_learning_result(result, json_output)


@app.command("revoke")
def revoke(
    invitation_id: str = typer.Argument(..., help="邀请 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """撤销未接受的邀请。"""
    confirm_destructive(f"确认撤销邀请 {invitation_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.invitations.revoke(invitation_id)
    if json_output:
        print_json({"revoked": True, "invitation_id": invitation_id})
    else:
        console.print(f"[green]邀请已撤销[/green] {invitation_id}")


@app.command("resend")
def resend(
    invitation_id: str = typer.Argument(..., help="邀请 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认轮换邀请 token"),
    show_secret: bool = typer.Option(
        False, "--show-secret", help="允许在文本输出中显示邀请链接和 token"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """轮换或恢复邀请 token；此命令不会发送邮件。"""
    if not show_secret and not json_output:
        raise typer.BadParameter(
            "重发会返回邀请链接和 token；请显式使用 --show-secret 或 --json"
        )
    confirm_destructive(f"确认重发邀请 {invitation_id} 并轮换 token?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.resend(invitation_id)
    print_learning_result(result, json_output)


@app.command("send-email")
def send_email(
    invitation_id: str = typer.Argument(..., help="邀请 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认通过 SMTP 发送邮件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """通过已配置的 SMTP 发送现有邀请，不更换 token。"""
    confirm_destructive(f"确认向邀请对象发送邮件 {invitation_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.invitations.send_email(invitation_id)
    if json_output:
        print_learning_result(result, True)
    else:
        print_learning_result(
            result.model_dump(mode="json", exclude={"invite_url"}), False
        )
