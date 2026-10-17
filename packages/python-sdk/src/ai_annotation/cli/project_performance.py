"""CLI for project member performance and evidence."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    console,
    get_client,
    print_learning_result,
)
from ai_annotation.project_performance_models import ProjectPerformanceQuery

app = typer.Typer(
    help="项目成员绩效、证据分页与 CSV 导出。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


class WorkType(str, Enum):
    annotation = "annotation"
    review = "review"


class AccountStatus(str, Enum):
    all = "all"
    active = "active"
    inactive = "inactive"


def _query(
    *,
    from_date: str | None,
    to: str | None,
    timezone_name: str | None,
    work_type: WorkType,
    account_status: AccountStatus,
    include_historical: bool,
    q: str | None,
    sort: str | None,
    cursor: str | None,
    limit: int,
) -> ProjectPerformanceQuery:
    if q is not None and len(q) > 200:
        raise typer.BadParameter("--q 最多 200 个字符")
    return ProjectPerformanceQuery(
        from_=from_date,
        to=to,
        timezone=timezone_name,
        work_type=work_type.value,
        account_status=account_status.value,
        include_historical=include_historical,
        q=q,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )


@app.command("members")
def members(
    project_id: str = typer.Argument(..., help="项目 ID"),
    from_date: str | None = typer.Option(None, "--from", help="起始日期或 ISO 时间"),
    to: str | None = typer.Option(None, "--to", help="结束日期或 ISO 时间"),
    timezone_name: str | None = typer.Option(None, "--timezone", help="日期时区"),
    work_type: WorkType = typer.Option(WorkType.annotation, "--work-type"),
    account_status: AccountStatus = typer.Option(AccountStatus.all, "--account-status"),
    include_historical: bool = typer.Option(
        False, "--include-historical", help="包含已移出项目的历史成员"
    ),
    q: str | None = typer.Option(None, "--q", help="成员姓名或邮箱筛选"),
    sort: str | None = typer.Option(
        None, "--sort", help="排序表达式，例如 -submitted_tasks"
    ),
    cursor: str | None = typer.Option(None, "--cursor", help="上页返回的 cursor"),
    limit: int = typer.Option(50, min=1, max=100, help="页面条数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取项目成员绩效一页；超级管理员或项目负责人可访问。"""
    query = _query(
        from_date=from_date,
        to=to,
        timezone_name=timezone_name,
        work_type=work_type,
        account_status=account_status,
        include_historical=include_historical,
        q=q,
        sort=sort,
        cursor=cursor,
        limit=limit,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_performance.members(project_id, query)
    if json_output:
        print_learning_result(result, True)
        return
    table = Table()
    table.add_column("用户 ID")
    table.add_column("姓名")
    table.add_column("邮箱")
    table.add_column("项目角色")
    table.add_column("账号状态")
    table.add_column("提交任务")
    table.add_column("审核决策")
    for item in result.items:
        submitted = item.metrics.submitted_tasks.value
        decisions = item.metrics.review_decisions.value
        table.add_row(
            str(item.user_id),
            item.name,
            item.email,
            item.project_role or "-",
            item.account_status,
            "-" if submitted is None else str(submitted),
            "-" if decisions is None else str(decisions),
        )
    console.print(table)
    console.print(
        f"范围 {result.scope.from_.isoformat()} → {result.scope.to.isoformat()} "
        f"({result.scope.timezone}) · coverage={result.coverage.state}"
    )
    if result.next_cursor:
        console.print(f"next_cursor={result.next_cursor}")


@app.command("member")
def member(
    project_id: str = typer.Argument(..., help="项目 ID"),
    member_id: str = typer.Argument(..., help="用户 ID"),
    from_date: str | None = typer.Option(None, "--from", help="起始日期或 ISO 时间"),
    to: str | None = typer.Option(None, "--to", help="结束日期或 ISO 时间"),
    timezone_name: str | None = typer.Option(None, "--timezone", help="日期时区"),
    work_type: WorkType = typer.Option(WorkType.annotation, "--work-type"),
    account_status: AccountStatus = typer.Option(AccountStatus.all, "--account-status"),
    include_historical: bool = typer.Option(
        False, "--include-historical", help="包含已移出项目的历史成员"
    ),
    q: str | None = typer.Option(None, "--q", help="成员姓名或邮箱筛选"),
    cursor: str | None = typer.Option(None, "--cursor", help="证据页 cursor"),
    limit: int = typer.Option(50, min=1, max=100, help="证据页条数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取成员绩效详情；cursor 继续翻页的是 evidence。"""
    query = _query(
        from_date=from_date,
        to=to,
        timezone_name=timezone_name,
        work_type=work_type,
        account_status=account_status,
        include_historical=include_historical,
        q=q,
        sort=None,
        cursor=cursor,
        limit=limit,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_performance.member(project_id, member_id, query)
    print_learning_result(result, json_output)


@app.command("events")
def events(
    project_id: str = typer.Argument(..., help="项目 ID"),
    member_id: str = typer.Argument(..., help="用户 ID"),
    from_date: str | None = typer.Option(None, "--from", help="起始日期或 ISO 时间"),
    to: str | None = typer.Option(None, "--to", help="结束日期或 ISO 时间"),
    timezone_name: str | None = typer.Option(None, "--timezone", help="日期时区"),
    work_type: WorkType = typer.Option(WorkType.annotation, "--work-type"),
    account_status: AccountStatus = typer.Option(AccountStatus.all, "--account-status"),
    include_historical: bool = typer.Option(
        False, "--include-historical", help="包含已移出项目的历史成员"
    ),
    q: str | None = typer.Option(None, "--q", help="成员姓名或邮箱筛选"),
    cursor: str | None = typer.Option(None, "--cursor", help="上页返回的事件 cursor"),
    limit: int = typer.Option(50, min=1, max=100, help="页面条数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取成员绩效证据事件的一页。"""
    query = _query(
        from_date=from_date,
        to=to,
        timezone_name=timezone_name,
        work_type=work_type,
        account_status=account_status,
        include_historical=include_historical,
        q=q,
        sort=None,
        cursor=cursor,
        limit=limit,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_performance.events(project_id, member_id, query)
    print_learning_result(result, json_output)


@app.command("export")
def export(
    project_id: str = typer.Argument(..., help="项目 ID"),
    dest: Path = typer.Argument(..., help="目标 CSV 文件；父目录必须已存在"),
    from_date: str | None = typer.Option(None, "--from", help="起始日期或 ISO 时间"),
    to: str | None = typer.Option(None, "--to", help="结束日期或 ISO 时间"),
    timezone_name: str | None = typer.Option(None, "--timezone", help="日期时区"),
    work_type: WorkType = typer.Option(WorkType.annotation, "--work-type"),
    account_status: AccountStatus = typer.Option(AccountStatus.all, "--account-status"),
    include_historical: bool = typer.Option(
        False, "--include-historical", help="包含已移出项目的历史成员"
    ),
    q: str | None = typer.Option(None, "--q", help="成员姓名或邮箱筛选"),
    sort: str | None = typer.Option(None, "--sort", help="排序表达式"),
    json_output: bool = typer.Option(False, "--json", help="输出路径 JSON"),
) -> None:
    """导出全部匹配成员，不受页面 cursor / limit 影响。"""
    query = _query(
        from_date=from_date,
        to=to,
        timezone_name=timezone_name,
        work_type=work_type,
        account_status=account_status,
        include_historical=include_historical,
        q=q,
        sort=sort,
        cursor=None,
        limit=50,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.project_performance.export_members(project_id, dest, query)
    print_learning_result(result, json_output)
