"""aap storage diagnostics and video-asset maintenance."""

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
from ai_annotation.storage_models import VideoAssetRetryRequest

app = typer.Typer(help="存储诊断与管理员视频资产维护。", no_args_is_help=True)


@app.command("health")
def health(json_output: bool = typer.Option(False, "--json")) -> None:
    """检查标注 bucket 连通性。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage.health()
    print_learning_result(result, json_output)


@app.command("buckets")
def buckets(json_output: bool = typer.Option(False, "--json")) -> None:
    """统计配置的 bucket；对象数量大时可能耗时。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage.buckets()
    print_learning_result(result, json_output)


@app.command("video-asset-failures")
def video_asset_failures(
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出当前管理员可管理的视频资产失败记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.storage.video_asset_failures(limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("retry-video-asset")
def retry_video_asset(
    body_path: Path = typer.Option(..., "--body", help="JSON 文件；- 从 stdin 读取"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """重新排队生成视频元数据、chunk 或帧；返回 queued 不代表已修复。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoAssetRetryRequest)
        confirm_destructive(
            f"确认重试 {body.dataset_item_id} 的 {body.asset_type} 资产?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.storage.retry_video_asset(body)
    print_learning_result(result, json_output)
