"""aap export 子命令: create → (wait → download) 全流程, 多格式 + 选项对齐 web。"""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    console,
    get_client,
    print_json,
    read_json_body,
)
from ai_annotation.cli.jobs import wait_job
from ai_annotation.export_models import ExportRequestBody

app = typer.Typer(
    help="标注导出: 创建导出 job → 等待完成 → 下载到本地。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap export project P-1 --target coco --target yolo-det --out ./out.zip[/]",
)


@app.command("project")
def project(
    project_id: str = typer.Argument(..., help="项目 ID"),
    target: list[str] = typer.Option(
        ...,
        "--target",
        help="导出格式, 可重复: coco / yolo-det / aap_json / video_json / kitti …",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="导出包输出路径 (--wait 时必填; --no-wait 时忽略)"
    ),
    body_path: Path | None = typer.Option(
        None, "--body", help="视频 scope 或 lidar 选项的 UTF-8 JSON 文件"
    ),
    include_attributes: bool = typer.Option(
        True,
        "--include-attributes/--no-include-attributes",
        help="是否携带 annotation.attributes 与 attribute_schema (默认含)",
    ),
    video_frame_mode: str | None = typer.Option(
        None, "--video-frame-mode", help="video 项目帧模式: keyframes | all_frames"
    ),
    axis_frame: str | None = typer.Option(
        None, "--axis-frame", help="lidar 3D box 坐标系: iso | source"
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="等待 job 完成并下载; --no-wait 仅创建返回 job_id",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """导出项目标注: 创建导出 job →（默认）等待完成 → 下载到 --out。"""
    extra: dict[str, str] = {}
    if video_frame_mode is not None:
        extra["video_frame_mode"] = video_frame_mode
    if axis_frame is not None:
        extra["axis_frame"] = axis_frame
    with cli_errors(json_output):
        body = read_json_body(body_path, ExportRequestBody) if body_path else None
        if "voc" in target and target != ["voc"]:
            raise typer.BadParameter("voc must be the only --target")
        if target == ["voc"]:
            if not wait:
                raise typer.BadParameter(
                    "VOC export is synchronous and does not support --no-wait"
                )
            if out is None:
                raise typer.BadParameter("VOC export requires --out")
            if body is not None:
                raise typer.BadParameter("--body applies to asynchronous exports only")
            with get_client(json_output) as client:
                dest = client.exports.download_voc(
                    project_id, out, include_attributes=include_attributes
                )
            if json_output:
                print_json({"out": str(dest)})
            else:
                console.print(f"[green]VOC 导出完成[/green] → {dest}")
            return
        if wait and out is None:
            raise typer.BadParameter("--wait 模式需要 --out 指定下载路径")
        with get_client(json_output) as client:
            job_id = client.exports.create(
                project_id,
                targets=target,
                include_attributes=include_attributes,
                body=body,
                **extra,
            )
            if not wait:
                if json_output:
                    print_json({"job_id": str(job_id), "waited": False})
                else:
                    console.print(
                        f"导出 job 已创建: {job_id} (--no-wait, 用 [dim]aap jobs wait {job_id}[/] 跟进)"
                    )
                return
            if not json_output:
                console.print(f"导出 job 已创建: {job_id}")
            job = wait_job(client, job_id, json_output)
            dest = client.exports.download(job, out)
    if json_output:
        print_json({"job_id": str(job_id), "status": job.status, "out": str(dest)})
    else:
        console.print(f"[green]导出完成[/green] → {dest}")
