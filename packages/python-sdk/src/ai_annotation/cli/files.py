"""aap files subcommands."""

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
from ai_annotation.file_models import FileUploadInit, FileUploadInitRequest

app = typer.Typer(
    help="文件上传与任务原件下载。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap files upload-init --project-id ... --file-name image.jpg --yes --json[/] · [dim]aap files download TASK_ID -o original.jpg[/]",
)


@app.command("upload-init")
def upload_init(
    project_id: str = typer.Option(..., "--project-id", help="项目 UUID"),
    file_name: str = typer.Option(..., "--file-name", help="文件名"),
    content_type: str | None = typer.Option(None, "--content-type", help="MIME 类型"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建 uploading 任务"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建上传占位任务并取得短时 PUT URL。"""
    with cli_errors(json_output):
        fields = {"content_type": content_type} if content_type is not None else {}
        body = FileUploadInitRequest(
            project_id=project_id, file_name=file_name, **fields
        )
    confirm_destructive(
        f"为项目 {project_id} 创建文件上传任务 {file_name}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.files.upload_init(body)
    print_learning_result(result, json_output)


@app.command("upload-complete")
def upload_complete(
    task_id: str = typer.Argument(..., help="上传中的任务 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认完成上传"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """确认对象存储上传完成并激活任务。"""
    confirm_destructive(f"确认完成上传任务 {task_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.files.upload_complete(task_id)
    print_learning_result(result, json_output)


@app.command("download")
def download(
    task_id: str = typer.Argument(..., help="任务 UUID"),
    output: Path = typer.Option(..., "--output", "-o", help="写入本地文件的路径"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """认证下载 SHA-256 校验后的任务原件。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            destination = client.files.download_original(task_id, output)
    print_learning_result({"task_id": task_id, "path": str(destination)}, json_output)


@app.command("upload-content")
def upload_content(
    init_path: Path = typer.Option(
        ..., "--init", help="upload-init 返回的 JSON 文件；- 从 stdin 读取"
    ),
    file_path: Path = typer.Option(
        ..., "--file", exists=True, dir_okay=False, help="上传的本地文件"
    ),
    content_type: str = typer.Option(
        ..., "--content-type", help="与 upload-init 相同的 MIME 类型"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """向短期签名 URL 流式上传字节；不会自动调用 upload-complete。"""
    with cli_errors(json_output):
        upload = read_json_body(init_path, FileUploadInit)
        confirm_destructive(f"确认上传文件到任务 {upload.task_id}?", yes, json_output)
        with get_client(json_output) as client:
            client.files.upload_content(upload, file_path, content_type=content_type)
    payload = {"uploaded": True, "task_id": str(upload.task_id)}
    print_learning_result(payload, json_output)


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise typer.BadParameter("--idempotency-key 必须是 UUID") from exc


@app.command("backfill-thumbnails")
def backfill_thumbnails(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认排队回填任务"),
    idempotency_key: str | None = typer.Option(
        None,
        "--idempotency-key",
        help="稳定 UUID；响应不确定时重试请复用原值",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受项目任务缩略图回填作业并返回可回放回执。"""
    idempotency_key = _normalize_idempotency_key(idempotency_key)
    confirm_destructive(f"确认回填项目 {project_id} 的缩略图?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.files.backfill_thumbnails(
                project_id, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
    if result.status in {"failed", "canceled"}:
        raise typer.Exit(1)
