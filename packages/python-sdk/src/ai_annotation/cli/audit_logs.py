"""aap audit-logs, including explicit local export destinations."""

from pathlib import Path

import typer

from ai_annotation.audit_models import AuditExportQuery, AuditLogQuery
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(
    help="按权限读取审计、月汇总与归档，并导出到明确文件。", no_args_is_help=True
)


@app.command("list")
def list_(
    query_path: Path | None = typer.Option(
        None, "--query", help="过滤／分页 JSON；- 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        query = (
            read_json_body(query_path, AuditLogQuery)
            if query_path is not None
            else AuditLogQuery()
        )
        with get_client(json_output) as client:
            result = client.audit_logs.list(query)
    print_learning_result(result, json_output)


@app.command("monthly-summary")
def monthly_summary(
    month: str = typer.Argument(..., help="UTC YYYY-MM"),
    business_only: bool = typer.Option(True, "--business-only/--include-http"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.audit_logs.monthly_summary(
                month, business_only=business_only
            )
    print_learning_result(result, json_output)


@app.command("archives")
def archives(json_output: bool = typer.Option(False, "--json")) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.audit_logs.archives()
    print_learning_result(result, json_output)


@app.command("archive")
def archive(
    year: int = typer.Argument(..., min=2000, max=9999),
    month: int = typer.Argument(..., min=1, max=12),
    limit: int = typer.Option(100, "--limit", min=1, max=1000),
    offset: int = typer.Option(0, "--offset", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.audit_logs.archive(year, month, limit=limit, offset=offset)
    print_learning_result(result, json_output)


@app.command("export")
def export(
    out: Path = typer.Option(..., "--out"),
    query_path: Path | None = typer.Option(
        None, "--query", help="含 format 和筛选的 JSON；- 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """导出 CSV／JSON 附件；会产生审计记录，不自动重试。"""
    with cli_errors(json_output):
        query = (
            read_json_body(query_path, AuditExportQuery)
            if query_path is not None
            else AuditExportQuery()
        )
        confirm_destructive("确认将审计数据导出到本地文件?", yes, json_output)
        with get_client(json_output) as client:
            result = client.audit_logs.export(out, query)
    print_learning_result(result, json_output)
