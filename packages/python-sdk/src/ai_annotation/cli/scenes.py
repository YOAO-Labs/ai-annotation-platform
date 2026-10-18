"""aap scenes subcommands."""

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
from ai_annotation.scene_models import SceneUpdate

app = typer.Typer(
    help="数据集场景与有序帧轨迹。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap scenes list --dataset-id ... --json[/] · [dim]aap scenes trajectory ...[/]",
)


@app.command("list")
def list_(
    dataset_id: str = typer.Option(..., "--dataset-id", help="数据集 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 数组"),
) -> None:
    """列出数据集中的场景。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scenes.list(dataset_id)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    scene_id: str = typer.Argument(..., help="场景 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取场景元数据。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scenes.get(scene_id)
    print_learning_result(result, json_output)


@app.command("trajectory")
def trajectory(
    scene_id: str = typer.Argument(..., help="场景 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取按 frame_index 排序的逐帧 ego 位姿。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scenes.trajectory(scene_id)
    print_learning_result(result, json_output)


@app.command("update")
def update(
    scene_id: str = typer.Argument(..., help="场景 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="JSON PATCH 文件；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新共享场景元数据"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新场景名称、来源格式或来源元数据。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, SceneUpdate)
        if not any(
            value is not None for value in body.model_dump(exclude_unset=True).values()
        ):
            raise typer.BadParameter("至少提供一个非 null 更新字段")
        if idempotency_key is not None:
            try:
                idempotency_key = str(UUID(idempotency_key))
            except ValueError as exc:
                raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc
    confirm_destructive(f"确认更新场景 {scene_id} 的元数据?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scenes.update(
                scene_id, body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
