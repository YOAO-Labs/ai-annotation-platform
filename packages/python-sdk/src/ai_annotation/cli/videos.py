"""Video asset and task-scoped video commands."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.video_models import (
    VideoChapterCreate,
    VideoChapterUpdate,
    VideoFramePrefetchRequest,
    VideoFrameRetryRequest,
)

app = typer.Typer(
    help="视频素材与章节: 查询视频资产、下载就绪的帧/chunk、管理章节。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

task_app = typer.Typer(
    help="任务视频: 查询任务媒体与管理视频分段租约。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


class VideoFrameFormat(str, Enum):
    webp = "webp"
    jpeg = "jpeg"


@app.command("manifest")
def video_manifest(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取视频 manifest 和短期有效的签名 URL。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.manifest(dataset_item_id)
    print_learning_result(result, json_output)


@app.command("segments")
def video_segments(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取分段；首次查询可能创建分段记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.segments(dataset_item_id)
    print_learning_result(result, json_output)


@app.command("frame-timetable")
def video_frame_timetable(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    from_frame: int | None = typer.Option(None, "--from-frame", min=0),
    to_frame: int | None = typer.Option(None, "--to-frame", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取视频帧时间表，可按源帧范围限制结果。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.frame_timetable(
                dataset_item_id, from_frame=from_frame, to_frame=to_frame
            )
    print_learning_result(result, json_output)


@app.command("chunks")
def video_chunks(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    from_frame: int | None = typer.Option(None, "--from-frame", min=0),
    to_frame: int | None = typer.Option(None, "--to-frame", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出指定帧范围的 chunk；缺失 chunk 会排队生成。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.chunks(
                dataset_item_id, from_frame=from_frame, to_frame=to_frame
            )
    print_learning_result(result, json_output)


@app.command("chunk")
def video_chunk(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    chunk_id: int = typer.Argument(..., min=0, help="chunk 序号"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取一个 chunk 状态；pending 会返回，不会自动轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.chunk(dataset_item_id, chunk_id)
    print_learning_result(result, json_output)


@app.command("chunk-samples")
def video_chunk_samples(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    chunk_id: int = typer.Argument(..., min=0, help="chunk 序号"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取一个 chunk 的编码样本表。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.chunk_samples(dataset_item_id, chunk_id)
    print_learning_result(result, json_output)


@app.command("frame")
def video_frame(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    frame_index: int = typer.Argument(..., min=0, help="零起始源帧序号"),
    format: VideoFrameFormat = typer.Option(VideoFrameFormat.webp, "--format"),
    width: int = typer.Option(512, "--width", min=1, max=4096),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取帧状态；pending 时返回 JSON 状态和 retry_after。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.frame(
                dataset_item_id,
                frame_index,
                format=format.value,
                width=width,
            )
    print_learning_result(result, json_output)


@app.command("prefetch-frames")
def prefetch_video_frames(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """排队提取请求中的帧；每次最多 500 帧。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoFramePrefetchRequest)
        with get_client(json_output) as client:
            result = client.videos.prefetch_frames(
                dataset_item_id,
                body.frame_indices,
                width=body.width,
                format=body.format,
            )
    print_learning_result(result, json_output)


