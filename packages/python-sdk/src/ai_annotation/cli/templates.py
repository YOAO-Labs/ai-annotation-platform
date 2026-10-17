"""aap templates subcommands."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    read_json_body,
)
from ai_annotation.project_template_models import (
    ProjectTemplate,
    ProjectTemplateCreate,
    ProjectTemplateUpdate,
)

app = typer.Typer(
    help="项目模板: 浏览、创建、更新、复制或删除模板。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap templates list --scope public --json[/] · [dim]aap templates create --body template.json[/]",
)


class TemplateScopeOption(str, Enum):
    private = "private"
    organization = "organization"
    public = "public"


def _show_template(template: ProjectTemplate, json_output: bool) -> None:
    payload = template.model_dump(mode="json", by_alias=True)
    if json_output:
        print_json(payload)
    else:
        console.print_json(data=payload)


@app.command("list")
def list_(
    scope: TemplateScopeOption | None = typer.Option(
        None, "--scope", help="可见范围: private、organization 或 public"
    ),
    type_key: list[str] | None = typer.Option(
        None, "--type-key", help="模板类型 key，可重复"
    ),
    search: str | None = typer.Option(None, "--search", help="模板名称包含搜索"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON 数组"),
) -> None:
    """列出当前账号可见的项目模板。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            templates = client.project_templates.list(
                scope=scope.value if scope is not None else None,
                type_key=type_key,
                search=search,
            )
    if json_output:
        print_json([item.model_dump(mode="json", by_alias=True) for item in templates])
        return
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("范围")
    table.add_column("类型")
    table.add_column("创建者")
    table.add_column("使用次数")
    for item in templates:
        table.add_row(
            item.display_id,
            item.name,
            item.scope,
            item.type_key,
            item.created_by_name or str(item.created_by),
            str(item.usage_count),
        )
    console.print(table)


@app.command("get")
def get(
    template_id: str = typer.Argument(..., help="模板 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取可见模板的完整配置。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            template = client.project_templates.get(template_id)
    _show_template(template, json_output)


@app.command("create")
def create(
    body_path: Path = typer.Option(
        ..., "--body", help="JSON 文件路径；用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """用 JSON 请求体创建模板。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTemplateCreate)
        with get_client(json_output) as client:
            template = client.project_templates.create(body)
    _show_template(template, json_output)


@app.command("update")
def update(
    template_id: str = typer.Argument(..., help="模板 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="JSON PATCH 文件；用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """只提交 JSON PATCH 中显式提供的字段。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTemplateUpdate)
        with get_client(json_output) as client:
            template = client.project_templates.update(template_id, body)
    _show_template(template, json_output)


@app.command("duplicate")
def duplicate(
    template_id: str = typer.Argument(..., help="可见模板 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """复制可见模板为当前账号的私有模板。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            template = client.project_templates.duplicate(template_id)
    _show_template(template, json_output)


@app.command("delete")
def delete(
    template_id: str = typer.Argument(..., help="模板 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除模板；JSON 模式必须显式传 --yes。"""
    confirm_destructive(f"确认删除模板 {template_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.project_templates.delete(template_id)
    if json_output:
        print_json({"deleted": True, "template_id": template_id})
    else:
        console.print(f"[green]模板已删除[/green] {template_id}")
