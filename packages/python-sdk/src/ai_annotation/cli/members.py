"""aap members 子命令。"""

from __future__ import annotations

from enum import Enum

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
)

app = typer.Typer(
    help="成员: 列出、添加、移除项目成员, 并预检 / 变更项目角色。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap members list P-1[/] · [dim]aap members preview-role P-1 <member-id> --role reviewer[/]",
)


class MemberRole(str, Enum):
    annotator = "annotator"
    reviewer = "reviewer"
    viewer = "viewer"


@app.command("list")
def list_(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目成员 (项目角色与平台角色分列)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            members = client.members.list(project_id)
    if json_output:
        print_json([m.model_dump(mode="json") for m in members])
        return
    table = Table()
    table.add_column("用户")
    table.add_column("邮箱")
    table.add_column("项目角色")
    table.add_column("平台角色")
    table.add_column("加入时间")
    for m in members:
        joined = m.assigned_at.strftime("%Y-%m-%d") if m.assigned_at else "-"
        table.add_row(m.user_name, m.user_email, m.role, m.platform_role, joined)
    console.print(table)


@app.command("add")
def add(
    project_id: str = typer.Argument(..., help="项目 ID"),
    user_id: str = typer.Option(..., "--user-id", help="用户 ID"),
    role: MemberRole = typer.Option(..., "--role", help="项目角色"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """添加项目成员 (平台 viewer 只能获得 viewer 项目角色)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            member = client.members.add(project_id, user_id, role.value)
    if json_output:
        print_json(member.model_dump(mode="json"))
    else:
        console.print(f"[green]成员已添加[/green] id={member.id}")


@app.command("preview-role")
def preview_role(
    project_id: str = typer.Argument(..., help="项目 ID"),
    member_id: str = typer.Argument(..., help="成员 ID"),
    role: MemberRole = typer.Option(..., "--role", help="目标项目角色"),
    replacement_annotator_id: str | None = typer.Option(
        None, "--replacement-annotator-id", help="标注工作接替人 ID"
    ),
    replacement_reviewer_id: str | None = typer.Option(
        None, "--replacement-reviewer-id", help="审核工作接替人 ID"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检角色变更 (只读): 阻塞项、资源快照与一次性 token。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            preview = client.members.preview_role_change(
                project_id,
                member_id,
                role.value,
                replacement_annotator_id=replacement_annotator_id,
                replacement_reviewer_id=replacement_reviewer_id,
            )
    if json_output:
        print_json(preview.model_dump(mode="json"))
        return
    handoff = "需要显式交接" if preview.requires_handoff else "无需交接"
    console.print(
        f"[green]角色变更预检[/green] {preview.current_role} → "
        f"{preview.target_role} · version={preview.current_version} · {handoff}"
    )
    if preview.blockers:
        console.print(f"阻塞项: {', '.join(preview.blockers)}")
    console.print(f"preview_token={preview.preview_token}")


@app.command("change-role")
def change_role(
    project_id: str = typer.Argument(..., help="项目 ID"),
    member_id: str = typer.Argument(..., help="成员 ID"),
    role: MemberRole = typer.Option(..., "--role", help="目标项目角色"),
    expected_version: int = typer.Option(
        ..., "--expected-version", help="预检返回的成员版本"
    ),
    preview_token: str = typer.Option(
        ..., "--preview-token", help="预检返回的一次性 token"
    ),
    reason: str = typer.Option(..., "--reason", help="变更原因"),
    replacement_annotator_id: str | None = typer.Option(
        None, "--replacement-annotator-id", help="标注工作接替人 ID"
    ),
    replacement_reviewer_id: str | None = typer.Option(
        None, "--replacement-reviewer-id", help="审核工作接替人 ID"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按预检结果变更成员项目角色 (CAS + 原子交接, 需先 preview-role)。"""
    confirm_destructive(
        f"确认将成员 {member_id} 的项目角色改为 {role.value}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            member = client.members.change_role(
                project_id,
                member_id,
                role.value,
                expected_version=expected_version,
                preview_token=preview_token,
                reason=reason,
                replacement_annotator_id=replacement_annotator_id,
                replacement_reviewer_id=replacement_reviewer_id,
            )
    if json_output:
        print_json(member.model_dump(mode="json"))
    else:
        console.print(
            f"[green]角色已变更[/green] id={member.id} · role={member.role} "
            f"· version={member.version}"
        )


@app.command("remove")
def remove(
    project_id: str = typer.Argument(..., help="项目 ID"),
    member_id: str = typer.Argument(..., help="成员 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """移除项目成员。"""
    confirm_destructive(f"确认移除成员 {member_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.members.remove(project_id, member_id)
    if json_output:
        print_json({"removed": True, "project_id": project_id, "member_id": member_id})
    else:
        console.print(f"[green]成员已移除[/green] {member_id}")
