"""aap point-cloud 子命令。"""

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
from ai_annotation.point_cloud_models import (
    CameraAnnotationMemberCreate,
    CameraAnnotationMemberDelete,
    CameraAnnotationMemberRestore,
    CameraAnnotationMemberUpdate,
    SensorCalibrationUpdate,
)

app = typer.Typer(
    help="查看点云任务相机 manifest、成员标注与传感器标定。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("manifest")
def manifest(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    json_output: bool = typer.Option(
        False, "--json", help="输出包含短期签名 URL 的裸 JSON"
    ),
) -> None:
    """读取点云/相机预签名 URL 和标定快照；URL 按 expires_in 秒过期。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            result = client.point_cloud.manifest(task_id)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    console.print(
        f"task={result.task_id} · 点云格式={result.point_cloud_format} · "
        f"链接有效 {result.expires_in}s · axis={result.axis_convention or '-'}"
    )
    table = Table()
    table.add_column("相机")
    table.add_column("角色")
    table.add_column("尺寸")
    table.add_column("标定版本")
    table.add_column("标定 digest")
    for camera in result.cameras:
        dimensions = (
            f"{camera.width}×{camera.height}"
            if camera.width is not None and camera.height is not None
            else "-"
        )
        table.add_row(
            camera.name,
            camera.role,
            dimensions,
            str(camera.calibration_revision or "-"),
            camera.calibration_digest or "-",
        )
    console.print(table)
    console.print("签名 URL 仅在 --json 输出；不要缓存过期链接。")


@app.command("camera-members")
def camera_members(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    scene_track_id: str | None = typer.Option(
        None, "--scene-track-id", help="筛选 SceneTrack"
    ),
    projection_camera_role: str | None = typer.Option(
        None, "--projection-camera-role", help="为指定 SceneTrack 投影到此相机"
    ),
    include_inactive: bool = typer.Option(
        False, "--include-inactive", help="包含软删除成员"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """列出当前任务可见的相机成员及其标定关联状态。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            page = client.point_cloud.camera_members(
                task_id,
                scene_track_id=scene_track_id,
                projection_camera_role=projection_camera_role,
                include_inactive=include_inactive,
            )
    data = page.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    table = Table()
    table.add_column("Member")
    table.add_column("Track")
    table.add_column("类别 / 相机")
    table.add_column("BBox (xywh)")
    table.add_column("可见性")
    table.add_column("状态")
    table.add_column("版次: member/track/cal")
    for item in page.items:
        bbox = item.bbox
        table.add_row(
            str(item.id),
            str(item.scene_track_id),
            f"{item.class_name} / {item.camera_role}",
            f"{bbox.x:g},{bbox.y:g},{bbox.w:g},{bbox.h:g}",
            item.visibility,
            f"{'active' if item.is_active else 'inactive'} / calibration {item.relation_status}",
            f"{item.version}/{item.track_revision}/{item.current_calibration_revision}",
        )
    console.print(table)
    console.print(f"track_revision={page.track_revision or '-'}")
    if page.projected_bbox is not None:
        bbox = page.projected_bbox
        console.print(f"projected_bbox={bbox.x:g},{bbox.y:g},{bbox.w:g},{bbox.h:g}")


@app.command("calibration-history")
def calibration_history(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    camera_role: str = typer.Argument(..., help="相机角色，例如 camera_front"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """查看当前标定 revision/digest 与历史快照；旧元数据可能是虚拟 revision 1。"""
    with cli_errors(json_output):
        with get_client(json_output) as client:
            history = client.point_cloud.calibration_history(task_id, camera_role)
    data = history.model_dump(mode="json")
    if json_output:
        print_json(data)
        return
    console.print(
        f"当前 revision={history.current_revision} · digest={history.current_digest}"
    )
    table = Table()
    table.add_column("Revision")
    table.add_column("Digest")
    table.add_column("创建时间")
    table.add_column("rect")
    for revision in history.items:
        table.add_row(
            str(revision.revision),
            revision.digest,
            revision.created_at.isoformat()
            if revision.created_at
            else "metadata (virtual)",
            "yes" if revision.calibration.rect is not None else "no",
        )
    console.print(table)


@app.command("member-create")
def member_create(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    body_file: Path = typer.Argument(
        ..., help="CameraAnnotationMemberCreate JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认创建相机标注"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """把同一 SceneTrack 的 active box_3d 投影成员写入指定相机。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, CameraAnnotationMemberCreate)
        confirm_destructive(
            f"确认在任务 {task_id} 为 track revision {body.expected_track_revision} "
            f"创建 {body.camera_role} 成员?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud.create_camera_member(task_id, body)
    _print_member_result(result, json_output, "created")


@app.command("member-update")
def member_update(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    member_id: str = typer.Argument(..., help="相机成员 Annotation ID"),
    body_file: Path = typer.Argument(
        ..., help="CameraAnnotationMemberUpdate JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新相机标注"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """用 annotation version、track revision 和 calibration digest 写入更新。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, CameraAnnotationMemberUpdate)
        confirm_destructive(
            f"确认更新相机成员 {member_id} (member v{body.expected_version}, "
            f"track r{body.expected_track_revision})?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud.update_camera_member(task_id, member_id, body)
    _print_member_result(result, json_output, "updated")


@app.command("member-delete")
def member_delete(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    member_id: str = typer.Argument(..., help="相机成员 Annotation ID"),
    body_file: Path = typer.Argument(
        ..., help="CameraAnnotationMemberDelete JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认软删除相机成员"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软删除相机成员；同时递增 SceneTrack revision。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, CameraAnnotationMemberDelete)
        confirm_destructive(
            f"确认软删除成员 {member_id} (member v{body.expected_version}, "
            f"track r{body.expected_track_revision})?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud.delete_camera_member(task_id, member_id, body)
    _print_member_result(result, json_output, "deleted")


@app.command("member-restore")
def member_restore(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    member_id: str = typer.Argument(..., help="相机成员 Annotation ID"),
    body_file: Path = typer.Argument(
        ..., help="CameraAnnotationMemberRestore JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认恢复相机成员"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """恢复软删除成员；服务端会重新检查 track、标定 revision/digest 和重复项。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, CameraAnnotationMemberRestore)
        confirm_destructive(
            f"确认恢复成员 {member_id} (member v{body.expected_version}, "
            f"track r{body.expected_track_revision}, calibration r{body.expected_calibration_revision})?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud.restore_camera_member(task_id, member_id, body)
    _print_member_result(result, json_output, "restored")


@app.command("calibration-update")
def calibration_update(
    task_id: str = typer.Argument(..., help="点云任务 ID"),
    camera_role: str = typer.Argument(..., help="相机角色，例如 camera_front"),
    body_file: Path = typer.Argument(
        ..., help="SensorCalibrationUpdate JSON 文件，或 - 从 stdin 读取"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认更新共享传感器标定"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """更新 DatasetItem 标定；共享数据集跨项目时服务端只允许 super_admin。"""
    with cli_errors(json_output):
        body = read_json_body(body_file, SensorCalibrationUpdate)
        confirm_destructive(
            f"确认更新 {camera_role} 标定 (expected r{body.expected_revision}, "
            f"digest={body.expected_digest[:12]}…)?",
            yes,
            json_output,
        )
        with get_client(json_output) as client:
            result = client.point_cloud.update_calibration(task_id, camera_role, body)
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"[green]标定回执[/green] dataset_item={result.dataset_item_id} "
            f"revision={result.revision} digest={result.digest}"
        )


def _print_member_result(result, json_output: bool, action: str) -> None:
    data = result.model_dump(mode="json")
    if json_output:
        print_json(data)
    else:
        console.print(
            f"[green]{action}[/green] member={result.id} active={result.is_active} "
            f"member_v={result.version} track_r={result.track_revision} "
            f"calibration={result.relation_status}"
        )
