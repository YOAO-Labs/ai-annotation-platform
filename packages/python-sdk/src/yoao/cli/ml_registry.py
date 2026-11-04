"""yoao ml-registry 全局物理 backend 管理。"""

import os

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
    help="全局模型服务注册表（超级管理员）。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _read_auth_token(env_name: str | None, prompt: bool) -> str | None:
    if env_name and prompt:
        raise typer.BadParameter("--auth-token-env 与 --prompt-auth-token 不能同时使用")
    if prompt:
        return typer.prompt("认证 token", hide_input=True)
    if env_name:
        token = os.environ.get(env_name)
        if token is None:
            raise typer.BadParameter(f"环境变量 {env_name} 未设置")
        return token
    return None


@app.command("list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出全局已登记的模型服务实例。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            backends = client.ml_registry.list()
    if json_output:
        print_json([backend.model_dump(mode="json") for backend in backends])
        return
    table = Table("ID", "名称", "状态", "URL")
    for backend in backends:
        table.add_row(str(backend.id), backend.name, backend.state, backend.url)
    console.print(table)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="名称"),
    url: str = typer.Option(..., "--url", help="模型服务地址"),
    interactive: bool = typer.Option(False, "--interactive", help="交互式模型服务"),
    auth_method: str = typer.Option("none", "--auth-method", help="认证方式"),
    auth_token_env: str | None = typer.Option(
        None, "--auth-token-env", help="从指定环境变量读取认证 token"
    ),
    prompt_auth_token: bool = typer.Option(
        False, "--prompt-auth-token", help="隐藏输入认证 token"
    ),
    gpu_resource_id: str | None = typer.Option(
        None, "--gpu-resource-id", help="GPU 资源 ID"
    ),
    vram_budget_mb: int | None = typer.Option(
        None, "--vram-budget-mb", min=1, help="VRAM 预算 MB"
    ),
    eviction_priority: int = typer.Option(0, "--eviction-priority", help="驱逐优先级"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """登记全局模型服务实例；登记不代表服务健康或项目已启用。"""
    auth_token = _read_auth_token(auth_token_env, prompt_auth_token)
    fields = {
        key: value
        for key, value in {
            "name": name,
            "url": url,
            "is_interactive": interactive,
            "auth_method": auth_method,
            "auth_token": auth_token,
            "gpu_resource_id": gpu_resource_id,
            "vram_budget_mb": vram_budget_mb,
            "eviction_priority": eviction_priority,
        }.items()
        if value is not None
    }
    with cli_errors(json_output):
        with get_client(json_output) as client:
            backend = client.ml_registry.create(**fields)
    if json_output:
        print_json(backend.model_dump(mode="json"))
    else:
        console.print(f"[green]模型服务实例已登记[/green] {backend.id}")


@app.command("update")
def update(
    registry_id: str = typer.Argument(..., help="全局模型服务实例 ID"),
    name: str | None = typer.Option(None, "--name", help="名称"),
    url: str | None = typer.Option(None, "--url", help="模型服务地址"),
    auth_method: str | None = typer.Option(None, "--auth-method", help="认证方式"),
    auth_token_env: str | None = typer.Option(
        None, "--auth-token-env", help="从指定环境变量读取认证 token"
    ),
    prompt_auth_token: bool = typer.Option(
        False, "--prompt-auth-token", help="隐藏输入认证 token"
    ),
    gpu_resource_id: str | None = typer.Option(
        None, "--gpu-resource-id", help="GPU 资源 ID"
    ),
    vram_budget_mb: int | None = typer.Option(
        None, "--vram-budget-mb", min=1, help="VRAM 预算 MB"
    ),
    eviction_priority: int | None = typer.Option(
        None, "--eviction-priority", help="驱逐优先级"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新全局模型服务实例配置。"""
    auth_token = _read_auth_token(auth_token_env, prompt_auth_token)
    fields = {
        key: value
        for key, value in {
            "name": name,
            "url": url,
            "auth_method": auth_method,
            "auth_token": auth_token,
            "gpu_resource_id": gpu_resource_id,
            "vram_budget_mb": vram_budget_mb,
            "eviction_priority": eviction_priority,
        }.items()
        if value is not None
    }
    if not fields:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            backend = client.ml_registry.update(registry_id, **fields)
    if json_output:
        print_json(backend.model_dump(mode="json"))
    else:
        console.print(f"[green]模型服务实例配置已更新[/green] {registry_id}")


def _destructive(registry_id: str, action: str, yes: bool, json_output: bool) -> None:
    action_label = {"delete": "删除", "unload": "卸载模型"}[action]
    confirm_destructive(
        f"确认对模型服务实例 {registry_id} 执行{action_label}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = getattr(client.ml_registry, action)(registry_id)
    if json_output:
        if result is None:
            print_json({"deleted": True, "registry_id": registry_id})
        else:
            print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]{action_label}已完成[/green] {registry_id}")


@app.command("delete")
def delete(
    registry_id: str = typer.Argument(..., help="全局模型服务实例 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除已静默的模型服务实例。"""
    _destructive(registry_id, "delete", yes, json_output)


@app.command("health")
def health(
    registry_id: str = typer.Argument(..., help="全局模型服务实例 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """检查模型服务实例健康状态并刷新能力快照。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.ml_registry.check_health(registry_id)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"{result.backend_name}: {result.status}")


@app.command("unload")
def unload(
    registry_id: str = typer.Argument(..., help="全局模型服务实例 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """在服务端静默守卫通过后卸载实例中的模型。"""
    _destructive(registry_id, "unload", yes, json_output)
