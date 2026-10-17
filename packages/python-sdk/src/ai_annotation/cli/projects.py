"""aap projects 子命令。"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.table import Table

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    console,
    get_client,
    print_json,
    print_learning_result,
    read_json_body,
)
from ai_annotation.models import Project
from ai_annotation.project_operation_models import (
    LidarExportPreflightRequest,
    ProjectClassRenameRequest,
    ProjectTransferRequest,
)

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
    type_: ProjectType | None = typer.Option(
        None, "--type", help="数据类型；省略时继承模板或源项目"
    ),
    type_key: str | None = typer.Option(None, "--type-key", help="覆盖项目类型 key"),
    type_label: str | None = typer.Option(None, "--type-label", help="覆盖类型名称"),
    template_id: str | None = typer.Option(None, "--template", help="模板 UUID"),
    source_project_id: str | None = typer.Option(
        None, "--source-project", help="源项目 UUID；与模板互斥"
    ),
    copy_annotation_guide: bool = typer.Option(
        False, "--copy-annotation-guide", help="从源项目复制标注指引"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """创建项目。"""
    with cli_errors(json_output):
        if template_id is not None and source_project_id is not None:
            raise typer.BadParameter("--template 与 --source-project 互斥")
        if copy_annotation_guide and (
            template_id is not None or source_project_id is None
        ):
            raise typer.BadParameter("--copy-annotation-guide 要求 --source-project")
        if (
            type_ is None
            and type_key is None
            and template_id is None
            and source_project_id is None
        ):
            raise typer.BadParameter(
                "提供 --type、--type-key、--template 或 --source-project"
            )
        fields = {"type_label": type_label} if type_label is not None else {}
        with get_client(json_output) as client:
            project = client.projects.create(
                name=name,
                data_type=type_.value if type_ is not None else None,
                type_key=type_key,
                template_id=template_id,
                source_project_id=source_project_id,
                copy_annotation_guide=copy_annotation_guide,
                **fields,
            )
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


@app.command("query")
def query(
    page: int = typer.Option(1, min=1, help="页码，从 1 开始"),
    page_size: int = typer.Option(20, min=1, max=100, help="每页条数"),
    status: str | None = typer.Option(None, "--status", help="项目状态"),
    search: str | None = typer.Option(None, "--search", help="名称关键字"),
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
    """分页查询项目。重复的 type-key / data-type 会作为重复 query 参数发送。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page_result = client.projects.query(
                page=page,
                page_size=page_size,
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
        print_learning_result(page_result, True)
        return
    table = Table()
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("类型")
    table.add_column("状态")
    table.add_column("任务进度")
    for project in page_result.items:
        table.add_row(
            project.display_id,
            project.name,
            project.data_type,
            project.status,
            _tasks_progress(project),
        )
    console.print(table)
    console.print(
        f"第 {page_result.page}/{page_result.pages} 页 · 共 {page_result.total} 个"
    )


@app.command("readiness")
def readiness(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看项目负责人可见的服务端启动就绪状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.readiness(project_id)
    if json_output:
        print_learning_result(result, True)
        return
    console.print(f"project_id={result.project_id} · guide_ready={result.guide_ready}")
    console.print(
        f"classes_ready={result.classes_ready} · datasets={result.linked_dataset_count} · "
        f"items={result.dataset_item_count} · tasks={result.task_count} · "
        f"linked_tasks={result.linked_task_count}"
    )
    console.print(
        f"batches={result.batch_count} · executable={result.executable_batch_count} · "
        f"assigned={result.assigned_batch_count} · activated={result.activated_batch_count}"
    )
    console.print(
        f"task_creation_active_jobs={result.task_creation_active_jobs} · "
        f"failed_jobs={result.task_creation_failed_jobs} · "
        f"latest={result.latest_task_creation_status or '-'}"
    )


@app.command("class-usage")
def class_usage(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看当前类别与属性键在活跃标注中的使用计数。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.class_usage(project_id)
    if json_output:
        print_learning_result(result, True)
    else:
        console.print_json(data=result.model_dump(mode="json"))


@app.command("rename-class")
def rename_class(
    project_id: str = typer.Argument(..., help="项目 ID"),
    old_name: str = typer.Option(..., "--old-name", help="原类别名"),
    new_name: str = typer.Option(..., "--new-name", help="新类别名"),
    tool_unit_id: str | None = typer.Option(
        None, "--tool-unit-id", help="只改此工具单位的类别绑定；标注名仍全项目同步"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """重命名类别绑定，并同步项目中同名的所有标注。"""
    try:
        request = ProjectClassRenameRequest(
            old_name=old_name, new_name=new_name, tool_unit_id=tool_unit_id
        )
    except ValidationError as exc:
        raise typer.BadParameter("类别名不能为空") from exc
    scope = f"工具单位 {tool_unit_id}" if tool_unit_id else "全部工具单位"
    confirm_destructive(
        f"确认在 {scope} 将类别 {request.old_name!r} 重命名为 {request.new_name!r}，并更新项目标注?",
        yes,
        json_output,
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.rename_class(project_id, request)
    print_learning_result(result, json_output)


@app.command("cleanup-orphans")
def cleanup_orphans(
    project_id: str = typer.Argument(..., help="项目 ID"),
    apply: bool = typer.Option(False, "--apply", help="实际清理，不只是预览"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认实际清理"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """默认只预览；--apply 会停用孤儿标注并清除孤儿属性键。"""
    if apply:
        confirm_destructive(
            f"确认清理项目 {project_id} 的孤儿标注与属性键?", yes, json_output
        )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.cleanup_annotation_orphans(
                project_id, dry_run=not apply
            )
    print_learning_result(result, json_output)


@app.command("datasets")
def datasets(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出项目已关联的数据集及项目内任务数。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            items = client.projects.list_datasets(project_id)
    if json_output:
        print_json([item.model_dump(mode="json") for item in items])
        return
    table = Table()
    table.add_column("数据集 ID")
    table.add_column("名称")
    table.add_column("类型")
    table.add_column("条目")
    table.add_column("项目任务")
    table.add_column("关联时间")
    for item in items:
        table.add_row(
            item.display_id,
            item.name,
            item.data_type,
            str(item.items_count),
            str(item.tasks_in_project),
            item.linked_at.isoformat() if item.linked_at else "-",
        )
    console.print(table)


@app.command("mention-candidates")
def mention_candidates(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前调用者可见的项目提及候选。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            items = client.projects.mention_candidates(project_id)
    if json_output:
        print_json([item.model_dump(mode="json") for item in items])
        return
    table = Table()
    table.add_column("用户 ID")
    table.add_column("姓名")
    table.add_column("邮箱")
    table.add_column("身份")
    for item in items:
        table.add_row(
            str(item.user_id), item.user_name, item.user_email or "-", item.kind
        )
    console.print(table)


@app.command("lidar-export-preflight")
def lidar_export_preflight(
    project_id: str = typer.Argument(..., help="LIDAR 项目 ID"),
    request_file: Path = typer.Argument(
        ..., help="Preflight JSON 请求文件，或 - 读取 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预检项目 LIDAR 导出目标，不创建导出任务。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, LidarExportPreflightRequest)
        with get_client(json_output) as client:
            result = client.projects.lidar_export_preflight(project_id, request)
    if json_output:
        print_learning_result(result, True)
    else:
        console.print(
            f"ready={result.ready} · checked_tasks={result.checked_tasks} · "
            f"issues={result.issue_count} · selected_camera_role={result.selected_camera_role or '-'}"
        )
        for issue in result.issues:
            console.print(
                f"{issue.code}: {issue.message} · task={issue.task_display_id or issue.task_id or '-'}"
            )


@app.command("orphan-tasks-preview")
def orphan_tasks_preview(
    project_id: str = typer.Argument(..., help="项目 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览无源任务及其标注数量。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.preview_orphan_tasks(project_id)
    if json_output:
        print_learning_result(result, True)
    else:
        console.print(
            f"orphan_tasks={result.orphan_tasks} · orphan_annotations={result.orphan_annotations}"
        )


@app.command("cleanup-orphan-tasks")
def cleanup_orphan_tasks(
    project_id: str = typer.Argument(..., help="项目 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除无源任务及关联数据"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """删除无源任务以及关联的标注、锁、评论、草稿和报告引用。"""
    confirm_destructive(
        f"确认清理项目 {project_id} 的无源任务及关联数据?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.cleanup_orphan_tasks(project_id)
    print_learning_result(result, json_output)


@app.command("transfer")
def transfer(
    project_id: str = typer.Argument(..., help="项目 ID"),
    new_owner_id: str = typer.Option(..., "--new-owner-id", help="新负责人用户 ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认转移项目所有权"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """将项目转给活跃 project_admin；此 API 仅允许 super_admin 调用。"""
    try:
        request = ProjectTransferRequest(new_owner_id=new_owner_id)
    except ValidationError as exc:
        raise typer.BadParameter("--new-owner-id 必须是 UUID") from exc
    confirm_destructive(
        f"确认将项目 {project_id} 转给 {request.new_owner_id}?", yes, json_output
    )
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.projects.transfer(project_id, request)
    print_learning_result(result, json_output)
