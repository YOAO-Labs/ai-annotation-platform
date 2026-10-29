"""aap ml-backends 项目启用与健康监控。"""

from __future__ import annotations

import typer
from rich.table import Table

from ai_annotation.cli._output import cli_errors, console, get_client, print_json
from ai_annotation.models import MLBackend

app = typer.Typer(
    help="项目模型服务启用与健康监控。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "示例: [dim]aap ml-backends list --project P-1[/] · "
        "[dim]aap ml-backends get <backend_id> --project P-1[/]"
    ),
)


def _gpu_util(b: MLBackend) -> str:
    gpu = b.health_meta.gpu_info if b.health_meta else None
    pct = gpu.gpu_utilization_percent if gpu else None
    return f"{pct}%" if pct is not None else "-"


def _model_version(b: MLBackend) -> str:
    return (b.health_meta.model_version if b.health_meta else None) or "-"


@app.command("list")
def list_(
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目接入的模型服务及健康状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            backends = client.ml_backends.list(project_id)
    if json_output:
        print_json([b.model_dump(mode="json") for b in backends])
        return
    table = Table()
    table.add_column("名称")
    table.add_column("状态")
    table.add_column("model_version")
    table.add_column("GPU")
    table.add_column("url")
    for b in backends:
        color = "green" if b.state == "connected" else "red"
        table.add_row(
            b.name,
            f"[{color}]{b.state}[/{color}]",
            _model_version(b),
            _gpu_util(b),
            b.url,
        )
    console.print(table)


@app.command("get")
def get(
    backend_id: str = typer.Argument(..., help="模型服务 ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看单个模型服务详情（含 health_meta）。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            backend = client.ml_backends.get(project_id, backend_id)
    if json_output:
        print_json(backend.model_dump(mode="json"))
    else:
        console.print(backend.model_dump(mode="json"))


@app.command("available")
def available(
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出全局 registry backend 及本项目启用态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            items = client.ml_backends.list_available(project_id)
    if json_output:
        print_json([item.model_dump(mode="json") for item in items])
        return
    table = Table("ID", "名称", "状态", "项目启用")
    for item in items:
        table.add_row(
            str(item.backend.id),
            item.backend.name,
            item.backend.state,
            "yes" if item.enabled else "no",
        )
    console.print(table)


def _set_backend_enabled(
    project_id: str, backend_id: str, enabled: bool, json_output: bool
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            item = client.ml_backends.set_enablement(project_id, backend_id, enabled)
    if json_output:
        print_json(item.model_dump(mode="json"))
    else:
        console.print(
            f"[green]{'已启用' if enabled else '已停用'}[/green] {backend_id}"
        )


@app.command("enable")
def enable(
    backend_id: str = typer.Argument(..., help="registry backend ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """为项目启用 registry backend。"""
    _set_backend_enabled(project_id, backend_id, True, json_output)


@app.command("disable")
def disable(
    backend_id: str = typer.Argument(..., help="registry backend ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """为项目停用 registry backend。"""
    _set_backend_enabled(project_id, backend_id, False, json_output)


@app.command("health")
def health(
    backend_id: str = typer.Argument(..., help="registry backend ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """对项目已启用 backend 执行健康检查。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_backends.check_health(project_id, backend_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"{result.backend_name}: {result.status}")


@app.command("pools")
def pools(
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目可用 service pool 及启用态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            items = client.ml_backends.list_available_pools(project_id)
    if json_output:
        print_json([item.model_dump(mode="json") for item in items])
        return
    table = Table("ID", "名称", "成员", "项目启用")
    for item in items:
        table.add_row(
            str(item.pool.id),
            item.pool.name,
            str(item.pool.member_count),
            "yes" if item.enabled else "no",
        )
    console.print(table)


def _set_pool_enabled(
    project_id: str, pool_id: str, enabled: bool, json_output: bool
) -> None:
    with cli_errors(json_output):
        with get_client(json_output) as client:
            item = client.ml_backends.set_pool_enablement(project_id, pool_id, enabled)
    if json_output:
        print_json(item.model_dump(mode="json"))
    else:
        console.print(
            f"[green]{'已启用' if enabled else '已停用'}[/green] pool {pool_id}"
        )


@app.command("pool-enable")
def pool_enable(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """为项目启用 service pool。"""
    _set_pool_enabled(project_id, pool_id, True, json_output)


@app.command("pool-disable")
def pool_disable(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    project_id: str = typer.Option(..., "--project", help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """为项目停用 service pool。"""
    _set_pool_enabled(project_id, pool_id, False, json_output)