@app.command("retry-frames")
def retry_video_frames(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认重新排队帧生成"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试失败帧；force 会清除所选帧缓存并重新生成。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoFrameRetryRequest)
        confirm_destructive("确认重新排队视频帧生成?", yes, json_output)
        with get_client(json_output) as client:
            result = client.videos.retry_frames(
                dataset_item_id,
                body.frame_indices,
                width=body.width,
                format=body.format,
                force=body.force,
            )
    print_learning_result(result, json_output)


@app.command("download-frame")
def download_video_frame(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    frame_index: int = typer.Argument(..., min=0, help="零起始源帧序号"),
    out: Path = typer.Option(..., "--out", help="本地文件路径"),
    format: VideoFrameFormat = typer.Option(VideoFrameFormat.webp, "--format"),
    width: int = typer.Option(512, "--width", min=1, max=4096),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """单次查询后下载就绪帧；pending 时退出，不轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            frame = client.videos.frame(
                dataset_item_id,
                frame_index,
                format=format.value,
                width=width,
            )
            result = client.videos.download_frame(frame, out)
    print_learning_result(result, json_output)


@app.command("download-chunk")
def download_video_chunk(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    chunk_id: int = typer.Argument(..., min=0, help="chunk 序号"),
    out: Path = typer.Option(..., "--out", help="本地文件路径"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """单次查询后下载就绪 chunk；pending 时退出，不轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            chunk = client.videos.chunk(dataset_item_id, chunk_id)
            result = client.videos.download_chunk(chunk, out)
    print_learning_result(result, json_output)


@app.command("chapters")
def video_chapters(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按帧范围读取视频章节。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.videos.chapters(dataset_item_id)
    print_learning_result(result, json_output)


@app.command("chapter-create")
def create_video_chapter(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建章节；写入要求项目管理权限。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoChapterCreate)
        with get_client(json_output) as client:
            result = client.videos.create_chapter(dataset_item_id, body)
    print_learning_result(result, json_output)


@app.command("chapter-update")
def update_video_chapter(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    chapter_id: str = typer.Argument(..., help="章节 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新章节的显式字段；写入要求项目管理权限。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoChapterUpdate)
        with get_client(json_output) as client:
            result = client.videos.update_chapter(dataset_item_id, chapter_id, body)
    print_learning_result(result, json_output)


@app.command("chapter-delete")
def delete_video_chapter(
    dataset_item_id: str = typer.Argument(..., help="视频 dataset item ID"),
    chapter_id: str = typer.Argument(..., help="章节 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除章节"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除章节；写入要求项目管理权限。"""
    with cli_errors(json_output):
        confirm_destructive("确认删除该视频章节?", yes, json_output)
        with get_client(json_output) as client:
            result = client.videos.delete_chapter(dataset_item_id, chapter_id)
    print_learning_result(result, json_output)


@task_app.command("manifest")
def task_video_manifest(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取旧版 task 视频 manifest。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.manifest(task_id)
    print_learning_result(result, json_output)


@task_app.command("manifest-v2")
def task_video_manifest_v2(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取精确帧链路 manifest。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.manifest_v2(task_id)
    print_learning_result(result, json_output)


@task_app.command("segments")
def task_video_segments(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取任务分段；首次查询可能创建分段记录。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.segments(task_id)
    print_learning_result(result, json_output)


@task_app.command("frame-timetable")
def task_video_frame_timetable(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    from_frame: int | None = typer.Option(None, "--from-frame", min=0),
    to_frame: int | None = typer.Option(None, "--to-frame", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取任务帧时间表，可按源帧范围限制结果。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.frame_timetable(
                task_id, from_frame=from_frame, to_frame=to_frame
            )
    print_learning_result(result, json_output)


@task_app.command("chunks")
def task_video_chunks(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    from_frame: int | None = typer.Option(None, "--from-frame", min=0),
    to_frame: int | None = typer.Option(None, "--to-frame", min=0),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出指定帧范围的 chunk；缺失 chunk 会排队生成。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.chunks(
                task_id, from_frame=from_frame, to_frame=to_frame
            )
    print_learning_result(result, json_output)


@task_app.command("chunk")
def task_video_chunk(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    chunk_id: int = typer.Argument(..., min=0, help="chunk 序号"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取一个 chunk 状态；pending 会返回，不会自动轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.chunk(task_id, chunk_id)
    print_learning_result(result, json_output)


@task_app.command("frame")
def task_video_frame(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    frame_index: int = typer.Argument(..., min=0, help="零起始源帧序号"),
    format: VideoFrameFormat = typer.Option(VideoFrameFormat.webp, "--format"),
    width: int = typer.Option(512, "--width", min=1, max=4096),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取帧状态；pending 时返回 JSON 状态和 retry_after。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.frame(
                task_id, frame_index, format=format.value, width=width
            )
    print_learning_result(result, json_output)


@task_app.command("prefetch-frames")
def prefetch_task_video_frames(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """排队提取请求中的帧；每次最多 500 帧。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoFramePrefetchRequest)
        with get_client(json_output) as client:
            result = client.task_videos.prefetch_frames(
                task_id, body.frame_indices, width=body.width, format=body.format
            )
    print_learning_result(result, json_output)


@task_app.command("retry-frames")
def retry_task_video_frames(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="请求 JSON 文件，或用 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认重新排队帧生成"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试失败帧；force 会清除所选帧缓存并重新生成。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, VideoFrameRetryRequest)
        confirm_destructive("确认重新排队视频帧生成?", yes, json_output)
        with get_client(json_output) as client:
            result = client.task_videos.retry_frames(
                task_id,
                body.frame_indices,
                width=body.width,
                format=body.format,
                force=body.force,
            )
    print_learning_result(result, json_output)


@task_app.command("download-frame")
def download_task_video_frame(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    frame_index: int = typer.Argument(..., min=0, help="零起始源帧序号"),
    out: Path = typer.Option(..., "--out", help="本地文件路径"),
    format: VideoFrameFormat = typer.Option(VideoFrameFormat.webp, "--format"),
    width: int = typer.Option(512, "--width", min=1, max=4096),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """单次查询后下载就绪帧；pending 时退出，不轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            frame = client.task_videos.frame(
                task_id, frame_index, format=format.value, width=width
            )
            result = client.task_videos.download_frame(frame, out)
    print_learning_result(result, json_output)


@task_app.command("download-chunk")
def download_task_video_chunk(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    chunk_id: int = typer.Argument(..., min=0, help="chunk 序号"),
    out: Path = typer.Option(..., "--out", help="本地文件路径"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """单次查询后下载就绪 chunk；pending 时退出，不轮询。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            chunk = client.task_videos.chunk(task_id, chunk_id)
            result = client.task_videos.download_chunk(chunk, out)
    print_learning_result(result, json_output)


@task_app.command("claim-segment")
def claim_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """领取并锁定分段。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.claim_segment(task_id, segment_id)
    print_learning_result(result, json_output)


@task_app.command("heartbeat-segment")
def heartbeat_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """延长当前用户的分段锁。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.heartbeat_segment(task_id, segment_id)
    print_learning_result(result, json_output)


@task_app.command("release-segment")
def release_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """释放当前用户的分段锁。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_videos.release_segment(task_id, segment_id)
    print_learning_result(result, json_output)


@task_app.command("submit-segment")
def submit_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认提交分段"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交分段；最后一个分段会令任务进入审核。"""
    with cli_errors(json_output):
        confirm_destructive(
            "确认提交视频分段?最后一个分段会提交整个任务进入审核。", yes, json_output
        )
        with get_client(json_output) as client:
            result = client.task_videos.submit_segment(task_id, segment_id)
    print_learning_result(result, json_output)


@task_app.command("reopen-segment")
def reopen_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认重开分段"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重开分段并清理任务审阅状态。"""
    with cli_errors(json_output):
        confirm_destructive("确认重开分段并清理任务审阅状态?", yes, json_output)
        with get_client(json_output) as client:
            result = client.task_videos.reopen_segment(task_id, segment_id)
    print_learning_result(result, json_output)


@task_app.command("unassign-segment")
def unassign_segment(
    task_id: str = typer.Argument(..., help="视频 task ID"),
    segment_id: str = typer.Argument(..., help="分段 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认取消分配"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """取消分段分配并释放锁。"""
    with cli_errors(json_output):
        confirm_destructive("确认取消视频分段分配并释放锁?", yes, json_output)
        with get_client(json_output) as client:
            result = client.task_videos.unassign_segment(task_id, segment_id)
    print_learning_result(result, json_output)
