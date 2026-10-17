"""CLI for administrative preannotation, health, analytics and alias operations."""

from pathlib import Path

import typer

from ai_annotation.admin_operation_models import (
    PreannotateBulkClearRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(
    help="管理端预标队列、系统健康与统计查询。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_PANELS = {
    "throughput_daily",
    "reject_rate_by_type",
    "duration_dist",
    "activity_heatmap",
}


@app.command("preannotate-queue")
def preannotate_queue(
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="返回批次数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号可管理项目中的已预标批次。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.admin_operations.preannotate_queue(limit=limit)
    print_learning_result(result, json_output)


@app.command("preannotate-clear")
def preannotate_clear(
    body_file: Path = typer.Option(
        ..., "--body-file", help="请求 JSON 文件，或 - 读取 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认清理所列 batch 数据"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """清理预测候选，或完整重置所列 batch。操作可能部分成功。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, PreannotateBulkClearRequest)
        confirm_destructive(
            f"确认以 {body.mode} 模式处理 {len(body.batch_ids)} 个 batch?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.admin_operations.bulk_clear_preannotate(body)
    print_learning_result(result, json_output)


@app.command("preannotate-summary")
def preannotate_summary(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前账号项目范围内的预标汇总。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.admin_operations.preannotate_summary()
    print_learning_result(result, json_output)


@app.command("system-health")
def system_health(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """并发检查数据库、Redis、MinIO 与 Celery。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.admin_operations.system_health()
    print_learning_result(result, json_output)


@app.command("analytics")
def analytics(
    panel: str = typer.Argument(
        ...,
        help="throughput_daily / reject_rate_by_type / duration_dist / activity_heatmap",
    ),
    days: int = typer.Option(30, "--days", min=1, max=365, help="查询天数"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取固定 DuckDB 分析面板，不接受任意查询语句。"""
    if panel not in _PANELS:
        raise typer.BadParameter("panel 必须是一个受支持的固定面板名称")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.admin_operations.analytics(panel, days=days)
    print_learning_result(result, json_output)


@app.command("alias-frequency")
def alias_frequency(
    project_id: str = typer.Argument(..., help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看项目预测标签频次；项目范围由服务端校验。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.admin_operations.alias_frequency(project_id)
    print_learning_result(result, json_output)
