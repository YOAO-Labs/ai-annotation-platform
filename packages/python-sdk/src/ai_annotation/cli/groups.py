"""Platform data-group catalog commands."""

from __future__ import annotations

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
)
from ai_annotation.user_management_models import GroupCreate, GroupUpdate

app = typer.Typer(
    help="平台级数据组目录。删除会清除所有账号的数据组关联。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap groups query --search annotator --json[/]",
)


def _text_groups(groups: list[object]) -> None:
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("说明")
    table.add_column("活跃成员")
    table.add_column("创建时间")
    for group in groups:
        created_at = getattr(group, "created_at", None)
        table.add_row(
            str(getattr(group, "id", "")),
            str(getattr(group, "name", "")),
            str(getattr(group, "description", None) or "-"),
            str(getattr(group, "member_count", 0)),
            created_at.isoformat() if created_at else "-",
        )
    console.print(table)


@app.command("list")
def list_(
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取兼容数组接口中的全部数据组。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            groups = client.groups.list()
    if json_output:
        print_json([group.model_dump(mode="json") for group in groups])
    else:
        _text_groups(groups)


@app.command("query")
def query(
    page: int = typer.Option(1, min=1, help="页码，从 1 开始"),
    page_size: int = typer.Option(50, min=1, max=500, help="每页条数"),
    search: str | None = typer.Option(None, "--search", help="数据组名称关键字"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """分页查询数据组。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.groups.query(page=page, page_size=page_size, search=search)
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    _text_groups(result.items)
    console.print(f"第 {result.page}/{result.pages} 页 · 共 {result.total} 个")


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="数据组名称"),
    description: str | None = typer.Option(None, "--description", help="说明"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建一个平台级数据组。"""
    request = GroupCreate(name=name, description=description)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.groups.create(request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]数据组已创建[/green] {result.id} · {result.name}")


@app.command("update")
def update(
    group_id: str = typer.Argument(..., help="数据组 ID"),
    name: str | None = typer.Option(None, "--name", help="新名称"),
    description: str | None = typer.Option(None, "--description", help="新说明"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新数据组；重命名会同步所有账号的缓存组名。"""
    fields = {
        key: value
        for key, value in {"name": name, "description": description}.items()
        if value is not None
    }
    if not fields:
        raise typer.BadParameter("至少提供 --name 或 --description")
    confirm_destructive(f"确认更新数据组 {group_id}?", yes, json_output)
    request = GroupUpdate(**fields)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.groups.update(group_id, request)
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        console.print(f"[green]数据组已更新[/green] {result.id} · {result.name}")


@app.command("delete")
def delete(
    group_id: str = typer.Argument(..., help="数据组 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除数据组，并清除所有用户的 group_id / group_name。"""
    confirm_destructive(
        f"确认删除数据组 {group_id}? 所有关联账号将变为未分组。", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.groups.delete(group_id)
    if json_output:
        print_json({"deleted": True, "group_id": group_id})
    else:
        console.print(f"[green]数据组及其关联已清除[/green] {group_id}")
