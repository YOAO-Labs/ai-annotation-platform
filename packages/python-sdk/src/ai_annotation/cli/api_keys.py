"""Self-service API keys with explicit one-time plaintext output."""

from pathlib import Path

import typer

from ai_annotation.api_key_models import ApiKeyCreateRequest, ApiKeyUpdateRequest
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(
    help="当前账号 API key；创建／轮换仅返回一次明文。", no_args_is_help=True
)


def _require_secret_output(json_output: bool, show_secret: bool) -> None:
    if not json_output and not show_secret:
        raise typer.BadParameter(
            "明文只返回一次，请指定 --json 或 --show-secret 后再提交"
        )


@app.command("list")
def list_(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.api_keys.list()
    print_learning_result(result, json_output)


@app.command("create")
def create(
    body_path: Path = typer.Option(..., "--body", help="JSON 文件或 - 读取 stdin"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    show_secret: bool = typer.Option(False, "--show-secret"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        body = read_json_body(body_path, ApiKeyCreateRequest)
        _require_secret_output(json_output, show_secret)
        confirm_destructive("确认创建当前账号的 API key?", yes, json_output)
        with get_client(json_output) as client:
            result = client.api_keys.create(
                body.name, scopes=body.scopes, expires_in_days=body.expires_in_days
            )
    print_learning_result(result, json_output)


@app.command("update")
def update(
    key_id: str = typer.Argument(...),
    body_path: Path = typer.Option(
        ..., "--body", help="JSON 补丁；expires_in_days=null 清除有效期"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        body = read_json_body(body_path, ApiKeyUpdateRequest)
        confirm_destructive("确认修改 API key 名称、权限或有效期?", yes, json_output)
        with get_client(json_output) as client:
            result = client.api_keys.patch(key_id, body)
    print_learning_result(result, json_output)


@app.command("rotate")
def rotate(
    key_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    show_secret: bool = typer.Option(False, "--show-secret"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        _require_secret_output(json_output, show_secret)
        confirm_destructive("确认轮换 API key 并立即使旧明文失效?", yes, json_output)
        with get_client(json_output) as client:
            result = client.api_keys.rotate(key_id)
    print_learning_result(result, json_output)


@app.command("revoke")
def revoke(
    key_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        confirm_destructive("确认吊销 API key?", yes, json_output)
        with get_client(json_output) as client:
            client.api_keys.revoke(key_id)
    print_learning_result({"revoked": True}, json_output)
