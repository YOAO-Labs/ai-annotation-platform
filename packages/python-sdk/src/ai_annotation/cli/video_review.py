"""CLI commands for video keyframe edits and track-quality review."""

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.video_review_models import (
    VideoMaskContentUploadRequest,
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoTrackCompositionRequest,
    VideoTrackConvertToBboxesRequest,
    VideoTrackQualityAcceptRequest,
    VideoTrackQualityRunRequest,
)

app = typer.Typer(
    help="视频标注审核与编辑。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
quality_app = typer.Typer(
    help="视频轨迹质量审核。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(quality_app, name="quality")


@app.command("mask-save")
def mask_save(
    task_id: str = typer.Argument(..., help="任务 ID"),
    annotation_id: str = typer.Argument(..., help="轨迹标注 ID"),
    frame_index: int = typer.Argument(..., min=0, help="视频帧索引"),
    expected_version: int = typer.Option(
        ..., "--expected-version", min=1, help="GET annotation 返回的 version"
    ),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """保存一个 mask keyframe，并用 annotation version 防止覆盖新改动。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoMaskKeyframeSaveRequest)
        with get_client(json_output) as client:
            result = client.video_review.save_mask_keyframe(
                task_id, annotation_id, frame_index, expected_version, body
            )
    print_learning_result(result, json_output)


@app.command("mask-upload")
def mask_upload(
    task_id: str = typer.Argument(..., help="任务 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="COCO RLE JSON 文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", help="确认创建 task-owned mask object"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传 COCO RLE 内容并创建 task-owned mask reference。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoMaskContentUploadRequest)
        confirm_destructive("确认上传并创建 mask content object?", yes, json_output)
        with get_client(json_output) as client:
            result = client.video_review.upload_mask_content(task_id, body)
    print_learning_result(result, json_output)


@app.command("mask-get")
def mask_get(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    frame_index: int | None = typer.Option(
        None, "--frame-index", min=0, help="读取视频标注在指定帧的 mask"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以 JSON COCO RLE 读取 mask；可选按视频帧解析轨迹。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            if frame_index is None:
                result = client.video_review.get_annotation_mask_content(annotation_id)
            else:
                result = client.video_review.get_annotation_frame_mask_content(
                    annotation_id, frame_index
                )
    print_learning_result(result, json_output)


@app.command("prediction-mask-get")
def prediction_mask_get(
    task_id: str = typer.Argument(..., help="任务 ID"),
    prediction_id: str = typer.Argument(..., help="预测 ID"),
    shape_index: int = typer.Argument(..., min=0, help="预测结果中的 shape 索引"),
    frame_index: int = typer.Argument(..., min=0, help="视频帧索引"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """以 JSON COCO RLE 读取预测轨迹在指定帧的 mask。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_review.get_prediction_mask_content(
                task_id, prediction_id, shape_index, frame_index
            )
    print_learning_result(result, json_output)


@app.command("mask-operate")
def mask_operate(
    task_id: str = typer.Argument(..., help="任务 ID"),
    annotation_id: str = typer.Argument(..., help="轨迹标注 ID"),
    frame_index: int = typer.Argument(..., min=0, help="视频帧索引"),
    expected_version: int = typer.Option(
        ..., "--expected-version", min=1, help="GET annotation 返回的 version"
    ),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除 keyframe、标记帧外区域或恢复 held mask。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoMaskKeyframeOperationRequest)
        with get_client(json_output) as client:
            result = client.video_review.operate_mask_keyframe(
                task_id, annotation_id, frame_index, expected_version, body
            )
    print_learning_result(result, json_output)


@quality_app.command("list")
def quality_list(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出任务的视频轨迹质量运行。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_review.list_track_quality_runs(task_id)
    print_learning_result(result, json_output)


@quality_app.command("run")
def quality_run(
    task_id: str = typer.Argument(..., help="任务 ID"),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """对相邻视频片段启动轨迹质量分析。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoTrackQualityRunRequest)
        with get_client(json_output) as client:
            result = client.video_review.run_track_quality(task_id, body)
    print_learning_result(result, json_output)


@quality_app.command("get")
def quality_get(
    task_id: str = typer.Argument(..., help="任务 ID"),
    run_id: str = typer.Argument(..., help="质量运行 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取质量运行、当前新鲜度、指标与候选配对。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.video_review.get_track_quality_run(task_id, run_id)
    print_learning_result(result, json_output)


@quality_app.command("accept")
def quality_accept(
    task_id: str = typer.Argument(..., help="任务 ID"),
    run_id: str = typer.Argument(..., help="质量运行 ID"),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按质量运行返回的 input_digest 提交完整配对决策。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoTrackQualityAcceptRequest)
        with get_client(json_output) as client:
            result = client.video_review.accept_track_quality_run(task_id, run_id, body)
    print_learning_result(result, json_output)


@app.command("compose")
def compose(
    task_id: str = typer.Argument(..., help="任务 ID"),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    yes: bool = typer.Option(False, "--yes", help="确认删除 composition 的源标注"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """组合视频标注；delete_sources=true 会停用源标注。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoTrackCompositionRequest)
        if body.delete_sources:
            confirm_destructive("确认停用参与组合的源标注?", yes, json_output)
        with get_client(json_output) as client:
            result = client.video_review.compose_tracks(task_id, body)
    print_learning_result(result, json_output)


@app.command("convert-to-bboxes")
def convert_to_bboxes(
    task_id: str = typer.Argument(..., help="任务 ID"),
    annotation_id: str = typer.Argument(..., help="轨迹标注 ID"),
    body_path: Path = typer.Option(..., "--body", help="请求 JSON 文件，- 表示 stdin"),
    yes: bool = typer.Option(False, "--yes", help="确认 split 可能停用源轨迹"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """从视频 bbox 轨迹生成逐帧框；split 可能停用源轨迹。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoTrackConvertToBboxesRequest)
        if body.operation == "split":
            confirm_destructive("确认拆分并移除对应源轨迹内容?", yes, json_output)
        with get_client(json_output) as client:
            result = client.video_review.convert_track_to_bboxes(
                task_id, annotation_id, body
            )
    print_learning_result(result, json_output)
