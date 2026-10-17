"""aap tasks 工作流命令。"""

from enum import Enum

import typer
from rich.table import Table

from ai_annotation.cli._output import cli_errors, console, get_client, print_json

app = typer.Typer(
    help="任务流转: 提交、跳过、撤回、重开与审核。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


class SkipReason(str, Enum):
    image_corrupt = "image_corrupt"
    no_target = "no_target"
    unclear = "unclear"
    other = "other"


class RejectReason(str, Enum):
    missing = "missing"
    extra = "extra"
    wrong_label = "wrong_label"
    wrong_geometry = "wrong_geometry"


def _print_result(result, json_output: bool) -> None:
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]{result.status}[/green] task={result.task_id}")


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    status: str | None = typer.Option(None, "--status", help="任务状态"),
    assignee_id: str | None = typer.Option(None, "--assignee-id", help="标注人用户 ID"),
    batch_id: str | None = typer.Option(None, "--batch-id", help="批次 ID"),
    unbatched: bool | None = typer.Option(
        None, "--unbatched/--all-batches", help="仅未分批 / 包含全部批次"
    ),
    reject_reason_type: str | None = typer.Option(
        None, "--reject-reason-type", help="退回原因类型"
    ),
    class_name: str | None = typer.Option(None, "--class-name", help="类别名称"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="起始偏移"),
    cursor: str | None = typer.Option(None, "--cursor", help="游标"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按项目与可选条件列出任务。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.tasks.list(
                project_id,
                status=status,
                limit=limit,
                offset=offset,
                cursor=cursor,
                assignee_id=assignee_id,
                batch_id=batch_id,
                unbatched=unbatched,
                reject_reason_type=reject_reason_type,
                class_name=class_name,
            )
    if json_output:
        print_json(page.model_dump(mode="json"))
        return
    table = Table()
    table.add_column("ID")
    table.add_column("状态")
    table.add_column("文件名")
    table.add_column("标注人")
    table.add_column("批次")
    for task in page.items:
        table.add_row(
            task.display_id,
            task.status,
            task.file_name,
            str(task.assignee_id or "-"),
            str(task.batch_id or "-"),
        )
    console.print(table)


@app.command("submit")
def submit(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """提交任务进入审核。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.tasks.submit(task_id)
    _print_result(result, json_output)


@app.command("skip")
def skip(
    task_id: str = typer.Argument(..., help="任务 ID"),
    reason: SkipReason = typer.Option(..., "--reason", help="跳过原因"),
    note: str | None = typer.Option(None, "--note", help="补充说明"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """跳过任务并转审核。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.tasks.skip(task_id, reason.value, note)
    _print_result(result, json_output)


def _simple_action(task_id: str, action: str, json_output: bool) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = getattr(client.tasks, action)(task_id)
    _print_result(result, json_output)


@app.command("withdraw")
def withdraw(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """撤回未被认领的审核任务。"""
    _simple_action(task_id, "withdraw", json_output)


@app.command("reopen")
def reopen(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重开已完成任务。"""
    _simple_action(task_id, "reopen", json_output)


@app.command("accept-rejection")
def accept_rejection(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受退回并开始重做。"""
    _simple_action(task_id, "accept_rejection", json_output)


@app.command("review-claim")
def review_claim(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """认领审核任务。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.tasks.claim_review(task_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"[green]已认领审核[/green] task={result.task_id} reviewer={result.reviewer_id}"
        )


@app.command("review-approve")
def review_approve(
    task_id: str = typer.Argument(..., help="任务 ID"),
    expected_qc_digest: str | None = typer.Option(
        None, "--expected-qc-digest", help="预期 QC digest"
    ),
    warning_issue_ids: list[str] | None = typer.Option(
        None, "--warning-issue-id", help="确认的 warning issue ID，可重复"
    ),
    note: str | None = typer.Option(None, "--note", help="审核备注"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """通过审核。"""
    fields = {
        key: value
        for key, value in {
            "expected_qc_digest": expected_qc_digest,
            "warning_issue_ids": warning_issue_ids,
            "note": note,
        }.items()
        if value is not None
    }
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.tasks.approve_review(task_id, **fields)
    _print_result(result, json_output)


@app.command("review-reject")
def review_reject(
    task_id: str = typer.Argument(..., help="任务 ID"),
    reason_type: RejectReason = typer.Option(
        ..., "--reason-type", help="结构化退回原因"
    ),
    reason: str = typer.Option(..., "--reason", help="详细原因"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """退回审核任务。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.tasks.reject_review(task_id, reason_type.value, reason)
    _print_result(result, json_output)
