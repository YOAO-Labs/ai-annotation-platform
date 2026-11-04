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
    help="ML service pool 、成员与路由观测（super-admin）。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _print_pool(pool, json_output: bool) -> None:
    if json_output:
        print_json(pool.model_dump(mode="json"))
    else:
        console.print(
            f"[green]{pool.name}[/green] id={pool.id} "
            f"enabled={pool.enabled} members={len(pool.members)}"
        )


@app.command("list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出 service pool。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pools = client.service_pools.list()
    if json_output:
        print_json([pool.model_dump(mode="json") for pool in pools])
        return
    table = Table("ID", "名称", "启用", "成员", "generation")
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
    pool_id: str = typer.Argument(..., help="service pool ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看 service pool 与成员。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.get(pool_id)
    _print_pool(pool, json_output)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="名称"),
    legacy_instance_id: str | None = typer.Option(
        None, "--legacy-instance-id", help="legacy registry instance ID"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建 service pool。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = client.service_pools.create(name, legacy_instance_id)
    _print_pool(pool, json_output)


@app.command("update")
def update(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    name: str | None = typer.Option(None, "--name", help="名称"),
    enabled: bool | None = typer.Option(
        None, "--enabled/--disabled", help="池启用状态"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新 service pool。"""
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
    pool_id: str = typer.Argument(..., help="service pool ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除 service pool。"""
    confirm_destructive(f"确认删除 service pool {pool_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.service_pools.delete(pool_id)
    if json_output:
        print_json({"deleted": True, "pool_id": pool_id})
    else:
        console.print(f"[green]service pool 已删除[/green] {pool_id}")


@app.command("member-add")
def member_add(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
    weight: int = typer.Option(1, "--weight", min=1, max=100, help="权重"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """添加或更新 pool member。"""
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
    confirm_destructive(
        f"确认 {action} member {registry_id} from pool {pool_id}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            pool = getattr(client.service_pools, action)(pool_id, registry_id)
    _print_pool(pool, json_output)


@app.command("member-remove")
def member_remove(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """移除已静默的 pool member。"""
    _member_destructive(pool_id, registry_id, "remove_member", yes, json_output)


@app.command("member-drain")
def member_drain(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将 pool member 转为 draining。"""
    _member_destructive(pool_id, registry_id, "drain_member", yes, json_output)


@app.command("member-resume")
def member_resume(
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """恢复 pool member 流量。"""
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
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
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
    pool_id: str = typer.Argument(..., help="service pool ID"),
    registry_id: str = typer.Option(..., "--registry-id", help="registry instance ID"),
    expected_fingerprint: str = typer.Option(
        ..., "--expected-fingerprint", help="预览得到的 candidate fingerprint"
    ),
    enable_pool: bool = typer.Option(False, "--enable-pool", help="接受后启用池"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """使用显式 candidate fingerprint 接受能力漂移。"""
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
    """查看 service pool 拓扑。"""
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
    """查看 service pool runtime snapshot。"""
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
