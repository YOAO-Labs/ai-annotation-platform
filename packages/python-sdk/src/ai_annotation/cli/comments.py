"""Agent-friendly commands for annotation comments and task discussions."""

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
from ai_annotation.comment_models import (
    AnnotationCommentCreate,
    AnnotationCommentUpdate,
)

app = typer.Typer(
    help="读取和维护标注评论、附件与任务讨论流。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


class DiscussionScope(str, Enum):
    all = "all"
    task = "task"
    annotation = "annotation"


@app.command("list")
def list_(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """兼容性接口：一次返回该标注的完整评论数组。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.list(annotation_id)
    print_learning_result(
        [comment.model_dump(mode="json", by_alias=True) for comment in result],
        json_output,
    )


@app.command("page")
def page(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    cursor: str | None = typer.Option(None, "--cursor", help="上一页返回的游标"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取一个标注的评论 keyset 页面。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.page(annotation_id, limit=limit, cursor=cursor)
    print_learning_result(result, json_output)


@app.command("task-page")
def task_page(
    task_id: str = typer.Argument(..., help="任务 ID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    cursor: str | None = typer.Option(None, "--cursor", help="上一页返回的游标"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取任务下所有标注的评论 keyset 页面。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.task_page(task_id, limit=limit, cursor=cursor)
    print_learning_result(result, json_output)


@app.command("discussion")
def discussion(
    task_id: str = typer.Argument(..., help="任务 ID"),
    scope: DiscussionScope = typer.Option(DiscussionScope.all, "--scope"),
    annotation_id: str | None = typer.Option(None, "--annotation-id"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    cursor: str | None = typer.Option(None, "--cursor", help="上一页返回的游标"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取含反馈和评论的权威混合讨论页；使用 source + data.id 作为身份。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.discussion_page(
                task_id,
                scope=scope.value,
                annotation_id=annotation_id,
                limit=limit,
                cursor=cursor,
            )
    print_learning_result(result, json_output)


@app.command("annotation-counts")
def annotation_counts(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取任务可见标注的稀疏评论计数。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.annotation_counts(task_id)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认写入评论"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建评论；JSON 可包含 mentions、attachments、画布批注和锚点。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AnnotationCommentCreate)
        confirm_destructive("确认创建评论?", yes, json_output)
        with get_client(json_output) as client:
            result = client.comments.create(annotation_id, request)
    print_learning_result(result, json_output)


@app.command("update")
def update(
    comment_id: str = typer.Argument(..., help="评论 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON PATCH 文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认修改评论"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新评论正文或解决状态；正文清空受附件/画布内容约束。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AnnotationCommentUpdate)
        if not request.model_fields_set:
            raise typer.BadParameter("PATCH 至少提供 body 或 is_resolved")
        confirm_destructive("确认修改评论?", yes, json_output)
        with get_client(json_output) as client:
            result = client.comments.update(comment_id, request)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    comment_id: str = typer.Argument(..., help="评论 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认软删除评论"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软删除评论；可用 --json --yes 在自动化中执行。"""
    with cli_errors(json_output):
        confirm_destructive(f"确认软删除评论 {comment_id}?", yes, json_output)
        with get_client(json_output) as client:
            client.comments.delete(comment_id)
    print_learning_result(None, json_output)


@app.command("upload-attachment")
def upload_attachment(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认上传文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传附件并返回可直接放入评论请求的元数据。"""
    with cli_errors(json_output):
        confirm_destructive("确认上传评论附件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.comments.upload_attachment(annotation_id, path)
    print_learning_result(result, json_output)


@app.command("download-attachment")
def download_attachment(
    annotation_id: str = typer.Argument(..., help="标注 ID"),
    key: str = typer.Option(..., "--key", help="附件 storage key"),
    output: Path = typer.Option(..., "--output", help="本地目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载附件；302 签名地址通过无认证流式连接读取。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.comments.download_attachment(annotation_id, key, output)
    print_learning_result(result, json_output)
