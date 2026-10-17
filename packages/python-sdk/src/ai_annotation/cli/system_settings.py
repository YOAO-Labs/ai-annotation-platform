"""aap system-settings management, including explicitly confirmed SMTP tests."""

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.system_setting_models import (
    SystemSettingsReset,
    SystemSettingsUpdate,
)

app = typer.Typer(help="超级管理员系统配置与显式 SMTP 测试。", no_args_is_help=True)


@app.command("get")
def get(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.system_settings.get()
    print_learning_result(result, json_output)


@app.command("update")
def update(
    body_path: Path = typer.Option(
        ..., "--body", help="JSON 文件或 - 读取 stdin；密码放入正文"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        body = read_json_body(body_path, SystemSettingsUpdate)
        confirm_destructive("确认更新平台系统配置?", yes, json_output)
        with get_client(json_output) as client:
            result = client.system_settings.update(body)
    print_learning_result(result, json_output)


@app.command("reset")
def reset(
    body_path: Path = typer.Option(
        ..., "--body", help="包含 keys 和可选 expected_version 的 JSON；- 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        body = read_json_body(body_path, SystemSettingsReset)
        confirm_destructive(
            f"确认重置 {len(body.keys)} 个系统配置覆盖值?", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.system_settings.reset(body)
    print_learning_result(result, json_output)


@app.command("test-smtp")
def test_smtp(
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """向当前超级管理员邮箱发送真实测试邮件。"""
    with cli_errors(json_output):
        confirm_destructive("确认向当前管理员发送 SMTP 测试邮件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.system_settings.test_smtp()
    print_learning_result(result, json_output)
