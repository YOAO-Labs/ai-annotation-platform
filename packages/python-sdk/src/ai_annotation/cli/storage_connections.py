"""aap storage-connections subcommands."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.storage_connection_models import (
    ConnectorAllowlistUpdate,
    StorageConnectionCreate,
    StorageConnectionUpdate,
)

app = typer.Typer(
    help="管理员配置 S3/SFTP 存储连接器。密钥只在 create/update 请求中输入。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap storage-connections create --body - --yes --json[/]",
)
allowlist_app = typer.Typer(help="超级管理员配置连接目标白名单。", no_args_is_help=True)
app.add_typer(allowlist_app, name="allowlist")


@app.command("list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 数组"),
) -> None:
    """列出当前账号可见的全局与自有连接器。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.list()
    print_learning_result(result, json_output)


@app.command("get")
def get(
    connection_id: str = typer.Argument(..., help="连接器 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取连接器配置和 secret_set 状态；API 永不返回密钥。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.get(connection_id)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    body_path: Path = typer.Option(
        ..., "--body", help="JSON 请求文件；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建连接器"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出脱敏后的裸 JSON"),
) -> None:
    """创建 S3 或 SFTP 连接器。secret 只从请求体输入，不会从 API 响应回读。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, StorageConnectionCreate)
        if idempotency_key is not None:
            try:
                idempotency_key = str(UUID(idempotency_key))
            except ValueError as exc:
                raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(
        f"确认创建 {body.kind} 连接器 {body.name} ({body.scope})?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.create(
                body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)


@app.command("update")
def update(
    connection_id: str = typer.Argument(..., help="连接器 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="JSON PATCH 文件；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新或轮换密钥"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出脱敏后的裸 JSON"),
) -> None:
    """部分更新连接器；非 null secret 会整体轮换现有凭据。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, StorageConnectionUpdate)
        if not any(
            value is not None for value in body.model_dump(exclude_unset=True).values()
        ):
            raise typer.BadParameter("至少提供一个非 null 更新字段")
        if idempotency_key is not None:
            try:
                idempotency_key = str(UUID(idempotency_key))
            except ValueError as exc:
                raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(
        f"确认更新连接器 {connection_id} 并按需轮换密钥?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.update(
                connection_id, body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    connection_id: str = typer.Argument(..., help="连接器 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出回执 JSON"),
) -> None:
    """删除当前账号有权管理的连接器。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(f"确认删除连接器 {connection_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.storage_connections.delete(
                connection_id, idempotency_key=idempotency_key
            )
    print_learning_result(None, json_output)


@app.command("test")
def test_connection(
    connection_id: str = typer.Argument(..., help="连接器 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认发起外部连接测试"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出测试结果 JSON"),
) -> None:
    """通过 API 测试连接；HTTP 200 且 ok=false 表示连通性测试失败。"""
    if idempotency_key is not None:
        try:
            idempotency_key = str(UUID(idempotency_key))
        except ValueError as exc:
            raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(
        f"确认连接 {connection_id} 配置的外部存储主机并读取最多 20 条样本?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.test(
                connection_id, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)


@app.command("deployment-sftp-preset")
def deployment_sftp_preset(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取超级管理员可见的部署 SFTP 预设，不含密码或私钥。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.deployment_sftp_preset()
    print_learning_result(result, json_output)


@allowlist_app.command("get")
def get_allowlist(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前白名单及其数据库/环境来源。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.get_allowlist()
    print_learning_result(result, json_output)


@allowlist_app.command("update")
def update_allowlist(
    body_path: Path = typer.Option(
        ..., "--body", help="JSON 请求文件；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认替换目标白名单"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """替换白名单；单条最多 253 字符、最多 256 条。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ConnectorAllowlistUpdate)
    confirm_destructive(
        f"确认将存储连接器白名单替换为 {len(body.entries)} 条?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.update_allowlist(body)
    print_learning_result(result, json_output)


@allowlist_app.command("reset")
def reset_allowlist(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认恢复环境白名单"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """移除数据库覆盖并恢复环境配置的白名单。"""
    confirm_destructive("确认恢复环境中的存储连接器白名单?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage_connections.reset_allowlist()
    print_learning_result(result, json_output)
