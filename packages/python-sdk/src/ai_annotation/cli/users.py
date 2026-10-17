"""Account, invitation-creation, and lifecycle administration commands."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    read_json_body,
)
from ai_annotation.user_management_models import (
    BulkGroupAssignmentRequest,
    BulkInviteRequest,
    ChangeUserRoleRequest,
    DeleteUserRequest,
    InvitationCreate,
    OffboardingCommitRequest,
    ReactivateRequest,
)

app = typer.Typer(
    help="账户管理: 查询、邀请、分组和生命周期操作。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "示例: [dim]aap users query --page 2 --page-size 100 --json[/] · "
        "[dim]aap users offboarding-preview <user-id> --json[/]"
    ),
)


class UserStatus(str, Enum):
    active = "active"
    inactive = "inactive"
    all = "all"


class AccountRole(str, Enum):
    super_admin = "super_admin"
    project_admin = "project_admin"
    employee = "employee"
    viewer = "viewer"


class MembershipRole(str, Enum):
    annotator = "annotator"
    reviewer = "reviewer"
    viewer = "viewer"


def _text_users(users: list[object]) -> None:
    table = Table()
    table.add_column("ID")
    table.add_column("姓名")
    table.add_column("邮箱")
    table.add_column("平台角色")
    table.add_column("状态")
    table.add_column("数据组")
    for user in users:
        table.add_row(
            str(getattr(user, "id", "")),
            str(getattr(user, "name", "")),
            str(getattr(user, "email", "")),
            str(getattr(user, "role", "")),
            str(getattr(user, "status", "")),
            str(getattr(user, "group_name", None) or "-"),
        )
    console.print(table)


@app.command("list")
def list_(
    role: AccountRole | None = typer.Option(None, "--role", help="平台角色过滤"),
    status: UserStatus = typer.Option(UserStatus.active, "--status", help="账号状态"),
    project_id: str | None = typer.Option(None, "--project-id", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取兼容数组接口；项目管理员忽略 --project-id，分页检索请用 query。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            users = client.users.list(
                role=role.value if role else None,
                status=status.value,
                project_id=project_id,
            )
    if json_output:
        print_json([user.model_dump(mode="json") for user in users])
    else:
        _text_users(users)


@app.command("query")
def query(
    page: int = typer.Option(1, min=1, help="页码，从 1 开始"),
    page_size: int = typer.Option(50, min=1, max=500, help="每页条数"),
    status: UserStatus = typer.Option(UserStatus.active, "--status", help="账号状态"),
    project_id: str | None = typer.Option(None, "--project-id", help="项目 ID"),
    group_id: str | None = typer.Option(None, "--group-id", help="数据组 ID"),
    role: AccountRole | None = typer.Option(None, "--role", help="平台角色"),
    search: str | None = typer.Option(None, "--search", help="姓名或邮箱关键字"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """分页查询并应用 project_id 过滤，保留 API 返回的管理能力标记。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.query(
                page=page,
                page_size=page_size,
                status=status.value,
                project_id=project_id,
                group_id=group_id,
                role=role.value if role else None,
                search=search,
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    _text_users(result.items)
    console.print(
        f"第 {result.page}/{result.pages} 页 · 共 {result.total} 个 · "
        f"page_size={result.page_size}"
    )
    for user in result.items:
        if not user.is_managed or not user.is_lifecycle_managed:
            flags = []
            if not user.is_managed:
                flags.append("只读")
            if not user.is_lifecycle_managed:
                flags.append("不可生命周期操作")
            console.print(f"{user.id}: {' / '.join(flags)}")


@app.command("stats")
def stats(
    status: UserStatus = typer.Option(UserStatus.active, "--status", help="账号状态"),
    project_id: str | None = typer.Option(None, "--project-id", help="项目 ID"),
    group_id: str | None = typer.Option(None, "--group-id", help="数据组 ID"),
    role: AccountRole | None = typer.Option(None, "--role", help="平台角色"),
    search: str | None = typer.Option(None, "--search", help="姓名或邮箱关键字"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前调用者可见范围内的用户统计。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.stats(
                status=status.value,
                project_id=project_id,
                group_id=group_id,
                role=role.value if role else None,
                search=search,
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"总数 {result.total} · 在线 {result.online} · "
            f"近 7 天活跃 {result.weekly_active}"
        )


@app.command("export")
def export(
    dest: Path = typer.Argument(..., help="目标文件；父目录必须已存在"),
    format_: str = typer.Option("csv", "--format", help="csv 或 json"),
    status: UserStatus = typer.Option(UserStatus.active, "--status", help="账号状态"),
    project_id: str | None = typer.Option(None, "--project-id", help="项目 ID"),
    group_id: str | None = typer.Option(None, "--group-id", help="数据组 ID"),
    role: AccountRole | None = typer.Option(None, "--role", help="平台角色"),
    search: str | None = typer.Option(None, "--search", help="姓名或邮箱关键字"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """流式导出用户数据；导出文件包含审计元数据。"""
    if format_ not in {"csv", "json"}:
        raise typer.BadParameter("--format 只能是 csv 或 json")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            path = client.users.export(
                dest,
                format=format_,
                status=status.value,
                project_id=project_id,
                group_id=group_id,
                role=role.value if role else None,
                search=search,
            )
    if json_output:
        print_json({"path": str(path)})
    else:
        console.print(f"[green]已导出[/green] {path}")


@app.command("invite")
def invite(
    email: str = typer.Option(..., "--email", help="受邀邮箱"),
    role: AccountRole = typer.Option(..., "--role", help="平台账号角色"),
    group_name: str | None = typer.Option(None, "--group-name", help="数据组名称"),
    project_id: str | None = typer.Option(None, "--project-id", help="可选项目 ID"),
    project_member_role: MembershipRole | None = typer.Option(
        None, "--project-member-role", help="项目角色；指定项目时必填"
    ),
    show_secret: bool = typer.Option(
        False, "--show-secret", help="在文本输出中显示一次性 token 和邀请链接"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过写入确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建邀请；不会发送邮件，返回的 token / 链接仅在此处展示。"""
    if project_id and project_member_role is None:
        raise typer.BadParameter("指定 --project-id 时必须提供 --project-member-role")
    if not json_output and not show_secret:
        raise typer.BadParameter("邀请凭证只返回一次；文本模式请显式传 --show-secret")
    try:
        request = InvitationCreate(
            email=email,
            role=role.value,
            group_name=group_name,
            project_id=project_id,
            project_member_role=(
                project_member_role.value if project_member_role else None
            ),
        )
    except ValidationError as exc:
        raise typer.BadParameter("邀请参数无效；请检查邮箱和项目角色") from exc
    confirm_destructive(f"确认创建发往 {request.email} 的邀请?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.invite(request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]邀请已创建[/green] expires={result.expires_at.isoformat()}"
        )
        console.print(f"token={result.token}")
        console.print(f"invite_url={result.invite_url}")


def _bulk_invite_command(
    request_file: Path,
    *,
    preview: bool,
    show_secret: bool,
    yes: bool,
    json_output: bool,
) -> None:
    if not preview and not json_output and not show_secret:
        raise typer.BadParameter(
            "批量邀请凭证只返回一次；文本模式请显式传 --show-secret"
        )
    with cli_errors(json_output):
        request = read_json_body(request_file, BulkInviteRequest)
        if not preview:
            confirm_destructive(
                f"确认尝试创建 {len(request.items)} 条邀请? 部分成功会单独提交。",
                yes,
                json_output,
            )
        with get_client(json_output) as client:
            result = (
                client.users.preview_bulk_invites(request)
                if preview
                else client.users.bulk_invite(request)
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"{'预检' if preview else '执行'}完成 · 成功 {result.succeeded} · 失败 {result.failed}"
        )
        for item in result.items:
            status = "OK" if item.ok else f"失败: {item.error or '未知错误'}"
            console.print(f"[{item.index}] {item.email}: {status}")
            if item.ok and item.invite_url and show_secret:
                console.print(f"  invite_url={item.invite_url}")
                if item.token:
                    console.print(f"  token={item.token}")
        if not preview and not show_secret and result.succeeded:
            console.print(
                "邀请凭证未显示；请通过 invitations resend 轮换凭证后安全分发。"
            )
    if not preview and result.failed:
        raise typer.Exit(code=1)


@app.command("bulk-invite-preview")
def bulk_invite_preview(
    request_file: Path = typer.Argument(
        ..., help="BulkInviteRequest JSON 文件，或 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检批量邀请；不持久化邀请行或返回 token。"""
    _bulk_invite_command(
        request_file, preview=True, show_secret=False, yes=True, json_output=json_output
    )


@app.command("bulk-invite")
def bulk_invite(
    request_file: Path = typer.Argument(
        ..., help="BulkInviteRequest JSON 文件，或 - 读取 stdin"
    ),
    show_secret: bool = typer.Option(False, "--show-secret", help="显示一次性邀请凭证"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过写入确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """执行批量邀请；逐项保存，允许部分成功。"""
    _bulk_invite_command(
        request_file,
        preview=False,
        show_secret=show_secret,
        yes=yes,
        json_output=json_output,
    )


def _bulk_group_command(
    request_file: Path, *, preview: bool, yes: bool, json_output: bool
) -> None:
    with cli_errors(json_output):
        request = read_json_body(request_file, BulkGroupAssignmentRequest)
        if not preview:
            action = (
                f"清除 {len(request.user_ids)} 个账号的数据组关联"
                if request.group_id is None
                else f"将 {len(request.user_ids)} 个账号设置到数据组 {request.group_id}"
            )
            confirm_destructive(f"确认{action}?", yes, json_output)
        with get_client(json_output) as client:
            result = (
                client.users.preview_bulk_group_assignment(request)
                if preview
                else client.users.bulk_group_assignment(request)
            )
    if json_output:
        print_json(result.model_dump(mode="json"))
    elif preview:
        console.print(f"可执行 {result.applicable} · 阻止 {result.blocked}")
        for item in result.items:
            mark = "OK" if item.ok else f"阻止: {item.error or '未知原因'}"
            console.print(f"{item.user_id}: {mark}")
    else:
        console.print(f"完成 · 成功 {result.succeeded} · 失败 {result.failed}")
        for item in result.items:
            status = "OK" if item.ok else f"失败: {item.error or '未知错误'}"
            console.print(f"{item.user_id}: {status}")
    if not preview and result.failed:
        raise typer.Exit(code=1)


@app.command("bulk-group-preview")
def bulk_group_preview(
    request_file: Path = typer.Argument(
        ..., help="BulkGroupAssignmentRequest JSON；必须提供 group_id，null 表示清除"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览账号批量分组。"""
    _bulk_group_command(request_file, preview=True, yes=True, json_output=json_output)


@app.command("bulk-group-apply")
def bulk_group_apply(
    request_file: Path = typer.Argument(
        ..., help="BulkGroupAssignmentRequest JSON；必须提供 group_id，null 表示清除"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过写入确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """执行账号批量分组；逐项提交，可能部分成功。"""
    _bulk_group_command(request_file, preview=False, yes=yes, json_output=json_output)


@app.command("preview-role")
def preview_role(
    user_id: str = typer.Argument(..., help="账号 ID"),
    role: AccountRole = typer.Option(..., "--role", help="目标平台角色"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """只读预览平台角色变更的阻塞项和影响。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.preview_role_change(user_id, role.value)
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    console.print(
        f"{result.email}: {result.current_role} → {result.requested_role} · "
        f"可变更={result.can_change}"
    )
    for blocker in result.blockers:
        console.print(f"阻塞: {blocker}")
    for warning in result.warnings:
        console.print(f"提示: {warning}")


@app.command("change-role")
def change_role(
    user_id: str = typer.Argument(..., help="账号 ID"),
    role: AccountRole = typer.Option(..., "--role", help="目标平台角色"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过写入确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """变更平台角色；建议先运行 preview-role，阻塞时 API 返回 409。"""
    confirm_destructive(
        f"确认将账号 {user_id} 的平台角色改为 {role.value}?", yes, json_output
    )
    request = ChangeUserRoleRequest(role=role.value)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.change_role(user_id, request.role)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]平台角色已变更[/green] {result.email} → {result.role}")


@app.command("assign-group")
def assign_group(
    user_id: str = typer.Argument(..., help="账号 ID"),
    group_id: str | None = typer.Option(None, "--group-id", help="目标数据组 ID"),
    clear: bool = typer.Option(False, "--clear", help="清除当前数据组"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过写入确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """设置或清除账号的数据组。"""
    if clear == (group_id is not None):
        raise typer.BadParameter("提供 --group-id 或 --clear 二者之一")
    action = (
        f"清除账号 {user_id} 的数据组关联"
        if clear
        else f"将账号 {user_id} 设置到数据组 {group_id}"
    )
    confirm_destructive(f"确认{action}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.assign_group(user_id, None if clear else group_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]数据组已更新[/green] {result.email}: {result.group_name or '-'}"
        )


@app.command("admin-reset-password")
def admin_reset_password(
    user_id: str = typer.Argument(..., help="账号 ID"),
    show_secret: bool = typer.Option(
        False, "--show-secret", help="允许在文本输出中显示一次性密码"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过密码重置确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重置低权限账号密码；先确认文本是否可安全接收一次性密码。"""
    if not json_output and not show_secret:
        raise typer.BadParameter("密码只返回一次；执行前请显式传 --show-secret")
    confirm_destructive(f"确认重置账号 {user_id} 的密码?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.admin_reset_password(user_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]临时密码已生成[/green] target={result.target_email}")
        console.print(f"temp_password={result.temp_password}")
        console.print(result.message)


@app.command("delete")
def delete(
    user_id: str = typer.Argument(..., help="账号 ID"),
    transfer_to_user_id: str | None = typer.Option(
        None, "--transfer-to-user-id", help="有待办任务/锁时的接收账号"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软删除账号；有待办任务或锁时须指定接收账号，否则 API 返回 409。"""
    confirm_destructive(f"确认软删除账号 {user_id}?", yes, json_output)
    request = (
        DeleteUserRequest(transfer_to_user_id=transfer_to_user_id)
        if transfer_to_user_id
        else None
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.delete(user_id, request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]账号已软删除[/green] {result.id} · status={result.status}"
        )


@app.command("deactivate")
def deactivate(
    user_id: str = typer.Argument(..., help="账号 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """立即停用账号、撤销 API keys；不会转交任务。"""
    confirm_destructive(f"确认立即停用账号 {user_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.deactivate(user_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]账号已停用[/green] {result.id} · status={result.status}")


@app.command("offboarding-preview")
def offboarding_preview(
    user_id: str = typer.Argument(..., help="账号 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取职责交接预览；执行时必须使用当前 preview_version。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.offboarding_preview(user_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    console.print(
        f"账号 {result.user.email} · 可提交={result.can_commit} · "
        f"preview_version={result.preview_version}"
    )
    for blocker in result.blockers:
        console.print(f"阻塞 [{blocker.code}]: {blocker.message}")
    for project in result.projects:
        console.print(
            f"{project.project_name}: locks={project.locked_task_count} · "
            f"blockers={len(project.blockers)}"
        )


@app.command("offboard")
def offboard(
    user_id: str = typer.Argument(..., help="账号 ID"),
    request_file: Path = typer.Argument(
        ..., help="OffboardingCommitRequest JSON；需含最新 preview_version"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按新鲜预览执行职责交接；409 时先重新预览，不自动刷新或重试。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, OffboardingCommitRequest)
        confirm_destructive(
            f"确认按 preview_version={request.preview_version} 执行 {request.mode} 并停用账号 {user_id}?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.users.offboard(user_id, request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]离职处理已完成[/green] {result.user.email} · "
            f"status={result.status} · mode={result.mode}"
        )
        console.print(
            f"交接 {len(result.transfers)} 项 · 未解决 {len(result.unresolved)} 项"
        )


@app.command("reactivate")
def reactivate(
    user_id: str = typer.Argument(..., help="账号 ID"),
    reason: str | None = typer.Option(None, "--reason", help="重新激活原因"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重新激活被 suspend / emergency_suspend 的账号；已删除账号不能恢复。"""
    confirm_destructive(f"确认重新激活账号 {user_id}?", yes, json_output)
    request = ReactivateRequest(reason=reason) if reason is not None else None
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.users.reactivate(user_id, request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]账号已重新激活[/green] {result.email} · status={result.status}"
        )
