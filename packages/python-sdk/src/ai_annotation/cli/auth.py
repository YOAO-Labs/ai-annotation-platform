"""Explicit CLI commands for login, registration, invitation, and session flows."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from pydantic import BaseModel

from ai_annotation.auth_models import (
    AuthAcceptInvitationRequest,
    AuthForgotPasswordRequest,
    AuthLoginRequest,
    AuthOpenRegisterRequest,
    AuthRegisterRequest,
    AuthResetPasswordRequest,
    AuthSendVerificationEmailRequest,
    AuthVerifyEmailRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)
from ai_annotation.errors import AAPError

app = typer.Typer(help="账号注册、邀请、邮箱验证与会话操作。", no_args_is_help=True)


def _print_token_result(result: BaseModel, json_output: bool, message: str) -> None:
    if json_output:
        print_json(result.model_dump(mode="json", by_alias=True))
    else:
        console.print(message)


def _read_invitation_token(token_file: Path | None) -> str:
    if token_file is None:
        return typer.prompt("Invitation token", hide_input=True)
    try:
        token = (
            sys.stdin.read()
            if token_file == Path("-")
            else token_file.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError):
        raise AAPError("无法读取邀请 token：请检查文件路径和 UTF-8 编码") from None
    token = token.strip()
    if not token:
        raise AAPError("邀请 token 不能为空")
    return token


@app.command("forgot-password")
def forgot_password(
    body_path: Path = typer.Option(
        ..., "--body", help="含邮箱的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认发送重置邮件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求密码重置邮件。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthForgotPasswordRequest)
        confirm_destructive("确认请求发送密码重置邮件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.forgot_password(
                body.email, captcha_token=body.captcha_token
            )
    print_learning_result(result, json_output)


@app.command("accept-invitation")
def accept_invitation(
    body_path: Path = typer.Option(
        ..., "--body", help="含邀请 token 的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认接受邀请"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以当前认证账号接受项目邀请。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthAcceptInvitationRequest)
        confirm_destructive("确认接受此邀请并加入项目?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.accept_invitation(body.token)
    print_learning_result(result, json_output)


@app.command("login")
def login(
    body_path: Path = typer.Option(
        ..., "--body", help="含邮箱与密码的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON 模式会输出 access_token"
    ),
) -> None:
    """使用邮箱密码登录；不会保存返回的 JWT。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthLoginRequest)
        with get_client(json_output) as client:
            result = client.auth.login(
                body.email, body.password, captcha_token=body.captcha_token
            )
    _print_token_result(
        result, json_output, "登录成功；使用 --json 可输出 access_token。"
    )


@app.command("logout")
def logout(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认撤销当前 JWT 会话"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """撤销当前 JWT 会话。"""
    with cli_errors(json_output):
        confirm_destructive("确认撤销当前 JWT 会话?", yes, json_output)
        with get_client(json_output) as client:
            client.auth.logout()
    print_json({"revoked": True}) if json_output else console.print("当前会话已撤销。")


@app.command("logout-all")
def logout_all(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认撤销该账号的所有 JWT 会话"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON 模式会输出新的 access_token"
    ),
) -> None:
    """撤销该账号的所有 JWT 会话并返回新 JWT。"""
    with cli_errors(json_output):
        confirm_destructive("确认撤销该账号的所有 JWT 会话?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.logout_all()
    _print_token_result(
        result, json_output, "所有 JWT 会话已撤销；使用 --json 可输出新 access_token。"
    )


@app.command("refresh")
def refresh(
    json_output: bool = typer.Option(
        False, "--json", help="JSON 模式会输出新的 access_token"
    ),
) -> None:
    """用当前 JWT 换取新 token；不会更新已保存凭据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.auth.refresh()
    _print_token_result(
        result, json_output, "JWT 已刷新；使用 --json 可输出新的 access_token。"
    )


@app.command("register")
def register(
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="含邀请 token、姓名和密码的 UTF-8 JSON 文件，使用 - 从 stdin 读取",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建账号"),
    json_output: bool = typer.Option(
        False, "--json", help="JSON 模式会输出 access_token"
    ),
) -> None:
    """通过邀请创建账号。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthRegisterRequest)
        confirm_destructive("确认通过邀请创建账号?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.register(body.token, body.name, body.password)
    message = "账号已创建；使用 --json 可输出 access_token。"
    if result.email_verification_required:
        message = "账号已创建，请完成邮箱验证。"
    _print_token_result(result, json_output, message)


@app.command("register-open")
def register_open(
    body_path: Path = typer.Option(
        ..., "--body", help="含邮箱、姓名和密码的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建账号"),
    json_output: bool = typer.Option(
        False, "--json", help="JSON 模式会输出 access_token"
    ),
) -> None:
    """开放注册账号。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthOpenRegisterRequest)
        confirm_destructive("确认提交开放注册并创建账号?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.register_open(
                body.email,
                body.name,
                body.password,
                captcha_token=body.captcha_token,
            )
    message = "账号已创建；使用 --json 可输出 access_token。"
    if result.email_verification_required:
        message = "账号已创建，请完成邮箱验证。"
    _print_token_result(result, json_output, message)


@app.command("reset-password")
def reset_password(
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="含重置 token 和新密码的 UTF-8 JSON 文件，使用 - 从 stdin 读取",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认更改密码并撤销旧 JWT 会话"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """使用一次性重置 token 设置新密码。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthResetPasswordRequest)
        confirm_destructive("确认更改密码并撤销旧 JWT 会话?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.reset_password(body.token, body.new_password)
    print_learning_result(result, json_output)


@app.command("send-verification-email")
def send_verification_email(
    body_path: Path = typer.Option(
        ..., "--body", help="含邮箱的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认发送验证邮件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求重发验证邮件。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthSendVerificationEmailRequest)
        confirm_destructive("确认请求发送验证邮件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.send_verification_email(body.email)
    print_learning_result(result, json_output)


@app.command("verify-email")
def verify_email(
    body_path: Path = typer.Option(
        ..., "--body", help="含验证 token 的 UTF-8 JSON 文件，使用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认消费验证 token"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """消费一次性邮箱验证 token。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, AuthVerifyEmailRequest)
        confirm_destructive("确认消费此邮箱验证 token?", yes, json_output)
        with get_client(json_output) as client:
            result = client.auth.verify_email(body.token)
    print_learning_result(result, json_output)


@app.command("resolve-invitation")
def resolve_invitation(
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="邀请 token 文件；使用 - 从 stdin 读取；省略时隐藏提示",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看公开邀请详情。"""
    with cli_errors(json_output):
        token = _read_invitation_token(token_file)
        with get_client(json_output) as client:
            result = client.auth.resolve_invitation(token)
    print_learning_result(result, json_output)


@app.command("registration-status")
def registration_status(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看开放注册是否启用。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.auth.registration_status()
    print_learning_result(result, json_output)
