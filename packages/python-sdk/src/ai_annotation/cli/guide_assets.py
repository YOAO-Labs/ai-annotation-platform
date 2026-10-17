"""CLI commands for project guide asset upload and retrieval."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.guide_asset_models import (
    GuideAssetUploadBytesRequest,
    GuideAssetUploadCompleteRequest,
    GuideAssetUploadInitRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(help="项目标注指引图片资源。", no_args_is_help=True)


@app.command("upload-init")
def upload_init(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 文件名、MIME 类型和大小，使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="输出含临时签名上传 URL 的裸 JSON"
    ),
) -> None:
    """签发临时上传 URL；文本输出不显示签名 URL。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, GuideAssetUploadInitRequest)
        with get_client(json_output) as client:
            result = client.guide_assets.upload_init(project_id, body)
    if json_output:
        print_learning_result(result, True)
    else:
        console.print(
            f"上传已准备: key={result.key}, expires_in={result.expires_in}s；"
            "使用 --json 获取临时上传 URL。"
        )


@app.command("upload-bytes")
def upload_bytes(
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="含签名 URL、文件路径和 MIME 类型的 UTF-8 JSON，使用 - 从 stdin 读取",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认写入对象存储"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """PUT 文件字节到签名 URL；之后需单独调用 upload-complete。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, GuideAssetUploadBytesRequest)
        confirm_destructive("确认上传标注指引图片字节?", yes, json_output)
        with get_client(json_output) as client:
            client.guide_assets.upload_bytes(
                body.upload_url, body.file_path, body.content_type
            )
    if json_output:
        print_json({"uploaded": True})
    else:
        console.print("字节已上传；请显式调用 upload-complete 关联资源。")


@app.command("upload-complete")
def upload_complete(
    project_id: str = typer.Argument(..., help="项目 ID"),
    body_path: Path = typer.Option(
        ...,
        "--body",
        help="含 key、原始文件名和 MIME 类型的 UTF-8 JSON，使用 - 从 stdin 读取",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认将资源添加到项目"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """验证已上传对象并将其添加到项目指引。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, GuideAssetUploadCompleteRequest)
        confirm_destructive("确认将图片资源添加到项目指引?", yes, json_output)
        with get_client(json_output) as client:
            result = client.guide_assets.upload_complete(project_id, body)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    project_id: str = typer.Argument(..., help="项目 ID"),
    key: str = typer.Option(..., "--key", help="guide-assets storage key"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除资源"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除项目指引资源及其存储对象。"""
    with cli_errors(json_output):
        confirm_destructive("确认删除此项目指引资源?", yes, json_output)
        with get_client(json_output) as client:
            result = client.guide_assets.delete(project_id, key)
    print_learning_result(result, json_output)


@app.command("sign-url")
def sign_url(
    project_id: str = typer.Argument(..., help="项目 ID"),
    key: str = typer.Option(..., "--key", help="guide-assets storage key"),
    json_output: bool = typer.Option(
        False, "--json", help="输出含临时签名下载 URL 的裸 JSON"
    ),
) -> None:
    """为当前账号签发临时下载 URL；文本输出不显示签名 URL。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.guide_assets.sign_url(project_id, key)
    if json_output:
        print_learning_result(result, True)
    else:
        console.print(
            f"下载 URL 已签发，有效期 {result.expires_in}s；使用 --json 获取 URL。"
        )
