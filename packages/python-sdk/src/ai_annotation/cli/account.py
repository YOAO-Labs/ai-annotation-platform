"""CLI for current-account settings, credentials and workbench events."""

from pathlib import Path

import typer
from pydantic import ValidationError

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)
from ai_annotation.errors import AAPError
from ai_annotation.account_models import (
    AvatarRefUpdate,
    DeactivationRequest,
    PasswordChange,
    ProfileUpdate,
    TaskEventBatchRequest,
    UserPreferencesPatch,
)

app = typer.Typer(
    help="当前账号的个人资料、偏好和自助操作。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
profile_app = typer.Typer(help="个人资料。", no_args_is_help=True)
preferences_app = typer.Typer(help="跨设备账户偏好。", no_args_is_help=True)
password_app = typer.Typer(help="账户密码。", no_args_is_help=True)
avatar_app = typer.Typer(help="账户头像。", no_args_is_help=True)
deactivation_app = typer.Typer(help="自助注销。", no_args_is_help=True)
events_app = typer.Typer(help="工作台任务事件。", no_args_is_help=True)

app.add_typer(profile_app, name="profile")
app.add_typer(preferences_app, name="preferences")
app.add_typer(password_app, name="password")
app.add_typer(avatar_app, name="avatar")
app.add_typer(deactivation_app, name="deactivation")
app.add_typer(events_app, name="events")


@profile_app.command("update")
def profile_update(
    name: str = typer.Option(..., "--name", help="显示名称，最多 100 字符"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新当前用户的显示名称。"""
    with cli_errors(json_output):
        try:
            body = ProfileUpdate(name=name)
        except ValidationError:
            raise AAPError("个人资料无效：name 不能为空且最多 100 字符") from None
        with get_client(json_output) as client:
            result = client.account.update_profile(body)
    print_learning_result(result, json_output)


@preferences_app.command("get")
def preferences_get(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前账户的偏好 JSON。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.account.get_preferences()
    print_learning_result(result, json_output)


@preferences_app.command("patch")
def preferences_patch(
    body_path: Path = typer.Option(
        ..., "--body", help="偏好 JSON 对象文件，- 表示 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交偏好部分更新；省略字段保留，null 与 revision 按原样传给服务端。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, UserPreferencesPatch)
        with get_client(json_output) as client:
            result = client.account.update_preferences(body.root)
    print_learning_result(result, json_output)


@password_app.command("change")
def password_change(
    body_path: Path | None = typer.Option(
        None, "--body", help="密码 JSON 文件；- 从 stdin 读取，适合自动化"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更改密码"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """安全地修改密码；交互输入隐藏，绝不接受密码命令行参数。"""
    with cli_errors(json_output):
        if body_path is not None:
            body = read_json_body(body_path, PasswordChange)
        else:
            if json_output:
                raise AAPError("--json 模式请通过 --body - 从 stdin 提供密码 JSON")
            old_password = typer.prompt("当前密码", hide_input=True)
            new_password = typer.prompt("新密码", hide_input=True)
            repeated = typer.prompt("再次输入新密码", hide_input=True)
            if new_password != repeated:
                raise AAPError("两次输入的新密码不一致")
            try:
                body = PasswordChange(
                    old_password=old_password, new_password=new_password
                )
            except ValidationError:
                raise AAPError("密码格式不符合服务端要求") from None
        confirm_destructive("确认修改当前账户密码?", yes, json_output)
        with get_client(json_output) as client:
            client.account.change_password(body)
    if json_output:
        print_json({"changed": True})
    else:
        typer.echo("密码已更新")


@avatar_app.command("upload")
def avatar_upload(
    file_path: Path = typer.Argument(..., help="PNG、JPEG 或 WebP 图片文件"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认替换当前上传头像"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传并规范化头像；替换后服务端会清理旧上传对象。"""
    with cli_errors(json_output):
        confirm_destructive("确认上传并替换当前头像?", yes, json_output)
        with get_client(json_output) as client:
            result = client.account.upload_avatar(file_path)
    print_learning_result(result, json_output)


@avatar_app.command("set")
def avatar_set(
    avatar_ref: str = typer.Argument(
        ..., help="preset:<slug>，不能指定 upload:<token>"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认替换当前头像"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """选择内置头像；不接受客户端指定上传 token。"""
    with cli_errors(json_output):
        try:
            body = AvatarRefUpdate(avatar_ref=avatar_ref)
        except ValidationError:
            raise AAPError("头像引用必须是 preset:<slug>") from None
        confirm_destructive("确认替换当前头像?", yes, json_output)
        with get_client(json_output) as client:
            result = client.account.set_avatar_ref(body)
    print_learning_result(result, json_output)


@avatar_app.command("clear")
def avatar_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认恢复默认头像"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """清除当前头像并恢复首字母图标。"""
    with cli_errors(json_output):
        confirm_destructive("确认清除当前头像?", yes, json_output)
        with get_client(json_output) as client:
            result = client.account.clear_avatar()
    print_learning_result(result, json_output)


@avatar_app.command("download")
def avatar_download(
    token: str = typer.Argument(
        ..., help="UserOut.avatar_ref 中 upload: 后的 32 位 token"
    ),
    dest: Path = typer.Argument(..., help="保存 WebP 图片的路径"),
    json_output: bool = typer.Option(False, "--json", help="输出路径 JSON"),
) -> None:
    """读取公开上传头像并原子写入目标文件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.avatars.download(token, dest)
    print_learning_result(result, json_output)


@deactivation_app.command("request")
def deactivation_request(
    reason: str | None = typer.Option(None, "--reason", help="申请原因，最多 500 字符"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认申请七天后的账号停用"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """申请自助注销；账号将在七天冷静期后停用。"""
    with cli_errors(json_output):
        try:
            body = DeactivationRequest(reason=reason)
        except ValidationError:
            raise AAPError("reason 不能超过 500 字符") from None
        confirm_destructive("确认申请七天后的账号停用?", yes, json_output)
        with get_client(json_output) as client:
            result = client.account.request_deactivation(body)
    print_learning_result(result, json_output)


@deactivation_app.command("cancel")
def deactivation_cancel(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """撤销尚在冷静期内的自助注销申请。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.account.cancel_deactivation()
    print_learning_result(result, json_output)


@app.command("heartbeat")
def heartbeat(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """刷新当前账户在线状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.account.heartbeat()
    if json_output:
        print_json({"ok": True})
    else:
        typer.echo("在线状态已刷新")


@events_app.command("submit")
def events_submit(
    body_path: Path = typer.Option(
        ..., "--body", help="事件批次 JSON 文件，- 表示 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交已结束的标注或审核区间；调用方应提供可重试的 client_id。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, TaskEventBatchRequest)
        with get_client(json_output) as client:
            result = client.account.submit_task_events(body)
    print_learning_result(result, json_output)
    if result.discarded:
        raise typer.Exit(code=1)
