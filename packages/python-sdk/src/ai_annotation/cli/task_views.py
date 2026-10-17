"""aap task-views commands for saved views and project task queries."""

from __future__ import annotations

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
from ai_annotation.task_view_models import (
    ProjectTaskQueryRequest,
    ProjectTaskViewCopyRequest,
    ProjectTaskViewCreate,
    ProjectTaskViewUpdate,
)

app = typer.Typer(
    help="浏览 Data Manager 筛选、执行任务查询并管理保存视图。",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog=(
        "示例: [dim]aap task-views list --project P-1 --json[/] · "
        "[dim]aap task-views query --project P-1 --body - --json[/]"
    ),
)


def _scope(value: str) -> str:
    if value not in {"tasks", "objects", "tracks"}:
        raise typer.BadParameter(
            "必须是 tasks、objects 或 tracks", param_hint="--scope"
        )
    return value


def _print_view(view) -> None:
    table = Table(title=view.name)
    table.add_column("字段")
    table.add_column("值")
    for key in (
        "id",
        "key",
        "project_id",
        "owner_id",
        "entity_scope",
        "visibility",
        "builtin",
        "task_count",
        "result_count",
        "invalid_fields",
    ):
        table.add_row(key, str(getattr(view, key)))
    console.print(table)


def _print_query(result) -> None:
    table = Table(title=f"任务 {result.total} 项 · offset {result.offset}")
    table.add_column("ID")
    table.add_column("任务编号")
    table.add_column("状态")
    table.add_column("文件")
    table.add_column("标注")
    for task in result.items:
        table.add_row(
            str(task.id),
            task.display_id,
            task.status,
            task.file_name,
            str(task.annotation_count),
        )
    console.print(table)


def _emit_view(view, json_output: bool) -> None:
    if json_output:
        print_json(view.model_dump(mode="json"))
    else:
        _print_view(view)


def _emit_query(result, json_output: bool) -> None:
    if json_output:
        print_json(result.model_dump(mode="json"))
    else:
        _print_query(result)


@app.command("list")
def list_views(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    scope: str = typer.Option("tasks", "--scope", help="tasks、objects 或 tracks"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出内置视图和当前账号可见的保存视图。"""
    entity_scope = _scope(scope)
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_views.list(project_id, entity_scope=entity_scope)
    if json_output:
        print_json(result.model_dump(mode="json"))
        return
    table = Table(title=f"项目视图 · {entity_scope}")
    table.add_column("ID / key")
    table.add_column("名称")
    table.add_column("可见性")
    table.add_column("结果")
    table.add_column("无效字段")
    for view in result.items:
        table.add_row(
            str(view.id or view.key),
            view.name,
            view.visibility,
            str(view.result_count),
            ", ".join(view.invalid_fields),
        )
    console.print(table)


@app.command("get")
def get_view(
    view_id: str = typer.Argument(..., help="保存视图 UUID"),
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取一条保存视图。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_views.get(project_id, view_id)
    _emit_view(result, json_output)


@app.command("create")
def create_view(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """保存筛选条件、排序和列设置。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTaskViewCreate)
        with get_client(json_output) as client:
            result = client.task_views.create(project_id, body)
    _emit_view(result, json_output)


@app.command("update")
def update_view(
    view_id: str = typer.Argument(..., help="保存视图 UUID"),
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新保存视图；用空对象或空列表清空对应配置。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTaskViewUpdate)
        with get_client(json_output) as client:
            result = client.task_views.update(project_id, view_id, body)
    _emit_view(result, json_output)


@app.command("delete")
def delete_view(
    view_id: str = typer.Argument(..., help="保存视图 UUID"),
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过交互确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除保存视图配置，不删除任务数据。"""
    with cli_errors(json_output):
        confirm_destructive(
            f"确认删除项目 {project_id} 的视图 {view_id}?", yes, json_output
        )
        with get_client(json_output) as client:
            client.task_views.delete(project_id, view_id)
    if json_output:
        print_json({"deleted": True, "project_id": project_id, "view_id": view_id})
    else:
        console.print(f"[green]视图已删除[/green] id={view_id}")


@app.command("copy")
def copy_view(
    view_id: str = typer.Argument(..., help="来源保存视图 UUID"),
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """复制视图；普通成员复制共享视图时 body 应指定 visibility=private。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTaskViewCopyRequest)
        with get_client(json_output) as client:
            result = client.task_views.copy(project_id, view_id, body)
    _emit_view(result, json_output)


@app.command("query")
def query_tasks(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    body_path: Path = typer.Option(
        ..., "--body", help="UTF-8 JSON 文件；使用 - 从 stdin 读取"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """按 filter_json 查询任务；此 POST 是只读查询。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, ProjectTaskQueryRequest)
        with get_client(json_output) as client:
            result = client.task_views.query(project_id, body)
    _emit_query(result, json_output)


@app.command("tasks")
def query_view_tasks(
    view_id: str = typer.Argument(..., help="保存任务视图 UUID"),
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="起始偏移"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """使用保存的任务视图查询一页任务。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.task_views.query_view(
                project_id, view_id, limit=limit, offset=offset
            )
    _emit_query(result, json_output)
