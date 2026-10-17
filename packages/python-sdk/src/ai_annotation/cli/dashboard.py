"""aap dashboard 子命令: 全员绩效 / 自助绩效 (只读)。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    console,
    confirm_destructive,
    get_client,
    print_json,
    sparkline,
)

app = typer.Typer(
    help="看板: 全员绩效 (people, 管理员) / 自助绩效 (me)。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap dashboard people[/] · [dim]aap dashboard me[/]",
)


def _fmt_rate(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.0f}%" if v <= 1 else f"{v:.0f}%"


@app.command("people")
def people(
    project: str | None = typer.Option(
        None, "--project", help="项目范围 (project_admin 必填)"
    ),
    role: str | None = typer.Option(
        None, "--role", help="按角色过滤: annotator / reviewer / both"
    ),
    period: str | None = typer.Option(
        None, "--period", help="统计周期: today / 7d / 4w / 1m"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """全员绩效卡片 (super_admin / project_admin)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            rows = client.dashboard.people(role=role, project=project, period=period)
    if json_output:
        print_json([p.model_dump(mode="json") for p in rows])
        return
    table = Table()
    table.add_column("姓名")
    table.add_column("角色")
    table.add_column("产出分")
    table.add_column("质量分")
    table.add_column("退回率")
    table.add_column("7日趋势")
    for p in rows:
        table.add_row(
            p.name,
            p.role,
            str(p.throughput_score),
            str(p.quality_score),
            _fmt_rate(p.rejected_rate),
            sparkline(p.sparkline_7d),
        )
    console.print(table)


@app.command("me")
def me_performance(
    period: str | None = typer.Option(None, "--period", help="统计周期 (默认 4w)"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """当前用户自助绩效 (自身趋势 + 团队均线对标)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            perf = client.dashboard.me_performance(period=period)
    if json_output:
        print_json(perf.model_dump(mode="json"))
        return
    console.print(
        f"[green]{perf.name}[/green] · 产出 {perf.throughput} · 质量 {perf.quality_score}"
    )
    console.print(f"产出趋势(4周): {sparkline(perf.trend_throughput)}")
    console.print(f"团队均线(4周): {sparkline(perf.team_trend_throughput)}")
    if perf.first_pass_yield is not None:
        console.print(f"一次通过率: {_fmt_rate(perf.first_pass_yield)}")


@app.command("reviewer-today-mini")
def reviewer_today_mini(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """当前审核员今日的通过、退回与平均审核耗时。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            stats = client.dashboard.reviewer_today_mini()
    data = stats.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"今日通过 {stats.approved_today} · 退回 {stats.rejected_today} · "
            f"平均耗时 {stats.avg_review_seconds if stats.avg_review_seconds is not None else '-'} 秒"
        )


@app.command("recent-reviews")
def recent_reviews(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="最多返回 100 条"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前审核员最近完成的审核。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            rows = client.dashboard.recent_reviews(limit=limit)
    data = [row.model_dump(mode="json") for row in rows]
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("任务")
    table.add_column("文件")
    table.add_column("项目")
    table.add_column("状态")
    table.add_column("审核时间")
    for row in rows:
        table.add_row(
            row.task_display_id,
            row.file_name,
            row.project_name,
            row.status,
            row.reviewed_at or "-",
        )
    console.print(table)


@app.command("annotator-onboarding")
def annotator_onboarding(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看当前用户在项目中的服务端任务、标注与审核进度。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.dashboard.annotator_onboarding(project_id)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"已分配 {result.assigned_task_count} · 已打开 {result.opened_task_count} · "
            f"已保存标注 {result.saved_annotation_count} · 已审核 {result.reviewed_task_count}"
        )


@app.command("annotator-batches")
def annotator_batches(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号可见的活动标注批次。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            rows = client.dashboard.annotator_batches()
    data = [row.model_dump(mode="json") for row in rows]
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("批次")
    table.add_column("项目")
    table.add_column("状态")
    table.add_column("标注进度")
    table.add_column("审核员")
    for row in rows:
        table.add_row(
            row.batch_display_id,
            row.project_name,
            row.status,
            f"{row.completed_tasks}/{row.total_tasks} ({row.progress_pct:g}%)",
            row.reviewer.name if row.reviewer else "-",
        )
    console.print(table)


@app.command("people-export")
def people_export(
    out: Path = typer.Option(..., "--out", help="CSV 输出路径"),
    project: str | None = typer.Option(None, "--project", help="项目范围"),
    role: str | None = typer.Option(None, "--role", help="角色过滤"),
    period: str = typer.Option("7d", "--period", help="统计周期"),
    sort: str = typer.Option(
        "throughput", "--sort", help="throughput/quality/activity/weekly_compare"
    ),
    q: str | None = typer.Option(None, "--query", help="姓名或邮箱搜索"),
    yes: bool = typer.Option(False, "--yes", "-y", help="允许覆盖现有文件"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """下载成员绩效 CSV；project_admin 必须指定自己负责的项目。"""
    if out.is_dir():
        raise typer.BadParameter("--out 必须指向文件")
    if not out.parent.exists():
        raise typer.BadParameter("--out 的父目录必须已存在")
    if out.exists():
        confirm_destructive(f"确认覆盖文件 {out}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            saved = client.dashboard.people_export(
                out, role=role, project=project, period=period, sort=sort, q=q
            )
    if json_output:
        print_json({"path": str(saved)})
    else:
        console.print(f"[green]已保存[/green] {saved}")


@app.command("person")
def person_detail(
    user_id: str = typer.Argument(..., help="用户 ID"),
    project: str | None = typer.Option(
        None, "--project", help="项目范围 (project_admin 必填)"
    ),
    period: str = typer.Option("4w", "--period", help="统计周期"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看管理范围内的成员绩效详情。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.dashboard.person_detail(
                user_id, project=project, period=period
            )
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"{result.name} · 产出 {result.throughput} · 质量 {result.quality_score} · "
            f"综合 {result.composite_score}"
        )
        console.print(f"产出趋势: {sparkline(result.trend_throughput)}")


@app.command("prediction-costs")
def prediction_cost_stats(
    range_: str = typer.Option("30d", "--range", help="7d 或 30d"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看全局预测成本与失败率 (super_admin)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            stats = client.dashboard.prediction_cost_stats(range=range_)
    data = stats.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"{stats.range} · 预测 {stats.total_predictions} · 失败 {stats.failed_predictions} · "
            f"失败率 {stats.failure_rate:.1%} · 总成本 {stats.total_cost:g}"
        )
