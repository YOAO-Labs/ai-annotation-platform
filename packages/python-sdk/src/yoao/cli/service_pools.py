"""yoao service-pools 逻辑服务池管理。"""

import typer
from rich.table import Table

from yoao.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
)

app = typer.Typer(
    help="模型服务资源池、成员与路由观测（超级管理员）。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _print_pool(pool, json_output: bool) -> None:
    if json_output:
        print_json(pool.model_dump(mode="json"))
    else:
        console.print(
            f"[green]{pool.name}[/green] ID={pool.id} "
            f"启用={pool.enabled} 成员={len(pool.members)}"
        )


@app.command("list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出模型服务资源池。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pools = client.service_pools.list()
    if json_output:
        print_json([pool.model_dump(mode="json") for pool in pools])
        return
    table = Table("ID", "名称", "启用", "成员", "路由代次")
    for pool in pools:
        table.add_row(
            str(pool.id),
            pool.name,
            "yes" if pool.enabled else "no",
            str(len(pool.members)),
            str(pool.routing_generation),
        )
    console.print(table)


@app.command("get")
def get(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看模型服务资源池及其成员。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.get(pool_id)
    _print_pool(pool, json_output)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="名称"),
    legacy_instance_id: str | None = typer.Option(
        None, "--legacy-instance-id", help="历史模型服务实例 ID"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建模型服务资源池。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.create(name, legacy_instance_id)
    _print_pool(pool, json_output)


@app.command("update")
def update(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    name: str | None = typer.Option(None, "--name", help="名称"),
    enabled: bool | None = typer.Option(
        None, "--enabled/--disabled", help="池启用状态"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新模型服务资源池。"""
    fields = {
        key: value
        for key, value in {"name": name, "enabled": enabled}.items()
        if value is not None
    }
    if not fields:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.update(pool_id, **fields)
    _print_pool(pool, json_output)


@app.command("delete")
def delete(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除模型服务资源池。"""
    confirm_destructive(f"确认删除模型服务资源池 {pool_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.service_pools.delete(pool_id)
    if json_output:
        print_json({"deleted": True, "pool_id": pool_id})
    else:
        console.print(f"[green]模型服务资源池已删除[/green] {pool_id}")


@app.command("member-add")
def member_add(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    weight: int = typer.Option(1, "--weight", min=1, max=100, help="权重"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """添加或更新资源池成员。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.add_member(pool_id, registry_id, weight)
    _print_pool(pool, json_output)


def _member_destructive(
    pool_id: str,
    registry_id: str,
    action: str,
    yes: bool,
    json_output: bool,
) -> None:
    action_label = {"remove_member": "移除", "drain_member": "排空"}[action]
    confirm_destructive(
        f"确认对资源池 {pool_id} 中的实例 {registry_id} 执行{action_label}?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = getattr(client.service_pools, action)(pool_id, registry_id)
    _print_pool(pool, json_output)


@app.command("member-remove")
def member_remove(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """移除已静默的资源池成员。"""
    _member_destructive(pool_id, registry_id, "remove_member", yes, json_output)


@app.command("member-drain")
def member_drain(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将资源池成员转为排空状态。"""
    _member_destructive(pool_id, registry_id, "drain_member", yes, json_output)


@app.command("member-resume")
def member_resume(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """恢复资源池成员流量。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.resume_member(pool_id, registry_id)
    _print_pool(pool, json_output)


def _print_drift(preview, json_output: bool) -> None:
    if json_output:
        print_json(preview.model_dump(mode="json"))
    else:
        console.print(
            f"has_drift={preview.has_drift} can_accept={preview.can_accept} "
            f"candidate={preview.candidate_fingerprint or '-'} "
            f"fields={','.join(preview.differing_fields) or '-'}"
        )


@app.command("drift-preview")
def drift_preview(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览成员能力指纹漂移。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            preview = client.service_pools.preview_capability_drift(
                pool_id, registry_id
            )
    _print_drift(preview, json_output)


@app.command("drift-accept")
def drift_accept(
    pool_id: str = typer.Argument(..., help="模型服务资源池 ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="模型服务实例 ID"),
    expected_fingerprint: str = typer.Option(
        ..., "--expected-fingerprint", help="预览得到的候选能力指纹"
    ),
    enable_pool: bool = typer.Option(False, "--enable-pool", help="接受后启用池"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """使用明确的候选能力指纹接受能力漂移。"""
    if json_output:
        confirm_destructive("确认接受能力漂移?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            if not json_output:
                preview = client.service_pools.preview_capability_drift(
                    pool_id, registry_id
                )
                _print_drift(preview, False)
                confirm_destructive("确认接受能力漂移?", yes, False)
            pool = client.service_pools.accept_capability_drift(
                pool_id,
                registry_id,
                expected_fingerprint,
                enable_pool=enable_pool,
            )
    _print_pool(pool, json_output)


@app.command("topology")
def topology(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看模型服务资源池拓扑。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.service_pools.topology()
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"router_mode={result.router_mode} pools={len(result.pools)} "
            f"generated_at={result.generated_at.isoformat()}"
        )


@app.command("runtime")
def runtime(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看模型服务资源池运行快照。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.service_pools.runtime_snapshot()
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(
            f"router_mode={result.router_mode} pools={len(result.pools)} "
            f"partial={result.partial}"
        )
