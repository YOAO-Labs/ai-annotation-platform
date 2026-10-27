"""CLI commands for project guide asset transfer."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
)

app = typer.Typer(help="项目标注指引图片资源。", no_args_is_help=True)


@app.command("upload")
def upload(
    project_id: str = typer.Argument(..., help="项目 ID"),
    file: Path = typer.Argument(..., help="待上传的图片文件"),
    file_name: str | None = typer.Option(
        None, "--name", help="资源文件名；默认使用本地文件名"
    ),
    content_type: str | None = typer.Option(
        None, "--content-type", help="MIME 类型；默认按文件名推断"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认将资源添加到项目"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """通过已认证请求上传图片并将其添加到项目指引。"""
    with cli_errors(json_output):
        confirm_destructive("确认将图片资源添加到项目指引?", yes, json_output)
        with get_client(json_output) as client:
            result = client.guide_assets.upload(
                project_id,
                file,
                file_name=file_name,
                content_type=content_type,
            )
    print_learning_result(result, json_output)


@app.command("content")
def content(
    project_id: str = typer.Argument(..., help="项目 ID"),
    key: str = typer.Option(..., "--key", help="guide-assets storage key"),
    output: Path = typer.Option(..., "--output", "-o", help="写入本地文件的路径"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载已认证的项目指引图片字节。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            data = client.guide_assets.content(project_id, key)
    output.write_bytes(data)
    if json_output:
        print_json({"key": key, "path": str(output), "bytes": len(data)})
    else:
        console.print(f"[green]已下载[/green] {key} → {output} ({len(data)} bytes)")


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
