"""aap scene-tracks subcommands for timeline, diagnostics, and track operations."""

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
from ai_annotation.scene_track_models import (
    LegacyTrackOperationExecuteRequest,
    LegacyTrackOperationRequest,
    SceneTrackCommandExecuteRequest,
    SceneTrackCommandRequest,
    SceneTrackRevertRequest,
)

app = typer.Typer(
    help="3D scene timelines, scene-wide track diagnostics, and versioned track commands.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "Examples: [dim]aap scene-tracks timeline T-1 --start-frame 0 --end-frame 99 --json[/] · "
        "[dim]aap scene-tracks command-preview T-1 --body - --json[/]"
    ),
)


def _validate_track_id(track_id: str) -> None:
    if not 1 <= len(track_id) <= 64:
        raise typer.BadParameter("track_id must contain 1 to 64 characters")


@app.command("timeline")
def scene_timeline(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    start_frame: int = typer.Option(..., "--start-frame", min=0),
    end_frame: int = typer.Option(..., "--end-frame", min=0),
    track_id: str | None = typer.Option(None, "--track-id"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取最多 200 帧的包含端点窗口；unavailable 帧会显式保留。"""
    if end_frame < start_frame:
        raise typer.BadParameter("--end-frame 必须大于等于 --start-frame")
    if end_frame - start_frame + 1 > 200:
        raise typer.BadParameter("时间轴窗口不能超过 200 帧")
    if track_id is not None:
        _validate_track_id(track_id)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.scene_timeline(
                task_id,
                start_frame=start_frame,
                end_frame=end_frame,
                track_id=track_id,
            )
    print_learning_result(result, json_output)


@app.command("track")
def get_track(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    track_id: str = typer.Argument(..., help="Scene Track ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取轨迹详情；服务端要求轨迹所有成员任务可见。"""
    _validate_track_id(track_id)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.get_track(task_id, track_id)
    print_learning_result(result, json_output)


@app.command("diagnostics")
def diagnostics(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    limit: int = typer.Option(200, "--limit", min=1, max=1000),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取完整 Scene 诊断；所有 Scene task 必须对当前用户可见。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.diagnostics(task_id, limit=limit)
    print_learning_result(result, json_output)


@app.command("command-operations")
def command_operations(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    track_id: str = typer.Argument(..., help="Scene Track ID"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前用户可见的命令历史；track_id 必须传入。"""
    _validate_track_id(track_id)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.list_command_operations(
                task_id, track_id, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("candidates")
def track_operation_candidates(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    track_id: str = typer.Argument(..., help="主轨迹 ID"),
    limit: int = typer.Option(20, "--limit", min=1, max=20),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出可合并轨迹；该读取接口要求任务写权限。"""
    _validate_track_id(track_id)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.list_track_operation_candidates(
                task_id, track_id, limit=limit
            )
    print_learning_result(result, json_output)


@app.command("command-preview")
def preview_command(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    body_path: Path = typer.Option(..., "--body", help="命令 JSON；用 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json", help="输出预览 JSON"),
) -> None:
    """预览 split/merge/mark_absent/resume/terminate，不写入轨迹。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, SceneTrackCommandRequest)
        with get_client(json_output) as client:
            result = client.scene_tracks.preview_command(task_id, body)
    print_learning_result(result, json_output)


@app.command("command-execute")
def execute_command(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="原预览请求 + snapshot_token + idempotency_key JSON；用 - 从 stdin 读取",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行此精确预览命令"),
    json_output: bool = typer.Option(False, "--json", help="输出完整操作结果"),
) -> None:
    """执行 preferred command；必须原样携带 preview snapshot 和调用方幂等键。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, SceneTrackCommandExecuteRequest)
    confirm_destructive("确认执行该 Scene Track 命令及其影响成员?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.execute_command(task_id, body)
    print_learning_result(result, json_output)


@app.command("command-revert")
def revert_command(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    operation_id: str = typer.Argument(..., help="原始已提交 operation ID"),
    idempotency_key: str = typer.Option(
        ..., "--idempotency-key", help="8-128 字符，调用方需自行稳定复用"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认回滚原始操作"),
    json_output: bool = typer.Option(False, "--json", help="输出完整 revert 结果"),
) -> None:
    """撤销指定原始 operation；revert 结果本身不能再次撤销。"""
    with cli_errors(json_output):
        body = SceneTrackRevertRequest(idempotency_key=idempotency_key)
    confirm_destructive(f"确认撤销 operation {operation_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.revert_command(task_id, operation_id, body)
    print_learning_result(result, json_output)


@app.command("track-operation-preview")
def preview_track_operation(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="兼容 split/merge JSON；用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出预览 JSON"),
) -> None:
    """使用兼容 split/merge API 预览；与 scene-track-command 是分开的合同。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, LegacyTrackOperationRequest)
        with get_client(json_output) as client:
            result = client.scene_tracks.preview_track_operation(task_id, body)
    print_learning_result(result, json_output)


@app.command("track-operation-execute")
def execute_track_operation(
    task_id: str = typer.Argument(..., help="锚点任务 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="兼容请求 + snapshot_token JSON；用 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行兼容 split/merge"),
    json_output: bool = typer.Option(False, "--json", help="输出操作结果"),
) -> None:
    """兼容旧 split/merge 执行；服务端生成幂等键，超时后不得盲目重放。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, LegacyTrackOperationExecuteRequest)
    confirm_destructive(
        "确认执行兼容 Scene Track split/merge? 超时后先检查 Scene 状态，不要盲目重放。",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.scene_tracks.execute_track_operation(task_id, body)
    print_learning_result(result, json_output)
