"""Schema-driven Data Manager reads and explicit selected-task actions."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)
from ai_annotation.data_manager_models import (
    DataManagerEntityQueryRequest,
    DataManagerMatchesRequest,
    DataManagerSummaryRequest,
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
    DataManagerTaskExportRequest,
)

app = typer.Typer(
    help="按项目 schema 查询任务对象、轨迹及处理选中任务。", no_args_is_help=True
)


@app.command("schema")
def schema(
    project_id: str = typer.Option(..., "--project", help="项目 UUID"),
    entity_scope: str = typer.Option(
        "tasks", "--entity-scope", help="tasks、objects 或 tracks"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """读取当前项目和粒度的字段、操作符、列与排序目录。"""
    if entity_scope not in {"tasks", "objects", "tracks"}:
        raise typer.BadParameter("entity-scope must be tasks, objects or tracks")
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.data_manager.schema(project_id, entity_scope=entity_scope)
    print_learning_result(result, json_output)


@app.command("summary")
def summary(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(..., "--body", help="JSON 文件，或 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取完整匹配任务范围的聚合，不按页面抽样。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerSummaryRequest)
        with get_client(json_output) as client:
            result = client.data_manager.summary(project_id, body)
    print_learning_result(result, json_output)


@app.command("matches")
def matches(
    task_id: str = typer.Argument(..., help="当前项目的任务 UUID"),
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(..., "--body", help="JSON 文件，或 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取一个任务中命中的对象／候选摘要。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerMatchesRequest)
        with get_client(json_output) as client:
            result = client.data_manager.matches(project_id, task_id, body)
    print_learning_result(result, json_output)


@app.command("query-objects")
def query_objects(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(..., "--body", help="JSON 文件，或 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取一页匹配对象，保留服务端返回的游标和 facet。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerEntityQueryRequest)
        with get_client(json_output) as client:
            result = client.data_manager.query_objects(project_id, body)
    print_learning_result(result, json_output)


@app.command("object-detail")
def object_detail(
    annotation_id: str = typer.Argument(..., help="对象 annotation UUID"),
    project_id: str = typer.Option(..., "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取可见对象的来源、属性与定位信息。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.data_manager.object_detail(project_id, annotation_id)
    print_learning_result(result, json_output)


@app.command("object-location")
def object_location(
    annotation_id: str = typer.Argument(..., help="对象 annotation UUID"),
    project_id: str = typer.Option(..., "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取对象工作台定位，不下载几何或媒体。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.data_manager.object_location(project_id, annotation_id)
    print_learning_result(result, json_output)


@app.command("query-tracks")
def query_tracks(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(..., "--body", help="JSON 文件，或 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取一页逻辑轨迹，不将 track_id 当作 track_ref。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerEntityQueryRequest)
        with get_client(json_output) as client:
            result = client.data_manager.query_tracks(project_id, body)
    print_learning_result(result, json_output)


@app.command("track-detail")
def track_detail(
    track_ref: str = typer.Argument(..., help="服务端返回的不透明 track_ref"),
    project_id: str = typer.Option(..., "--project"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """读取轨迹及全部可见成员；当前接口没有成员分页。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.data_manager.track_detail(project_id, track_ref)
    print_learning_result(result, json_output)


@app.command("assignment-preview")
def assignment_preview(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(..., "--body", help="JSON 文件，或 - 从 stdin 读取"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """预览选中任务的人员变更与版本，不应用分配。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerTaskAssignmentRequest)
        with get_client(json_output) as client:
            result = client.data_manager.assignment_preview(project_id, body)
    print_learning_result(result, json_output)


@app.command("assignment-apply")
def assignment_apply(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(
        ..., "--body", help="含预览版本的 JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认应用分配"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """带回原选择、目标与 preview_version，按服务端资格应用。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerTaskAssignmentApplyRequest)
        confirm_destructive("确认按此预览应用选中任务分配?", yes, json_output)
        with get_client(json_output) as client:
            result = client.data_manager.assignment_apply(project_id, body)
    print_learning_result(result, json_output)


@app.command("export-tasks")
def export_tasks(
    project_id: str = typer.Option(..., "--project"),
    body_path: Path = typer.Option(
        ..., "--body", help="明确任务选择的 JSON 文件，或 - 从 stdin 读取"
    ),
    idempotency_key: str | None = typer.Option(
        None, "--idempotency-key", help="调用方保存的稳定幂等键"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建导出作业"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """仅导出选中任务，返回 Job 受理信息，不隐式等待／下载。"""
    with cli_errors(json_output):
        body = read_json_body(body_path, DataManagerTaskExportRequest)
        confirm_destructive("确认导出这些明确选中的任务?", yes, json_output)
        with get_client(json_output) as client:
            result = client.data_manager.export_tasks(
                project_id, body, idempotency_key=idempotency_key
            )
    print_learning_result(result, json_output)
