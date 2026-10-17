"""Offline stream discovery and bounded NDJSON subscriptions."""

from __future__ import annotations

import asyncio
from uuid import UUID

import typer

from ai_annotation.streams import describe_stream_channel, list_stream_channels
from ai_annotation.cli._output import (
    cli_errors,
    get_client,
    print_json,
    print_learning_result,
)

app = typer.Typer(help="公开 WebSocket：离线发现与有界 NDJSON 监听")


@app.command("list")
def list_channels(json_output: bool = typer.Option(False, "--json")) -> None:
    """离线列出六条频道；不需要凭据或 streams extra。"""
    print_learning_result(list_stream_channels(), json_output)


@app.command("describe")
def describe(
    name: str = typer.Argument(...), json_output: bool = typer.Option(False, "--json")
) -> None:
    """离线读取事件 schema、鉴权与 REST 恢复说明。"""
    try:
        result = describe_stream_channel(name.replace("-", "_"))
    except ValueError:
        raise typer.BadParameter("未知频道；请先运行 streams list") from None
    print_learning_result(result, json_output)


async def _follow(context, max_events: int, timeout: float) -> None:
    try:
        async with asyncio.timeout(timeout):
            async with context as events:
                count = 0
                async for event in events:
                    print_json(event.model_dump(mode="json", by_alias=True))
                    count += 1
                    if count >= max_events:
                        return
    except TimeoutError:
        return


@app.command("notifications")
def notifications(
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """当前用户通知（JWT）；每行一个事件，窗口结束不表示目标工作已完成。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(_follow(client.streams.notifications(), max_events, timeout))


@app.command("preannotation")
def preannotation(
    project_id: UUID = typer.Argument(...),
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """项目预标进度；无隐式重连、等待完成或重新提交。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(
            _follow(client.streams.preannotation(project_id), max_events, timeout)
        )


@app.command("batch-events")
def batch_events(
    project_id: UUID = typer.Argument(...),
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """项目批次变化提示；收到事件后重新读取批次状态。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(
            _follow(client.streams.batch_events(project_id), max_events, timeout)
        )


@app.command("prediction-jobs")
def prediction_jobs(
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """全局预测作业提示（管理员 JWT）。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(_follow(client.streams.prediction_jobs(), max_events, timeout))


@app.command("video-tracker")
def video_tracker(
    job_id: UUID = typer.Argument(...),
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """Tracker 事件（JWT）；帧结果是瞬态候选，不表示已写入标注。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(_follow(client.streams.video_tracker(job_id), max_events, timeout))


@app.command("ml-stats")
def ml_stats(
    max_events: int = typer.Option(20, "--max-events", min=1, max=1000),
    timeout: float = typer.Option(30, "--timeout", min=0.1, max=3600),
) -> None:
    """ML 统计（管理员）；订阅会启用服务端既有的统计轮询。"""
    with cli_errors(True), get_client(True) as client:
        asyncio.run(_follow(client.streams.ml_stats(), max_events, timeout))
