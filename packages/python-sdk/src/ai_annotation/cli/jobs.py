"""aap jobs 子命令 + 共享的 wait_job 进度跟随。"""

from __future__ import annotations

import typer
from rich.table import Table
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from ai_annotation import Client
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    get_error_format,
    print_json,
    progress_console,
)
from ai_annotation.models import Job

app = typer.Typer(
    help="异步任务: 等待、软取消或重试失败项。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap jobs wait <job_id>[/] · [dim]aap jobs cancel <job_id>[/]",
)


def wait_job(client: Client, job_id: str, json_mode: bool = False) -> Job:
    """轮询 job 到终态; 非 --json 模式显示 rich 进度条并打印完成行。

    failed / cancelled 抛 JobFailedError, 由上层 cli_errors 统一转红色错误。
    """
    if json_mode:
        return client.jobs.wait(job_id)
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=progress_console,
        disable=get_error_format() == "json",
    ) as progress:
        task = progress.add_task(f"等待 job {job_id}", total=100)

        def _on_progress(job: Job) -> None:
            progress.update(
                task,
                completed=job.progress_pct,
                description=f"{job.kind} · {job.status}",
            )

        job = client.jobs.wait(job_id, on_progress=_on_progress)
        progress.update(task, completed=100)
    console.print(f"[green]job {job.id} completed[/green] (kind={job.kind})")
    return job


@app.command("list")
def list_(
    status: list[str] | None = typer.Option(None, "--status", help="任务状态，可重复"),
    kind: list[str] | None = typer.Option(None, "--kind", help="任务类型，可重复"),
    project_id: str | None = typer.Option(None, "--project", help="项目 ID"),
    search: str | None = typer.Option(None, "--search", help="任务名称或 ID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="起始偏移"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出异步任务。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.jobs.list(
                status=status,
                kind=kind,
                project_id=project_id,
                search=search,
                limit=limit,
                offset=offset,
            )
    if json_output:
        print_json(page.model_dump(mode="json"))
        return
    table = Table()
    table.add_column("ID")
    table.add_column("类型")
    table.add_column("状态")
    table.add_column("进度")
    table.add_column("项目")
    for job in page.items:
        table.add_row(
            str(job.id),
            job.kind,
            job.status,
            f"{job.progress_pct}%",
            str(job.project_id or "-"),
        )
    console.print(table)


@app.command("wait")
def wait(
    job_id: str = typer.Argument(..., help="async job ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """等待异步任务到终态, 跟随进度。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            job = wait_job(client, job_id, json_output)
    if json_output:
        print_json(job.model_dump(mode="json"))


@app.command("cancel")
def cancel(
    job_id: str = typer.Argument(..., help="async job ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """请求软取消一个异步任务 (协作式; 终态由后续轮询反映)。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.jobs.cancel(job_id)
    if json_output:
        print_json({"job_id": job_id, "cancel_requested": True})
    else:
        console.print(f"[green]已请求取消 job {job_id}[/green] (终态稍后由后端落定)")


@app.command("retry-failed")
def retry_failed(
    job_id: str = typer.Argument(..., help="async job ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重试 job 中已记录的可重试失败项。"""
    confirm_destructive(f"确认重试 job {job_id} 的失败项?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.jobs.retry_failed(job_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]已入队 {result.queued} 项[/green] skipped={result.skipped}"
        )
