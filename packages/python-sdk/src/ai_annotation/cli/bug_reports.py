"""Agent-friendly commands for reporter and manager bug-report workflows."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.bug_report_models import (
    BugCommentCreate,
    BugReportCreate,
    BugReportUpdate,
)
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(
    help="提交者和全局管理者可使用的问题反馈 API。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("list")
def list_(
    status: str | None = typer.Option(None, "--status", help="状态"),
    severity: str | None = typer.Option(None, "--severity", help="严重程度"),
    route: str | None = typer.Option(None, "--route", help="前端路由过滤"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="分页偏移"),
    markdown: bool = typer.Option(False, "--markdown", help="返回新问题 Markdown 摘要"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """全局管理员列表；Markdown 变体由 API 角色策略保护。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.bug_reports.list(
                status=status,
                severity=severity,
                route=route,
                limit=limit,
                offset=offset,
                format="markdown" if markdown else None,
            )
    print_learning_result(result, json_output)


@app.command("mine")
def mine(
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="每页条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="分页偏移"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前提交者自己的问题反馈。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.bug_reports.list_mine(limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("create")
def create(
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认提交问题反馈"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交问题反馈；project/task IDs 是诊断上下文，不是授权依据。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, BugReportCreate)
        confirm_destructive("确认提交问题反馈?", yes, json_output)
        with get_client(json_output) as client:
            result = client.bug_reports.create(request)
    print_learning_result(result, json_output)


@app.command("get")
def get(
    report_id: str = typer.Argument(..., help="问题反馈 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """获取自己提交的问题或管理者可见的问题及评论。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.bug_reports.get(report_id)
    print_learning_result(result, json_output)


@app.command("update")
def update(
    report_id: str = typer.Argument(..., help="问题反馈 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON PATCH 文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新问题反馈"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交者可改标题/描述/严重度；管理员还可改流转与分派字段。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, BugReportUpdate)
        if not request.model_fields_set:
            raise typer.BadParameter("PATCH 至少提供一个字段")
        confirm_destructive("确认更新问题反馈?", yes, json_output)
        with get_client(json_output) as client:
            result = client.bug_reports.update(report_id, request)
    print_learning_result(result, json_output)


@app.command("delete")
def delete(
    report_id: str = typer.Argument(..., help="问题反馈 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认永久删除"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """永久删除自己提交的问题或管理者可管理的问题及其评论。"""
    with cli_errors(json_output):
        confirm_destructive(f"确认永久删除问题反馈 {report_id}?", yes, json_output)
        with get_client(json_output) as client:
            client.bug_reports.delete(report_id)
    print_learning_result(None, json_output)


@app.command("comment")
def comment(
    report_id: str = typer.Argument(..., help="问题反馈 ID"),
    body: str = typer.Option(..., "--body", help="评论正文"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认添加评论"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """添加评论；提交者评论可能重新打开已关闭的问题。"""
    with cli_errors(json_output):
        request = BugCommentCreate(body=body)
        confirm_destructive("确认添加问题评论?", yes, json_output)
        with get_client(json_output) as client:
            result = client.bug_reports.add_comment(report_id, request)
    print_learning_result(result, json_output)


@app.command("cluster")
def cluster(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """计算新问题的相似项建议；只读计算，不修改问题。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.bug_reports.cluster()
    print_learning_result(result, json_output)


@app.command("upload-screenshot")
def upload_screenshot(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认上传截图"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """上传截图，返回报告 create 请求可用的 storage key 和附件元数据。"""
    with cli_errors(json_output):
        confirm_destructive("确认上传问题截图?", yes, json_output)
        with get_client(json_output) as client:
            result = client.bug_reports.upload_screenshot(path)
    print_learning_result(result, json_output)


@app.command("download-attachment")
def download_attachment(
    report_id: str = typer.Argument(..., help="问题反馈 ID"),
    key: str = typer.Option(..., "--key", help="附件 storage key"),
    output: Path = typer.Option(..., "--output", help="本地目标文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载附件；API 302 签名地址通过无认证流式连接读取。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.bug_reports.download_attachment(report_id, key, output)
    print_learning_result(result, json_output)
