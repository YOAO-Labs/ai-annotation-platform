"""aap projects 子命令。"""

from __future__ import annotations

from enum import Enum

import typer
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
)
from ai_annotation.models import Project

app = typer.Typer(
    help="项目管理: 列出、创建、更新或删除项目。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="示例: [dim]aap projects create --name demo --type image[/] · [dim]aap projects list --json[/]",
)


class ProjectType(str, Enum):
    image = "image"
    video = "video"
    lidar = "lidar"


def _tasks_progress(p: Project) -> str:
    total = getattr(p, "total_tasks", None)
    done = getattr(p, "completed_tasks", None)
    if total is None or done is None:
        return "-"
    return f"{done}/{total}"


@app.command("list")
def list_(
    status: str | None = typer.Option(None, "--status", help="项目状态"),
    search: str | None = typer.Option(None, "--search", help="名称搜索"),
    type_key: list[str] | None = typer.Option(
        None, "--type-key", help="项目类型 key，可重复"
    ),
    data_type: list[str] | None = typer.Option(
        None, "--data-type", help="数据类型，可重复"
    ),
    member_id: str | None = typer.Option(None, "--member-id", help="成员用户 ID"),
    project_role: str | None = typer.Option(None, "--project-role", help="项目角色"),
    created_from: str | None = typer.Option(
        None, "--created-from", help="创建日期起始"
    ),
    created_to: str | None = typer.Option(None, "--created-to", help="创建日期截止"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            projects = client.projects.list(
                status=status,
                search=search,
                type_key=type_key,
                data_type=data_type,
                member_id=member_id,
                project_role=project_role,
                created_from=created_from,
                created_to=created_to,
            )
    if json_output:
        print_json([p.model_dump(mode="json") for p in projects])
        return
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("类型")
    table.add_column("状态")
    table.add_column("任务进度")
    for p in projects:
        table.add_row(p.display_id, p.name, p.data_type, p.status, _tasks_progress(p))
    console.print(table)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="项目名称"),
    type_: ProjectType = typer.Option(..., "--type", help="数据类型"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建项目。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            project = client.projects.create(name=name, data_type=type_.value)
    if json_output:
        print_json(project.model_dump(mode="json"))
    else:
        console.print(
            f"[green]项目已创建[/green] id={project.id} display_id={project.display_id}"
        )


@app.command("update")
def update(
    project_id: str = typer.Argument(..., help="项目 ID"),
    name: str | None = typer.Option(None, "--name", help="项目名称"),
    status: str | None = typer.Option(None, "--status", help="项目状态"),
    type_label: str | None = typer.Option(None, "--type-label", help="类型名称"),
    type_key: str | None = typer.Option(None, "--type-key", help="类型 key"),
    data_type: str | None = typer.Option(None, "--data-type", help="数据类型"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新项目的显式指定字段。"""
    fields = {
        key: value
        for key, value in {
            "name": name,
            "status": status,
            "type_label": type_label,
            "type_key": type_key,
            "data_type": data_type,
        }.items()
        if value is not None
    }
    if not fields:
        raise typer.BadParameter("至少提供一个更新选项")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            project = client.projects.update(project_id, **fields)
    if json_output:
        print_json(project.model_dump(mode="json"))
    else:
        console.print(f"[green]项目已更新[/green] id={project.id}")


@app.command("delete")
def delete(
    project_id: str = typer.Argument(..., help="项目 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除项目。"""
    confirm_destructive(f"确认删除项目 {project_id}?", yes, json_output)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            client.projects.delete(project_id)
    if json_output:
        print_json({"deleted": True, "project_id": project_id})
    else:
        console.print(f"[green]项目已删除[/green] {project_id}")
