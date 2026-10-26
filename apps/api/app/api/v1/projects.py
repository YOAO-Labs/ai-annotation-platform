from aap_task_contract import normalize_task
from aap_inference.capability_registry import INPUT_BBOX_PROMPT, INPUT_CROP
import aap_inference.preannotate as preannotate_schemas
from aap_workspace.configuration_requests import ProjectCreate, ProjectUpdate
from aap_workspace_contract.project_query_contract import (
    ProjectConfigurationEvidence,
)
import logging
import uuid
from app.application.preannotation_contract import (
    BatchPredictionDispatcher,
    BatchPredictionRequest,
)
from app.application.project_workspace import (
    transfer_project_owner as transfer_project_owner_workflow,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from aap_assets_catalog.associations_contract import (
    ListProjectDatasetsRequest,
    ProjectDatasetSummary,
)
from app.deps import (
    get_db,
    get_current_user,
    get_current_user_id,
    require_roles,
    require_project_visible,
    require_project_owner,
    require_project_capability,
    get_batch_prediction_dispatcher,
)
from app.db.enums import (
    TaskStatus,
    UserRole,
    WORKBENCH_AI_EDITABLE_TASK_STATUSES,
)
from aap_workspace_contract.access import ProjectCapability
from app.services.workspace_membership import (
    add_member as add_project_member,
    change_role as change_project_member_role,
    preview_role_change as preview_project_member_role_change,
    remove_member as remove_project_member,
)
from aap_workspace.contract import MembershipRecord
from app.services.user_brief import resolve_briefs
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.async_job import AsyncJob
from app.services.jobs_lifecycle_data import job_lock_statement
from app.schemas.project import (
    ProjectOut,
    ProjectPage,
    ProjectStats,
    ProjectClassUsageOut,
    ProjectCleanupOrphansRequest,
    ProjectCleanupOrphansOut,
    ProjectMemberOut,
    ProjectMemberCreate,
    ProjectMemberRolePreviewRequest,
    ProjectMemberRolePreviewOut,
    ProjectMemberRoleChangeRequest,
    ProjectAccessOut,
    ProjectTransferRequest,
    ProjectReadinessSummary,
    MentionCandidateOut,
)
from app.schemas.project_pipeline import ProjectPipelineApplyRequest, ProjectPipelineOut
from app.schemas.export import (
    ExportRequestBody,
    LidarExportPreflightRequest,
    LidarExportPreflightResponse,
)
from app.config import settings
from app.services.pipeline_validation import (
    check_capability_violations,
    check_parent_geometry_roi,
    resolve_preannotate_queue,
)
from app.services.batch_permissions import allows_bulk_preannotation
from app.services.accepted_inputs import InputAdmission, get_input_admission
from app.services.accepted_input_manifests import (
    InputManifestCaptureFactory,
    get_input_manifest_capture,
)
from app.services.accepted_video_inputs import (
    VideoInputManifestCaptureFactory,
    get_video_input_manifest_capture,
)
from app.services.assets_catalog import AssetsCatalogGateway
from app.application.project_deletion import (
    delete_project as delete_project_transaction,
)
from app.application.project_task_cleanup import (
    cleanup_annotation_orphans as cleanup_project_annotation_orphans,
    delete_source_less_tasks as cleanup_source_less_tasks,
)
from app.services.inference_pipelines import (
    commit_pipeline_command,
    execute_pipeline_command,
    get_pipeline_projection,
)
from app.services.workspace_configuration import execute_configuration
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_project_query import execute_project_query
from aap_workspace_contract.project_query_values import materialize_project_query_data
from app.services.workspace_directory_query import execute_directory_query

from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    get_core_row,
    task_row,
)

router = APIRouter()
logger = logging.getLogger("app.api.projects")

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
_VIDEO_INPUT_IMAGE_MARKERS = {
    "accepted_inputs_protocol",
    "accepted_input_ids",
    "accepted_input_manifest_protocol",
    "accepted_input_manifest_id",
    "accepted_input_manifest_digest",
}


def _preannotation_execution_unit(body: preannotate_schemas.PreannotateRequest):
    if not body.pipeline_stages:
        return None
    source = next(
        (stage.source for stage in body.pipeline_stages if stage.parent_stage is None),
        None,
    )
    return (source or {}).get("execution_unit")


def _frame_pipeline_supported(execution_unit, pipeline_stages) -> bool:
    return execution_unit != "frame" or len(pipeline_stages or []) <= 1


def _build_video_input_selection(body, task_ids, *, max_frames: int):
    from aap_assets_video_inputs import authority, values

    return authority.VideoSelection(
        batch_id=body.batch_id,
        task_ids=task_ids,
        predict_mode=body.predict_mode,
        execution_scope=body.execution_scope,
        sampling=values.FrameSampling(
            max_frames=max_frames,
            step=1,
            width=1280,
            format="jpeg",
        ),
    )


def _restore_video_input_selection(body, task_ids, payload):
    from aap_assets_video_inputs import authority, execution

    raw_selection = (
        payload.get("accepted_video_selection") if isinstance(payload, dict) else None
    )
    if not isinstance(raw_selection, dict):
        raise execution.VideoInputFailure("manifest_scope_mismatch")
    try:
        selection = authority.VideoSelection.model_validate(raw_selection)
    except (ValueError, TypeError):
        raise execution.VideoInputFailure("manifest_scope_mismatch") from None
    expected_task_ids = (
        tuple(sorted(set(task_ids), key=str)) if task_ids is not None else None
    )
    if (
        selection.model_dump(mode="json") != raw_selection
        or selection.batch_id != body.batch_id
        or selection.task_ids != expected_task_ids
        or selection.predict_mode != body.predict_mode
        or selection.execution_scope != body.execution_scope
    ):
        raise execution.VideoInputFailure("manifest_scope_mismatch")
    return selection


def _video_manifest_payload_matches(payload, job_id, selection) -> bool:
    from aap_assets_video_inputs import values

    task_ids = (
        [str(task_id) for task_id in selection.task_ids]
        if selection.task_ids is not None
        else None
    )
    digest = (
        payload.get("accepted_video_selection_digest")
        if isinstance(payload, dict)
        else None
    )
    if (
        not isinstance(payload, dict)
        or payload.get("accepted_video_inputs_protocol") != values.VIDEO_INPUT_SCHEMA
        or payload.get("accepted_video_manifest_id") != str(job_id)
        or payload.get("accepted_video_selection") != selection.model_dump(mode="json")
        or payload.get("execution_unit") != "frame"
        or payload.get("execution_scope") != selection.execution_scope
        or "batch_id" not in payload
        or payload.get("batch_id")
        != (str(selection.batch_id) if selection.batch_id else None)
        or any(key in payload for key in _VIDEO_INPUT_IMAGE_MARKERS)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return False
    if task_ids is None:
        return "task_ids" not in payload and "scope" not in payload
    return payload.get("task_ids") == task_ids and payload.get("scope") == {
        "task_ids": task_ids
    }


async def _capture_video_input_manifest(
    db,
    job,
    *,
    actor_id,
    project_id,
    selection,
    capture_factory: VideoInputManifestCaptureFactory,
    replay: bool,
):
    from aap_assets_video_inputs import execution as video_contract
    from aap_assets_video_inputs import values

    payload = job.payload or {}
    selection_payload = selection.model_dump(mode="json")
    if replay:
        if not _video_manifest_payload_matches(payload, job.id, selection):
            raise video_contract.VideoInputFailure("manifest_scope_mismatch")
    else:
        if any(key in payload for key in _VIDEO_INPUT_IMAGE_MARKERS):
            raise video_contract.VideoInputFailure("manifest_scope_mismatch")
        payload = {
            **payload,
            "execution_unit": "frame",
            "accepted_video_inputs_protocol": values.VIDEO_INPUT_SCHEMA,
            "accepted_video_manifest_id": str(job.id),
            "accepted_video_selection": selection_payload,
        }
        job.payload = payload

    capture_request = video_contract.CaptureVideoManifestRequest(
        job_id=job.id,
        actor_id=actor_id,
        project_id=project_id,
        selection=selection,
    )
    header = await capture_factory(db).capture(capture_request)
    video_contract.require_scope(
        "capture_accepted_video_manifest", capture_request, header
    )
    if replay and header.selection_digest != payload["accepted_video_selection_digest"]:
        raise video_contract.VideoInputFailure("manifest_scope_mismatch")
    job.payload = {
        **(job.payload or {}),
        "accepted_video_selection_digest": header.selection_digest,
        "total_tasks": header.selected_count,
        "video_input_candidate_count": header.candidate_count,
    }
    return header


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    status: str | None = None,
    search: str | None = None,
    type_key: list[str] | None = Query(None),
    data_type: list[str] | None = Query(None),
    member_id: uuid.UUID | None = None,
    project_role: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_project_query(
        db,
        operation="list_projects",
        actor=user,
        filters={
            "status": status,
            "search": search,
            "type_key": type_key,
            "data_type": data_type,
            "member_id": member_id,
            "project_role": project_role,
            "created_from": created_from,
            "created_to": created_to,
        },
    )


@router.get("/query", response_model=ProjectPage)
async def query_projects(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    search: str | None = None,
    type_key: list[str] | None = Query(None),
    data_type: list[str] | None = Query(None),
    member_id: uuid.UUID | None = None,
    project_role: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_project_query(
        db,
        operation="query_projects",
        actor=user,
        filters={
            "status": status,
            "search": search,
            "type_key": type_key,
            "data_type": data_type,
            "member_id": member_id,
            "project_role": project_role,
            "created_from": created_from,
            "created_to": created_to,
        },
        pagination={"page": str(page), "page_size": page_size},
    )


@router.get("/stats", response_model=ProjectStats)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_project_query(
        db,
        operation="project_stats",
        actor=user,
    )


@router.post("", response_model=ProjectOut)
async def create_project(
    data: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(*_MANAGERS)),
):
    result = await execute_configuration(
        db,
        operation="create_project",
        payload=data.model_dump(exclude_unset=True),
        actor_id=current_user.id,
    )
    await db.commit()
    return await execute_project_query(
        db,
        operation="get_project_projection",
        actor=current_user,
        target_id=result.resource_id,
        projection_evidence=ProjectConfigurationEvidence(
            source="configuration",
            operation_id=result.operation_id,
            operation=result.operation,
        ),
    )


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    return await execute_project_query(
        db,
        operation="get_project",
        actor=user,
        target_id=project_id,
    )


@router.get("/{project_id}/readiness", response_model=ProjectReadinessSummary)
async def get_project_readiness(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    """Return the owner-facing, server-observed project start conditions.

    This endpoint intentionally reports task-creation jobs and active member
    accounts together with counters.  The settings page can therefore explain
    a pending or failed worker without treating a partially created project as
    ready.
    """
    return await execute_project_query(
        db,
        operation="project_readiness",
        actor=user,
        target_id=project_id,
    )


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    data: ProjectUpdate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    result = await execute_configuration(
        db,
        operation="update_project",
        target_id=project.id,
        payload=data.model_dump(exclude_unset=True),
        actor_id=current_user.id,
    )
    await db.commit()
    projection = await execute_project_query(
        db,
        operation="get_project_projection",
        actor=current_user,
        target_id=project.id,
        projection_evidence=ProjectConfigurationEvidence(
            source="configuration",
            operation_id=result.operation_id,
            operation=result.operation,
        ),
    )
    projection["capability_warnings"] = await _compute_pipeline_capability_warnings(
        db, projection["preannotate_pipeline"]
    )
    return projection


@router.post(
    "/{project_id}/pipelines/apply",
    response_model=ProjectPipelineOut,
    status_code=201,
)
async def apply_project_pipeline(
    body: ProjectPipelineApplyRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    mutation = await execute_pipeline_command(
        db,
        operation="apply_pipeline",
        actor=current_user,
        target_id=project.id,
        payload={
            "pipeline_id": body.pipeline_id,
            "set_default": body.set_default,
        },
    )
    await commit_pipeline_command(db, mutation)
    return await get_pipeline_projection(db, mutation, actor=current_user)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    from ipaddress import ip_address
    from app.middleware.request_id import request_id_var
    from app.services.audit import audit_origin_from_request
    from aap_audit_contract import ProjectDeletionAuditContext

    origin = audit_origin_from_request(request)
    try:
        ip = str(ip_address(origin.ip)) if origin.ip else None
    except ValueError:
        ip = None
    await delete_project_transaction(
        db,
        project.id,
        actor_id=actor_id,
        audit_context=ProjectDeletionAuditContext(
            method=origin.method,
            path=origin.path,
            ip=ip,
            request_id=request_id_var.get()[:36] or None,
        ),
    )
    await db.commit()
    return Response(status_code=204)


class ClassRenameRequest(BaseModel):
    old_name: str
    new_name: str
    # Omission targets all stored bindings, including disabled tool units.
    tool_unit_id: str | None = None


@router.post("/{project_id}/classes/rename", response_model=ProjectOut)
async def rename_class(
    body: ClassRenameRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """B-13 · 原子地把类别 old_name 重命名为 new_name:
    - 更新 tool_bindings 中对应 unit (或所有 unit) 的 classes[].name (强隔离 · 仅本 unit)
    - **始终跨 unit** 改全项目内同名 annotations.class_name (避免历史 magic-box /
      region / 旧 schema 残留留下"孤儿"标注: binding 已无该类, 但 annotation 仍引用)
    - 不动 predictions.result (alias 不变)
    """
    old_name = body.old_name.strip()
    new_name = body.new_name.strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    result = await execute_configuration(
        db,
        operation="rename_project_class",
        actor_id=current_user.id,
        target_id=project.id,
        payload={
            "old_name": old_name,
            "new_name": new_name,
            "tool_unit_id": body.tool_unit_id,
        },
    )
    rename_result = result.rename_result
    if rename_result is None:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用")
    if rename_result.changed:
        await db.commit()
    elif rename_result.projection is not None:
        return materialize_project_query_data(rename_result.projection)
    return await execute_project_query(
        db,
        operation="get_project_projection",
        actor=current_user,
        target_id=project.id,
        projection_evidence=rename_result.evidence,
    )


@router.get("/{project_id}/class-usage", response_model=ProjectClassUsageOut)
async def get_class_usage(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    from app.services.labeling_class_query import execute_labeling_class_query

    await execute_project_query(
        db,
        operation="require_project_manager",
        actor=current_user,
        target_id=project_id,
    )
    return await execute_labeling_class_query(db, project_id=project_id)


@router.post("/{project_id}/cleanup-orphans", response_model=ProjectCleanupOrphansOut)
async def cleanup_annotation_orphans(
    request: Request,
    body: ProjectCleanupOrphansRequest | None = None,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    dry_run = True if body is None else body.dry_run
    result = await cleanup_project_annotation_orphans(
        db,
        project=project,
        actor_id=current_user.id,
        dry_run=dry_run,
    )

    if not dry_run:
        from app.services.audit import AuditService

        await AuditService.log(
            db,
            actor=current_user,
            action="project.cleanup_annotation_orphans",
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail=result,
        )
        await db.commit()

    return ProjectCleanupOrphansOut(**result)


@router.post("/{project_id}/transfer", response_model=ProjectOut)
async def transfer_owner(
    body: ProjectTransferRequest,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    evidence = await transfer_project_owner_workflow(
        db,
        actor=current_user,
        project_id=project_id,
        new_owner_id=body.new_owner_id,
    )
    await db.commit()
    return await execute_project_query(
        db,
        operation="get_project_projection",
        actor=current_user,
        target_id=project_id,
        projection_evidence=evidence,
    )


@router.get("/{project_id}/access", response_model=ProjectAccessOut)
async def get_project_access(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """Resolved project access for the current account.

    Managers may have null membership fields; ordinary members expose their
    membership identity/version and fixed capability set.
    """

    return await execute_project_query(
        db, operation="get_project_access", actor=current_user, target_id=project_id
    )


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_members(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    return await execute_directory_query(
        db, operation="list_members", actor_id=actor_id, target_id=project_id
    )


@router.get(
    "/{project_id}/mention-candidates", response_model=list[MentionCandidateOut]
)
async def list_mention_candidates(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """讨论区 @ 提及候选：项目负责人、启用的平台超管、项目成员，按 user_id 去重。

    顺序固定为 负责人 → 超管 → 成员，前端据此展示「项目负责人 / 超级管理员」标签。
    """
    return await execute_directory_query(
        db, operation="mention_candidates", actor_id=actor_id, target_id=project_id
    )


def _member_value(member: ProjectMember | MembershipRecord, target) -> ProjectMemberOut:
    return ProjectMemberOut(
        id=member.id,
        user_id=member.user_id,
        user_name=target.name if target else "",
        user_email=target.email if target else "",
        role=member.role,
        platform_role=target.role if target else None,
        version=member.version,
        assigned_at=member.assigned_at,
        updated_at=member.updated_at,
        avatar_ref=target.avatar_ref if target else None,
    )


async def _member_out(
    db: AsyncSession, member: ProjectMember | MembershipRecord
) -> ProjectMemberOut:
    briefs = await resolve_briefs(db, [member.user_id])
    return _member_value(member, briefs.get(str(member.user_id)))


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def add_member(
    body: ProjectMemberCreate,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    member = await add_project_member(
        db,
        project_id=project.id,
        actor_id=current_user.id,
        target_user_id=body.user_id,
        project_role=body.role,
    )
    return await _member_out(db, member)


@router.post(
    "/{project_id}/members/{member_id}/role/preview",
    response_model=ProjectMemberRolePreviewOut,
)
async def preview_member_role_change(
    member_id: uuid.UUID,
    body: ProjectMemberRolePreviewRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    """Read-only role-change preview: blockers, resource snapshot and token."""

    result = await preview_project_member_role_change(
        db,
        project_id=project.id,
        actor_id=current_user.id,
        member_id=member_id,
        target_role=body.project_role,
        replacement_annotator_id=body.replacement_annotator_id,
        replacement_reviewer_id=body.replacement_reviewer_id,
    )
    return ProjectMemberRolePreviewOut(**result)


@router.patch(
    "/{project_id}/members/{member_id}/role",
    response_model=ProjectMemberOut,
)
async def change_member_role(
    member_id: uuid.UUID,
    body: ProjectMemberRoleChangeRequest,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    """Role change with expected version, preview token and explicit handoff."""

    member = await change_project_member_role(
        db,
        project_id=project.id,
        actor_id=current_user.id,
        member_id=member_id,
        target_role=body.project_role,
        expected_version=body.expected_version,
        preview_token=body.preview_token,
        reason=body.reason,
        replacement_annotator_id=body.replacement_annotator_id,
        replacement_reviewer_id=body.replacement_reviewer_id,
    )
    return await _member_out(db, member)


@router.delete("/{project_id}/members/{member_id}", status_code=204)
async def remove_member(
    member_id: uuid.UUID,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    _access=Depends(require_project_capability(ProjectCapability.MEMBER_MANAGE.value)),
):
    await remove_project_member(
        db, project_id=project.id, actor_id=current_user.id, member_id=member_id
    )
    return Response(status_code=204)


def _validate_export_targets(
    targets: list[str], data_type: str | None = None
) -> list[str]:
    """v0.10.43 · 校验并去重导出目标，非法转 400。v0.10.47 · 按 data_type 过滤模态。"""
    from app.services.exporting.packaging import clean_export_targets

    try:
        return clean_export_targets(targets, data_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{project_id}/exports/lidar:preflight",
    response_model=LidarExportPreflightResponse,
)
async def preflight_project_lidar_export(
    body: LidarExportPreflightRequest,
    project: Project = Depends(require_project_visible),
    db: AsyncSession = Depends(get_db),
    _access=Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
) -> LidarExportPreflightResponse:
    if project.data_type != "lidar":
        raise HTTPException(status_code=422, detail="project is not a lidar project")
    targets = _validate_export_targets(body.targets, project.data_type)
    from app.services.exporting.lidar_preflight import preflight_lidar_export

    return await preflight_lidar_export(
        db,
        project_id=project.id,
        batch_id=None,
        targets=targets,
        options=body.lidar,
    )


@router.post("/{project_id}/export", status_code=202)
async def export_project(
    request: Request,
    body: ExportRequestBody | None = None,
    targets: list[str] = Query(
        default=["coco"],
        description="导出目标，可多选：coco / yolo-det / yolo-obb / yolo-seg"
        " / label-studio-brush / binary-png / indexed-png / aap_json"
        " / video_json / yolo-frames-det / yolo-frames-seg / coco-frames-seg / davis"
        " / youtube-vos / mots / mot / kitti"
        " / coco-multicamera / nuscenes / pointmask"
        "（voc 仅可单选，走同步下载；lidar.kitti 为 3D label，video.kitti 为 tracking label）",
    ),
    include_attributes: bool = Query(
        True,
        description="是否在导出包中携带 annotation.attributes 与 project.attribute_schema",
    ),
    video_frame_mode: str = Query(
        "keyframes",
        pattern="^(keyframes|all_frames)$",
        description="video-track 导出帧模式：keyframes 仅关键帧，all_frames 展开逐帧插值；图片项目忽略",
    ),
    axis_frame: str = Query(
        "iso",
        pattern="^(iso|source)$",
        description="3D box export axis frame: iso keeps platform-normalized PSR; source maps back to dataset axis convention",
    ),
    indexed_overlap_policy: str = Query(
        "error",
        pattern="^(error|z_order|larger_area|smaller_area)$",
        description="Indexed PNG 的实例重叠策略；默认拒绝重叠",
    ),
    video_overlap_policy: str = Query(
        "error",
        pattern="^(error|z_order|larger_area|smaller_area)$",
        description="DAVIS / YouTube-VOS 的实例重叠策略；默认拒绝重叠",
    ),
    mots_frame_base: int = Query(
        0,
        ge=0,
        le=1,
        description="MOTS 输出帧号基准：0 或 1",
    ),
    project: Project = Depends(require_project_visible),
    actor: IdentityActorFacts = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _access=Depends(
        require_project_capability(ProjectCapability.EXPORT_ANNOTATIONS.value)
    ),
):
    # v0.10.27 · 导出异步化：创建 async_job(kind=export) + 派发 run_export，返回 {job_id}。
    # v0.10.43 · 多目标（方案 B）：一个 job 产一个 zip（>1 目标分子目录）。
    # 状态/下载走 GET /async-jobs/{id}（result.download_url 为 7d 预签名）。
    # VOC 后端保留同步 blob（前端已隐藏），仅可单选；不删避免破坏 API 契约。
    from app.services.audit import AuditService, AuditAction, export_detail

    targets = _validate_export_targets(targets, project.data_type)

    lidar_options = body.lidar if body else None
    lidar_options_payload = (
        lidar_options.model_dump(mode="json", exclude_none=True)
        if lidar_options
        else None
    )
    if project.data_type == "lidar":
        from app.services.exporting.lidar_preflight import (
            LidarExportPreflightFailed,
            assert_lidar_export_ready,
        )

        try:
            await assert_lidar_export_ready(
                db,
                project_id=project.id,
                batch_id=None,
                targets=targets,
                options=lidar_options,
            )
        except LidarExportPreflightFailed as exc:
            raise HTTPException(
                status_code=409,
                detail=exc.report.model_dump(mode="json"),
            ) from exc

    from app.services.exporting.video_scope import normalize_video_export_scope

    try:
        video_scope = await normalize_video_export_scope(
            db,
            actor_id=actor.id,
            project=project,
            request=body.scope if body else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    video_scope_payload = video_scope.as_dict() if video_scope else None
    if project.data_type == "video":
        from app.services.video_canonical import (
            VideoBoundaryUnreconciledError,
            assert_export_boundaries_reconciled,
        )

        try:
            await assert_export_boundaries_reconciled(
                db,
                project=project,
                batch_id=None,
                scope=video_scope,
            )
        except VideoBoundaryUnreconciledError as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    if "voc" in targets:
        if targets != ["voc"]:
            raise HTTPException(
                status_code=400, detail="voc 格式只能单独导出，不能与其它目标混选"
            )
        from app.services.exporting.service import ExportService, UnsupportedExportError

        svc = ExportService(db)
        try:
            data = await svc.export_voc(
                project.id, include_attributes=include_attributes
            )
        except UnsupportedExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await AuditService.log(
            db,
            actor=actor,
            action=AuditAction.PROJECT_EXPORT,
            target_type="project",
            target_id=str(project.id),
            request=request,
            status_code=200,
            detail={"format": "voc", "project_display_id": project.display_id},
        )
        await db.commit()
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename={project.display_id}_voc.zip"
            },
        )

    from app.services import async_job as async_job_svc
    from app.db.models.async_job import AsyncJobKind
    from app.services.jobs_queue import dispatch_job
    from app.services.jobs_dispatch import (
        prepare_job_dispatch,
        publish_prepared_dispatch,
    )

    job = await async_job_svc.create_job(
        db,
        kind=AsyncJobKind.EXPORT.value,
        user_id=actor.id,
        project_id=project.id,
        payload={
            "targets": targets,
            "format": ",".join(targets),
            "include_attributes": include_attributes,
            "video_frame_mode": video_frame_mode,
            "axis_frame": axis_frame,
            "indexed_overlap_policy": indexed_overlap_policy,
            "video_overlap_policy": video_overlap_policy,
            "mots_frame_base": mots_frame_base,
            "project_display_id": project.display_id,
            **(
                {"video_export_scope": video_scope_payload}
                if video_scope_payload
                else {}
            ),
            **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
        },
    )
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.PROJECT_EXPORT,
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=202,
        detail=export_detail(
            actor=actor,
            request=request,
            base={"targets": targets, "project_display_id": project.display_id},
            filter_criteria={
                "include_attributes": include_attributes,
                "video_frame_mode": video_frame_mode,
                "axis_frame": axis_frame,
                "indexed_overlap_policy": indexed_overlap_policy,
                "video_overlap_policy": video_overlap_policy,
                "mots_frame_base": mots_frame_base,
                **(
                    {"video_export_scope": video_scope_payload}
                    if video_scope_payload
                    else {}
                ),
                **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
            },
        ),
    )
    execution_id = str(uuid.uuid4())
    dispatch_args = []
    dispatch_kwargs = {
        "project_id": str(project.id),
        "batch_id": None,
        "targets": targets,
        "opts": {
            "include_attributes": include_attributes,
            "video_frame_mode": video_frame_mode,
            "axis_frame": axis_frame,
            "indexed_overlap_policy": indexed_overlap_policy,
            "video_overlap_policy": video_overlap_policy,
            "mots_frame_base": mots_frame_base,
            **(
                {"video_export_scope": video_scope_payload}
                if video_scope_payload
                else {}
            ),
            **({"lidar": lidar_options_payload} if lidar_options_payload else {}),
        },
        "async_job_id": str(job.id),
    }
    lease = await prepare_job_dispatch(
        db,
        job_id=job.id,
        task_name="app.workers.export.run_export",
        execution_id=execution_id,
        args=dispatch_args,
        kwargs=dispatch_kwargs,
    )
    await db.commit()

    await publish_prepared_dispatch(
        db,
        lease,
        publish=lambda: dispatch_job(
            task_name="app.workers.export.run_export",
            execution_id=execution_id,
            args=dispatch_args,
            kwargs=dispatch_kwargs,
        ),
    )
    return {"job_id": str(job.id)}


def _stage_supported_inputs(backend, model_id: str | None) -> list[str]:
    """v0.18.15 · 从 backend 能力快照取某 model 的 supported_inputs (一等输入契约)。

    health_meta.capabilities.models[] 已由 extract_capabilities 规范化 (含合成默认)。
    无快照 / 无 model_id / 匹配不到 → 返回 [] (调用方据此放过门控, 保持零退化)。
    """
    meta = getattr(backend, "health_meta", None)
    caps = meta.get("capabilities") if isinstance(meta, dict) else None
    models = caps.get("models") if isinstance(caps, dict) else None
    if not isinstance(models, list) or model_id is None:
        return []
    for m in models:
        if isinstance(m, dict) and str(m.get("id")) == str(model_id):
            return list(m.get("supported_inputs") or [])
    return []


def _stage_model(backend, model_id: str | None) -> dict:
    """v0.19.2 WS2 · 从 backend 能力快照取某 model 的规范化条目 (无快照/匹配不到 → {})。

    供 dispatch-time 校验读 resource_profile / output_attribute_types。读
    health_meta.capabilities.models[] (extract_capabilities 已规范化), 不走 /instances。
    """
    meta = getattr(backend, "health_meta", None)
    caps = meta.get("capabilities") if isinstance(meta, dict) else None
    models = caps.get("models") if isinstance(caps, dict) else None
    if not isinstance(models, list) or model_id is None:
        return {}
    for m in models:
        if isinstance(m, dict) and str(m.get("id")) == str(model_id):
            return {
                **m,
                "task": normalize_task(m.get("task") or "unknown", allow_unknown=True),
            }
    return {}


def _stage_device(backend, model_id: str | None) -> str | None:
    """v0.19.5 · 取某 model 的 resource_profile.device (gpu/cpu); 无快照/未自报 → None。"""
    from app.services.trained_models import controlled_device, is_controlled

    if is_controlled(model_id):
        return controlled_device(backend)
    rp = _stage_model(backend, model_id).get("resource_profile") or {}
    d = rp.get("device")
    return d if isinstance(d, str) else None


def _assert_capabilities(
    backend, model_id: str | None, where: str, *, writes_attributes: bool
) -> None:
    """v0.19.3 WS1 · 派发期能力闸门: 任一能力违例 → 422 硬挡。

    判据本体抽到 services/pipeline_validation.check_capability_violations (纯函数), 与保存路径
    软提示 + 前端 stageWarning 共用同一 SSOT。batchable=false (交互/有状态) 不可批量; 写属性的
    下游模型不产 class → 提前拦。均「显式自报才拦, 缺省放过」, 对老 backend 零退化。
    """
    violations = check_capability_violations(
        _stage_model(backend, model_id),
        where=where,
        model_id=model_id,
        writes_attributes=writes_attributes,
    )
    if violations:
        raise HTTPException(status_code=422, detail=violations[0].detail)


async def _compute_pipeline_capability_warnings(db, stages) -> list[str]:
    """v0.19.3 WS1 · 保存编排时算「能力软提示」(不挡), 与 dispatch 422 同判据共享纯函数。

    保存是配置中途态: backend 可能未启用 / 能力快照滞后 / 先存草稿之后再换 backend, 故只软提示。
    解析不到 backend 的阶段静默跳过 (留 dispatch-time 422 作最终把关)。返回 detail 字符串列表。
    """
    if not stages or not isinstance(stages, list):
        return []
    from app.services.ml_backend import MLBackendService

    svc = MLBackendService(db)
    warnings: list[str] = []
    # v0.20.21 · 建 stage号→(caps, model_id) map, 供下面校验下游阶段的「上游产出几何可否作 ROI」。
    stage_caps: dict[int, tuple[dict, object]] = {}
    for s in stages:
        if not isinstance(s, dict) or not s.get("ml_backend_id"):
            continue
        backend = await svc.get(s["ml_backend_id"])
        if not backend:
            continue
        caps = _stage_model(backend, s.get("model_id"))
        if s.get("stage") is not None:
            stage_caps[s["stage"]] = (caps, s.get("model_id"))
        is_source = s.get("parent_stage") is None
        where = "源阶段" if is_source else f"stage {s.get('stage')} "
        # 源阶段不写属性, class 判据不适用 (与 dispatch 对称)。
        writes_attributes = (
            not is_source
            and (s.get("write") or {}).get("target", "attributes") == "attributes"
        )
        violations = check_capability_violations(
            caps,
            where=where,
            model_id=s.get("model_id"),
            writes_attributes=writes_attributes,
        )
        warnings.extend(v.detail for v in violations)
    # v0.20.21 · 上下游几何兼容: 下游按上游框作 ROI, 上游若不产 bbox/polygon → 软提示。
    for s in stages:
        if not isinstance(s, dict):
            continue
        parent = s.get("parent_stage")
        if parent is None or parent not in stage_caps:
            continue
        parent_caps, parent_model_id = stage_caps[parent]
        geo_violations = check_parent_geometry_roi(
            parent_caps,
            where=f"stage {s.get('stage')} ",
            parent_model_id=parent_model_id,
        )
        warnings.extend(v.detail for v in geo_violations)
    return warnings


async def _validate_preannotate_task_scope(
    db: AsyncSession,
    *,
    project: Project,
    task_ids: list[uuid.UUID],
    batch_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Validate and canonicalize an explicit preannotation task scope."""

    requested = sorted(set(task_ids), key=str)
    rows = (
        await db.execute(
            select(task_row, batch_row)
            .outerjoin(batch_row, batch_row.id == task_row.batch_id)
            .where(task_row.project_id == project.id, task_row.id.in_(requested))
        )
    ).all()
    if len(rows) != len(requested):
        raise HTTPException(
            status_code=422,
            detail="task_ids must belong to the selected project",
        )
    if batch_id is not None and any(task.batch_id != batch_id for task, _ in rows):
        raise HTTPException(
            status_code=422,
            detail="task_ids must belong to batch_id",
        )
    if any(batch is not None and batch.admin_locked for _, batch in rows):
        raise HTTPException(
            status_code=409,
            detail="one or more selected batches are admin-locked",
        )
    if any(task.status != "pending" for task, _ in rows):
        raise HTTPException(
            status_code=409,
            detail="explicit preannotation tasks must be pending",
        )
    # issue #124 · draft 批次在未分派标注员与质检员时同样可批量预标;
    # 已分派人员的 draft 与已进入人工流程的批次仍拒绝。
    if any(
        batch is not None and not allows_bulk_preannotation(batch) for _, batch in rows
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "explicit preannotation tasks must belong to active or "
                "unassigned draft batches"
            ),
        )
    from app.services.task_lock import TaskLockService

    lock_service = TaskLockService(db)
    for task, _ in rows:
        if await lock_service.active_lock(task.id) is not None:
            raise HTTPException(
                status_code=409,
                detail="one or more selected tasks are locked for editing",
            )
    return requested


# 终态任务: 先撤回 / 重开后再运行; 其它不可编辑状态 (如 uploading) 单独提示。
_WORKBENCH_AI_TERMINAL_STATUSES = {
    TaskStatus.REVIEW.value,
    TaskStatus.COMPLETED.value,
}


async def _validate_workbench_task_scope(
    db: AsyncSession,
    *,
    project: Project,
    task_ids: list[uuid.UUID],
    actor: User | IdentityActorFacts,
) -> list[uuid.UUID]:
    """校验工作台「当前题 AI」的单题执行范围 (issue #121)。

    与数据管理批量路径不同, 交互式执行只针对调用者正在编辑的那道题, 因此允许
    in_progress 任务与 draft 批次; 任务归属、批次管理员锁、他人编辑锁与终态任务
    仍然受保护。拒绝原因用中文, 因为工作台面板会原样展示 API detail。
    """

    requested = sorted(set(task_ids), key=str)
    if len(requested) != 1:
        raise HTTPException(
            status_code=422,
            detail="工作台当前题 AI 一次仅支持一个任务",
        )
    row = (
        await db.execute(
            select(task_row, batch_row)
            .outerjoin(batch_row, batch_row.id == task_row.batch_id)
            .where(task_row.project_id == project.id, task_row.id == requested[0])
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在或不属于当前项目")
    task, batch = row
    if batch is not None and batch.admin_locked:
        raise HTTPException(
            status_code=409,
            detail="任务所属批次已被管理员锁定，无法运行 AI",
        )
    # 白名单而非黑名单: 只有明确可编辑的 pending / in_progress / rejected 放行,
    # uploading (对象未校验) 与未来新增/非法状态一律拒绝 (issue #121 review)。
    if task.status not in WORKBENCH_AI_EDITABLE_TASK_STATUSES:
        if task.status in _WORKBENCH_AI_TERMINAL_STATUSES:
            detail = "当前任务已提交审核或已完成，请先撤回或重开后再运行 AI"
        else:
            detail = "当前任务尚未就绪，请等待上传完成后再运行 AI"
        raise HTTPException(status_code=409, detail=detail)
    from app.services.task_lock import TaskLockService

    # 同一 task 可能存在多行残留锁 (unique 仅在 (task_id, user_id) 上), 必须逐行判断,
    # 不能只看 active_lock() 的第一行, 否则他人持锁时可能被本人锁掩盖 (issue #121 review)。
    locks = await TaskLockService(db).active_locks(task.id)
    if any(lock.user_id != actor.id for lock in locks):
        raise HTTPException(
            status_code=409,
            detail="当前任务正由其他成员编辑，无法运行 AI",
        )
    return requested


@router.post("/{project_id}/preannotate")
async def trigger_preannotation(
    body: preannotate_schemas.PreannotateRequest,
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional durable identity for retrying a scoped preannotation request.",
    ),
    batch_dispatcher: BatchPredictionDispatcher = Depends(
        get_batch_prediction_dispatcher
    ),
    input_admission: InputAdmission = Depends(get_input_admission),
    input_manifest_capture: InputManifestCaptureFactory = Depends(
        get_input_manifest_capture
    ),
    video_input_manifest_capture: VideoInputManifestCaptureFactory = Depends(
        get_video_input_manifest_capture
    ),
):
    from app.services.ml_backend import MLBackendService
    from app.services.audit import AuditService
    from app.services.prediction_context import build_predict_context
    from app.services.prediction_retry import retryable_request_context
    from app.services import trained_models

    execution_unit = _preannotation_execution_unit(body)
    if not _frame_pipeline_supported(execution_unit, body.pipeline_stages):
        raise HTTPException(
            status_code=422,
            detail="逐帧预标注暂不支持多阶段流水线",
        )

    uses_controlled_model = trained_models.is_controlled(body.model_id) or any(
        trained_models.is_controlled(stage.model_id)
        for stage in (body.pipeline_stages or [])
    )
    frozen_model_ref = None
    if uses_controlled_model:
        root_source = (
            next(
                (
                    s.source
                    for s in (body.pipeline_stages or [])
                    if s.parent_stage is None
                ),
                None,
            )
            or {}
        )
        trained_models.assert_image_scope(
            project,
            prompt=body.prompt,
            task=body.task,
            prompt_type=body.prompt_type,
            model_variants=body.model_variants,
            pipeline_stages=body.pipeline_stages,
            execution_unit=root_source.get("execution_unit"),
        )
        if root_source.get("data_type") not in {None, "image"}:
            raise HTTPException(status_code=422, detail="受控模型不支持该数据源")

    svc = MLBackendService(db)
    source_backend_id = body.ml_backend_id
    if source_backend_id is None:
        raise HTTPException(status_code=422, detail="ml_backend_id 必填")
    backend = await svc.get(source_backend_id)
    # v0.19.0 ADR-0044 · 校验 backend 存在且在本项目「已启用」
    # (project_ml_backend_pool.enabled, 经 pool member 解析回 registry):
    # 与下游阶段校验对称, 防止 owner 手动 POST 未启用/别项目 backend id 触发预标。
    if not backend or not await svc.is_enabled(project.id, source_backend_id):
        raise HTTPException(status_code=404, detail="ML Backend not found")

    from aap_inference.capabilities import resolve_model_task

    try:
        body.task = resolve_model_task(backend, body.model_id, body.task)
        for stage in body.pipeline_stages or []:
            if stage.parent_stage is None:
                stage.task = body.task
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    retry_context = build_predict_context(
        prompt=body.prompt,
        output_mode=body.output_mode,
        params=body.params,
        model_id=body.model_id,
        task=body.task,
        prompt_type=body.prompt_type,
        model_variants=body.model_variants,
        class_filter=body.class_filter,
        box_threshold=float(project.box_threshold),
        text_threshold=float(project.text_threshold),
    )
    if retry_context is not None and retryable_request_context(retry_context) is None:
        raise HTTPException(status_code=422, detail="预标注模型上下文不能超过 8 KiB")

    # v0.19.2 WS2 · 源阶段 (单模型预标 / 流水线源) batchable 闸门: 交互/有状态模型不可批量。
    # 源阶段不写属性, class 判据不适用 (writes_attributes=False)。
    _assert_capabilities(backend, body.model_id, "源阶段", writes_attributes=False)

    # v0.19.5 · 设备感知队列路由: 收集源 + 各下游阶段 model 的 device, 据此选 Celery 队列。
    stage_devices: list[str | None] = [_stage_device(backend, body.model_id)]

    # v0.18.1 · 多阶段编排: 校验每个下游阶段的 backend 存在且归属本项目, 归一化成 worker
    # 可消费的 stage dict 列表 (uuid → str)。源阶段 backend 归属已在上面校验。
    pipeline_stages_payload: list[dict] | None = None
    if body.pipeline_stages:
        norm: list[dict] = []
        # v0.20.21 · stage号→(caps, model_id) map, source stage is identified by parent_stage=None.
        # 循环内填各下游 caps, 循环后校验「上游产出几何可否作下游 ROI」。
        stage_caps: dict[int, tuple[dict, object]] = {
            next(
                stage.stage
                for stage in body.pipeline_stages
                if stage.parent_stage is None
            ): (_stage_model(backend, body.model_id), body.model_id)
        }
        for st in body.pipeline_stages:
            resolved_input = st.input
            if st.parent_stage is not None:
                st_backend = await svc.get(st.ml_backend_id)
                if not st_backend or not await svc.is_enabled(
                    project.id, st.ml_backend_id
                ):
                    raise HTTPException(
                        status_code=404,
                        detail=f"stage {st.stage} 的 ML Backend 不存在或未在本项目启用",
                    )
                try:
                    st.task = resolve_model_task(st_backend, st.model_id, st.task)
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"stage {st.stage}: {exc}"
                    ) from exc
                # v0.19.2 WS2 / v0.19.3 WS1 · 下游阶段能力闸门: batchable + 写属性产 class。
                # 写属性的子但模型不产 class → 跑完属性恒空, 提前 422。
                _assert_capabilities(
                    st_backend,
                    st.model_id,
                    f"stage {st.stage} ",
                    writes_attributes=(st.write or {}).get("target", "attributes")
                    == "attributes",
                )
                # v0.19.5 · 收集下游阶段 device 供队列路由。
                stage_devices.append(_stage_device(st_backend, st.model_id))
                # v0.20.21 · 记本阶段 caps, 供循环后校验其作为下游父阶段时几何可否作 ROI。
                stage_caps[st.stage] = (
                    _stage_model(st_backend, st.model_id),
                    st.model_id,
                )
                # v0.18.15 · 按子模型 supported_inputs 解析投递方式 + 几何可达性门控。
                # 产几何的子: supported_inputs 须含 bbox_prompt (geometry-prompt 路径) 或
                # crop (普通检测器在 crop 上跑 + 坐标回映)。据此把投递方式烘焙进 input.mode,
                # worker 直接消费 (无快照时 inputs=[] → 放过门控、不烘焙, 保持零退化)。
                target = (st.write or {}).get("target", "attributes")
                inputs = _stage_supported_inputs(st_backend, st.model_id)
                if target in {"geometry", "intermediate"} and inputs:
                    if INPUT_BBOX_PROMPT not in inputs and INPUT_CROP not in inputs:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"stage {st.stage} 产几何 (write.target={target!r}), 但其模型 "
                                f"supported_inputs={inputs} 不含 "
                                f"{INPUT_BBOX_PROMPT!r}/{INPUT_CROP!r}, 无法作几何下游"
                            ),
                        )
                    if not (st.input or {}).get("mode"):
                        mode = "geometry" if INPUT_BBOX_PROMPT in inputs else "crop"
                        resolved_input = {**(st.input or {}), "mode": mode}
            norm.append(
                {
                    "stage": st.stage,
                    "ml_backend_id": str(st.ml_backend_id),
                    "model_id": st.model_id,
                    "task": st.task,
                    "prompt_type": st.prompt_type,
                    "model_variants": st.model_variants,
                    "params": st.params,
                    "class_filter": st.class_filter,
                    "parent_stage": st.parent_stage,
                    "parent_class_filter": st.parent_class_filter,
                    "roi": st.roi,
                    "write": st.write,
                    "on_failure": st.on_failure,
                    # v0.18.14 · 卡片显示名 + 写回属性键前缀 (子物体命名空间, 如 hat_color)。
                    "label": st.label,
                    # v0.18.15 · 投递模式: 用户显式 input 或按 supported_inputs 烘焙的结果。
                    "input": resolved_input,
                    # v0.18.2 · 键冲突策略下放到每个下游阶段, worker 末位覆盖时据此合并。
                    "on_key_conflict": body.on_key_conflict,
                }
            )
        # v0.20.21 · 上下游几何兼容闸门 (与「下游能否吃框」门对称): 下游按上游框作 ROI,
        # 上游若完全不产 bbox/polygon → 该阶段所有父框运行期被跳过、零富集, 提前 422 硬挡。
        for st in body.pipeline_stages:
            if st.parent_stage is None or st.parent_stage not in stage_caps:
                continue
            parent_caps, parent_model_id = stage_caps[st.parent_stage]
            geo_violations = check_parent_geometry_roi(
                parent_caps,
                where=f"stage {st.stage} ",
                parent_model_id=parent_model_id,
            )
            if geo_violations:
                raise HTTPException(status_code=422, detail=geo_violations[0].detail)
        pipeline_stages_payload = norm

    from app.services import async_job as async_job_svc
    from app.services.data_management.actions import (
        assert_idempotent_request_matches,
        find_idempotent_job,
        lock_idempotency_key,
        normalize_idempotency_key,
        request_digest as data_manager_request_digest,
    )

    idempotency_key = normalize_idempotency_key(idempotency_key)
    existing_job = None
    request_digest_value = None
    canonical_task_ids = (
        sorted(set(body.task_ids), key=str) if body.task_ids is not None else None
    )
    if idempotency_key is not None:
        canonical_body = body.model_dump(mode="json")
        canonical_body["task_ids"] = (
            [str(task_id) for task_id in canonical_task_ids]
            if canonical_task_ids is not None
            else None
        )
        request_digest_value = data_manager_request_digest(canonical_body)
        await lock_idempotency_key(
            db,
            action="preannotate",
            project_id=project.id,
            actor_id=current_user.id,
            key=idempotency_key,
        )
        existing_job = await find_idempotent_job(
            db,
            kind="batch_predict",
            project_id=project.id,
            actor_id=current_user.id,
            key=idempotency_key,
        )
        if existing_job is not None:
            assert_idempotent_request_matches(existing_job, digest=request_digest_value)
            if existing_job.status != "pending":
                return {
                    "job_id": str(existing_job.id),
                    "status": existing_job.status,
                    "total_tasks": (existing_job.payload or {}).get("total_tasks"),
                    "channel": f"project:{project.id}:preannotate",
                    "celery_task_id": existing_job.celery_task_id,
                }

    existing_payload = (
        existing_job.payload
        if existing_job is not None and isinstance(existing_job.payload, dict)
        else {}
    )
    has_video_manifest_markers = any(
        key in existing_payload
        for key in (
            "accepted_video_inputs_protocol",
            "accepted_video_manifest_id",
            "accepted_video_selection",
            "accepted_video_selection_digest",
        )
    )
    has_image_manifest_markers = any(
        key in existing_payload for key in _VIDEO_INPUT_IMAGE_MARKERS
    )
    if existing_job is not None and (
        (execution_unit == "frame" and has_image_manifest_markers)
        or (execution_unit != "frame" and has_video_manifest_markers)
    ):
        await async_job_svc.mark_dispatch_failed(
            db, existing_job.id, error="accepted_video_manifest_identity_invalid"
        )
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail="本次预标注作业的冻结输入类型不匹配，请重新发起",
        )
    replay_image_manifest = existing_job is not None and (
        existing_payload.get("accepted_input_manifest_protocol") is not None
    )
    replay_video_manifest = existing_job is not None and execution_unit == "frame"
    replay_manifest = replay_image_manifest or replay_video_manifest
    if body.execution_scope == "workbench":
        if not body.task_ids:
            raise HTTPException(
                status_code=422,
                detail="工作台当前题 AI 必须指定当前任务",
            )
        # 工作台单题语义由任务推导批次, 不接受 batch_id: 否则会落入下方「批次须 active」
        # 的批量校验 (draft 批次误报 400) 或放行无关 active 批次 (issue #121 review)。
        if body.batch_id is not None:
            raise HTTPException(
                status_code=422,
                detail="工作台当前题 AI 不支持指定批次",
            )
    explicit_task_ids: list[uuid.UUID] | None = None
    if body.task_ids:
        if replay_manifest:
            # The native capture replay validates the original selector; do not
            # reselect or replace accepted membership from today's Task state.
            explicit_task_ids = canonical_task_ids
        elif body.execution_scope == "workbench":
            explicit_task_ids = await _validate_workbench_task_scope(
                db,
                project=project,
                task_ids=body.task_ids,
                actor=current_user,
            )
        else:
            explicit_task_ids = await _validate_preannotate_task_scope(
                db,
                project=project,
                task_ids=body.task_ids,
                batch_id=body.batch_id,
            )

    # v0.9.5 · 指定 batch 时校验归属本项目 + 状态在 active
    total_tasks_hint: int | None = None
    if replay_manifest:
        total_tasks_hint = (existing_job.payload or {}).get("total_tasks")
    elif body.batch_id:
        from app.db.enums import BatchStatus

        batch = await get_core_row(db, batch_row, body.batch_id)
        if not batch or batch.project_id != project.id:
            raise HTTPException(status_code=404, detail="Batch not found")
        # issue #124 · 未分派人员（标注员与质检员）的 draft 批次同样可批量预标,
        # 让管理员先跑 AI 再分派人工; 已分派 draft / 其它状态仍拒绝。
        if not allows_bulk_preannotation(batch):
            if batch.status == BatchStatus.DRAFT:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "draft batch already has an annotator or reviewer "
                        "assigned; bulk preannotation is only available "
                        "before assignment"
                    ),
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "batch.status must be 'active' or an unassigned 'draft' "
                    f"to preannotate, got {batch.status!r}"
                ),
            )
        if batch.admin_locked:
            raise HTTPException(status_code=409, detail="batch is admin-locked")
        hint_conds = [
            task_row.batch_id == body.batch_id,
            task_row.status == "pending",
        ]
        # v0.11.24 · skip_predicted 下进度条分母应排除已预标 task，否则虚高
        if body.predict_mode == "skip_predicted":
            hint_conds.append(task_row.total_predictions == 0)
        count_q = await db.execute(select(func.count(task_row.id)).where(*hint_conds))
        total_tasks_hint = int(count_q.scalar_one() or 0)
    elif explicit_task_ids is None:
        hint_conds = [task_row.project_id == project.id, task_row.status == "pending"]
        if body.predict_mode == "skip_predicted":
            hint_conds.append(task_row.total_predictions == 0)
        count_q = await db.execute(select(func.count(task_row.id)).where(*hint_conds))
        total_tasks_hint = int(count_q.scalar_one() or 0)

    # v0.19.5 · 设备感知路由: 全 CPU pipeline → cpu 队列; 任一 GPU/未自报阶段 → gpu(ml) 队列 (零退化)。
    queue = resolve_preannotate_queue(
        stage_devices,
        gpu_queue=settings.preannotate_gpu_queue,
        cpu_queue=settings.preannotate_cpu_queue,
    )
    # v0.21.6 · detect-then-track: 含 tracker 阶段的 job 施加 soft 超时 (帧上限之外的双保险)。
    #   单阶段源 tracker 走 body.task; 编排里的 tracker 阶段走 pipeline_stages_payload。
    has_tracker_stage = body.task == "tracker" or any(
        (s or {}).get("task") == "tracker" for s in (pipeline_stages_payload or [])
    )
    has_text_stage = body.task == "text" or any(
        (s or {}).get("task") == "text" for s in (pipeline_stages_payload or [])
    )
    # v0.21.7 · 逐帧执行单位: 从源阶段(parent_stage=None)的 source.execution_unit 提取 (norm 丢弃了
    #   source, 故从原始 body.pipeline_stages 取)。frame → worker 走二级 fan-out 逐帧跑图像 backend。
    #   execution_unit 是整任务迭代粒度、非 per-stage, 故作 batch_predict 顶层参数。
    #   源阶段判据须与 _validate_pipeline_stages 一致 (恰一个 parent_stage=None); stage 号是自由整数,
    #   不保证为 0, 故不能按 s.stage == 0 找源。
    uses_input_manifest = replay_image_manifest or (
        body.execution_scope == "bulk"
        and project.data_type == "image"
        and execution_unit not in {"frame", "video", "scene"}
        and not has_tracker_stage
        and not has_text_stage
        and body.task not in {"text", "tracker"}
    )
    uses_video_manifest = execution_unit == "frame"

    accepted_input_task_id: uuid.UUID | None = None
    if (
        body.execution_scope == "workbench"
        and explicit_task_ids is not None
        and len(explicit_task_ids) == 1
        and execution_unit not in {"frame", "video", "scene"}
        and not has_tracker_stage
        and not has_text_stage
        and body.task not in {"text", "tracker"}
    ):
        candidate_task = await get_core_row(db, task_row, explicit_task_ids[0])
        if candidate_task is not None and candidate_task.file_type == "image":
            accepted_input_task_id = candidate_task.id

    accepted_input_payload = (
        {
            "accepted_inputs_protocol": "aap-assets-inputs/v1",
            # The durable job exists before admission. The empty map records that
            # this operation requires a canonical input and can safely replay it.
            "accepted_input_ids": {},
        }
        if accepted_input_task_id is not None
        else {}
    )

    if uses_controlled_model and not replay_manifest:
        media_scope = [task_row.project_id == project.id, task_row.file_type != "image"]
        if explicit_task_ids is not None:
            media_scope.append(task_row.id.in_(explicit_task_ids))
        else:
            media_scope.append(task_row.status == "pending")
        if body.batch_id:
            media_scope.append(task_row.batch_id == body.batch_id)
        if await db.scalar(select(task_row.id).where(*media_scope).limit(1)):
            raise HTTPException(status_code=422, detail="所选任务包含非图片素材")

    async def freeze_controlled_model():
        from app.services.trained_model_reference_contract import (
            FreezeModelReferenceError,
            FreezeModelReferenceRequest,
        )

        try:
            ref = await trained_models.bind_frozen_model_reference(db).freeze(
                FreezeModelReferenceRequest(
                    project_id=project.id,
                    project_classes=trained_models.project_classes(project),
                    model_id=body.model_id,
                    registry_id=source_backend_id,
                )
            )
        except FreezeModelReferenceError as exc:
            trained_models.raise_frozen_model_reference_error(exc)
        checked = build_predict_context(
            prompt=body.prompt,
            output_mode=body.output_mode,
            params=body.params,
            model_id=body.model_id,
            task=body.task,
            prompt_type=body.prompt_type,
            model_variants=body.model_variants,
            class_filter=body.class_filter,
            frozen_model_ref=ref,
        )
        if checked is not None and retryable_request_context(checked) is None:
            raise HTTPException(
                status_code=422, detail="模型类别与来源上下文超过 8 KiB 限制"
            )
        return ref

    # 派发前预建持久作业, 让客户端可轮询同一 job、并发重试不会重复入队。
    # - 数据管理重试可带幂等键 (复用/去重)。
    # - 工作台单题请求 (无幂等键) 也预建, 使 worker 在派发前的范围复校验失败时
    #   on_failure 能按 celery_task_id 落一条 failed 作业 (issue #121 review)。
    accepted_input_manifest_protocol = None
    accepted_input_manifest_id = None
    accepted_input_manifest_digest = None
    accepted_video_inputs_protocol = None
    accepted_video_manifest_id = None
    accepted_video_selection_digest = None
    accepted_video_selection = None

    async def capture_input_manifest(job: AsyncJob, *, replay: bool):
        nonlocal total_tasks_hint
        nonlocal accepted_input_manifest_protocol
        nonlocal accepted_input_manifest_id
        nonlocal accepted_input_manifest_digest
        if not uses_input_manifest:
            return

        from aap_assets_input_manifests import contract as manifests

        selection = manifests.ManifestSelection(
            batch_id=body.batch_id,
            task_ids=explicit_task_ids,
            predict_mode=body.predict_mode,
        )
        payload = job.payload or {}
        if replay and (
            payload.get("accepted_input_manifest_protocol") != manifests.MANIFEST_SCHEMA
            or payload.get("accepted_input_manifest_id") != str(job.id)
            or payload.get("input_selection") != selection.model_dump(mode="json")
            or not isinstance(payload.get("accepted_input_manifest_digest"), str)
        ):
            await async_job_svc.mark_dispatch_failed(
                db, job.id, error="accepted_input_manifest_identity_invalid"
            )
            await db.commit()
            raise HTTPException(
                status_code=409,
                detail="本次批量图像任务缺少有效冻结输入清单，请重新发起",
            )
        if not replay:
            job.payload = {
                **payload,
                "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
                "accepted_input_manifest_id": str(job.id),
                "input_selection": selection.model_dump(mode="json"),
            }
        capture_request = manifests.CaptureManifestRequest(
            job_id=job.id,
            actor_id=current_user.id,
            project_id=project.id,
            selection=selection,
        )
        try:
            header = await input_manifest_capture(db).capture(capture_request)
            manifests.require_scope(capture_request, header)
            if replay and (
                header.selection_digest != payload["accepted_input_manifest_digest"]
            ):
                raise manifests.ManifestFailure("manifest_scope_mismatch")
        except (ValueError, TypeError, AttributeError) as exc:
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail="冻结输入清单返回无效，请重试本次请求",
                headers={"Retry-After": "5"},
            ) from exc
        except manifests.ManifestFailure as exc:
            await db.rollback()
            transient = exc.code.endswith("_busy") or exc.code in {
                "storage_unavailable",
                "invalid_response",
            }
            raise HTTPException(
                status_code=503 if transient else 409,
                detail=f"无法冻结批量图像输入：{exc.code}",
                headers={"Retry-After": "5"} if transient else None,
            ) from exc
        total_tasks_hint = header.selected_count
        accepted_input_manifest_protocol = manifests.MANIFEST_SCHEMA
        accepted_input_manifest_id = str(job.id)
        accepted_input_manifest_digest = header.selection_digest
        job.payload = {
            **(job.payload or {}),
            "accepted_input_manifest_digest": header.selection_digest,
            "total_tasks": header.selected_count,
            "input_candidate_count": header.candidate_count,
        }

    async def capture_video_manifest(job: AsyncJob, *, replay: bool):
        nonlocal total_tasks_hint
        nonlocal accepted_video_inputs_protocol
        nonlocal accepted_video_manifest_id
        nonlocal accepted_video_selection_digest
        nonlocal accepted_video_selection
        if not uses_video_manifest:
            return

        from aap_assets_video_inputs import execution as video_contract
        from aap_assets_video_inputs import values as video_values

        try:
            video_selection = (
                _restore_video_input_selection(body, explicit_task_ids, job.payload)
                if replay
                else _build_video_input_selection(
                    body,
                    explicit_task_ids,
                    max_frames=settings.frame_preannotate_max_frames,
                )
            )
            header = await _capture_video_input_manifest(
                db,
                job,
                actor_id=current_user.id,
                project_id=project.id,
                selection=video_selection,
                capture_factory=video_input_manifest_capture,
                replay=replay,
            )
        except video_contract.VideoInputFailure as exc:
            transient = exc.code.endswith("_busy") or exc.code in {
                "storage_unavailable",
                "invalid_response",
            }
            if replay and not transient:
                await async_job_svc.mark_dispatch_failed(
                    db, job.id, error=f"accepted_video_{exc.code}"
                )
                await db.commit()
            else:
                await db.rollback()
            raise HTTPException(
                status_code=503 if transient else 409,
                detail=(
                    "冻结视频输入暂时不可用，请重试本次请求"
                    if transient
                    else f"无法冻结逐帧视频输入：{exc.code}"
                ),
                headers={"Retry-After": "5"} if transient else None,
            ) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail="冻结视频输入返回无效，请重试本次请求",
                headers={"Retry-After": "5"},
            ) from exc
        except Exception as exc:
            # A lost native response is ambiguous; keep replayable durable state
            # only when it was committed before this call.
            await db.rollback()
            raise HTTPException(
                status_code=503,
                detail="冻结视频输入暂时不可用，请重试本次请求",
                headers={"Retry-After": "5"},
            ) from exc

        total_tasks_hint = header.selected_count
        accepted_video_inputs_protocol = video_values.VIDEO_INPUT_SCHEMA
        accepted_video_manifest_id = str(job.id)
        accepted_video_selection_digest = header.selection_digest
        accepted_video_selection = video_selection.model_dump(mode="json")

    precreated_job: AsyncJob | None = None
    if idempotency_key is not None:
        existing = existing_job
        if existing is not None:
            # A request may have crashed after committing this pending row but
            # before publishing to Celery. Reuse the row and safely republish;
            # the worker's row lock makes a duplicate delivery a no-op.
            precreated_job = existing
            if uses_controlled_model:
                frozen_model_ref = (existing.payload or {}).get("frozen_model_ref")
                if not frozen_model_ref:
                    raise HTTPException(
                        status_code=409, detail="原任务缺少冻结模型身份"
                    )
        else:
            scoped_ids = (
                [str(task_id) for task_id in explicit_task_ids]
                if explicit_task_ids is not None
                else None
            )
            precreated_payload: dict = {
                "batch_id": str(body.batch_id) if body.batch_id else None,
                "execution_scope": body.execution_scope,
                "ml_backend_id": str(source_backend_id),
                "total_tasks": (
                    len(scoped_ids) if scoped_ids is not None else total_tasks_hint
                ),
                "project_display_id": project.display_id,
                "project_name": project.name,
                "data_manager_idempotency_key": idempotency_key,
                "data_manager_request_digest": request_digest_value,
                **accepted_input_payload,
            }
            if scoped_ids is not None:
                precreated_payload["scope"] = {"task_ids": scoped_ids}
                precreated_payload["task_ids"] = scoped_ids
            if uses_controlled_model:
                frozen_model_ref = await freeze_controlled_model()
                precreated_payload["frozen_model_ref"] = frozen_model_ref
            precreated_job = await async_job_svc.create_job(
                db,
                kind="batch_predict",
                user_id=current_user.id,
                project_id=project.id,
                payload=precreated_payload,
            )
        # Use the durable async job id as the Celery task id. The failure hook
        # can therefore resolve a job even if the worker fails before the API
        # request reaches its post-dispatch commit.
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
        await capture_input_manifest(precreated_job, replay=existing is not None)
        await capture_video_manifest(precreated_job, replay=existing is not None)
        await db.commit()
    else:
        # Every request without an idempotency key still needs a durable job id
        # before publishing, including ordinary project/batch bulk requests.
        # Otherwise the API returns a Celery id while the worker creates a
        # different AsyncJob only after it starts selecting tasks.
        scoped_ids = (
            [str(task_id) for task_id in explicit_task_ids]
            if explicit_task_ids is not None
            else None
        )
        if uses_controlled_model:
            frozen_model_ref = await freeze_controlled_model()
        precreated_job = await async_job_svc.create_job(
            db,
            kind="batch_predict",
            user_id=current_user.id,
            project_id=project.id,
            payload={
                "batch_id": str(body.batch_id) if body.batch_id else None,
                "execution_scope": body.execution_scope,
                "ml_backend_id": str(source_backend_id),
                "total_tasks": len(scoped_ids)
                if scoped_ids is not None
                else total_tasks_hint,
                "project_display_id": project.display_id,
                "project_name": project.name,
                **accepted_input_payload,
                **(
                    {"scope": {"task_ids": scoped_ids}, "task_ids": scoped_ids}
                    if scoped_ids is not None
                    else {}
                ),
                **({"frozen_model_ref": frozen_model_ref} if frozen_model_ref else {}),
            },
        )
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
        await capture_input_manifest(precreated_job, replay=False)
        await capture_video_manifest(precreated_job, replay=False)
        await db.commit()

    accepted_inputs_protocol: str | None = None
    accepted_input_ids: dict[str, str] | None = None
    if accepted_input_task_id is not None:
        from aap_assets_inputs import contract as accepted_inputs_contract
        from aap_assets_inputs.contract import InputFailure

        if precreated_job is None:
            raise RuntimeError("Workbench input admission requires a durable job")
        job_id = precreated_job.id
        task_key = str(accepted_input_task_id)
        durable_payload = precreated_job.payload or {}
        if durable_payload.get("accepted_inputs_protocol") != "aap-assets-inputs/v1":
            await async_job_svc.mark_dispatch_failed(
                db,
                job_id,
                error="accepted_input_legacy_workbench_job_rejected",
            )
            await db.commit()
            raise HTTPException(
                status_code=409,
                detail="本次工作台图像任务缺少已冻结的输入身份，请重新发起",
            )

        raw_input_ids = durable_payload.get("accepted_input_ids")
        if not isinstance(raw_input_ids, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_input_ids.items()
        ):
            await async_job_svc.mark_dispatch_failed(
                db, job_id, error="accepted_input_identity_invalid"
            )
            await db.commit()
            raise HTTPException(status_code=409, detail="冻结输入身份无效")
        if raw_input_ids and set(raw_input_ids) != {task_key}:
            await async_job_svc.mark_dispatch_failed(
                db, job_id, error="accepted_input_scope_mismatch"
            )
            await db.commit()
            raise HTTPException(status_code=409, detail="冻结输入任务范围不匹配")

        try:
            admission = await input_admission.accept(
                accepted_inputs_contract.FindInputRequest(
                    job_id=job_id,
                    task_id=accepted_input_task_id,
                    actor_id=current_user.id,
                )
            )
        except InputFailure as exc:
            terminal_source_errors = {
                "source_missing",
                "source_changed",
                "source_not_ready",
                "not_image",
                "task_not_visible",
                "permission_denied",
                "input_missing",
                "input_expired",
                "input_corrupt",
                "input_scope_mismatch",
                "job_not_active",
            }
            if exc.code in terminal_source_errors:
                await async_job_svc.mark_dispatch_failed(
                    db, job_id, error=f"accepted_input_{exc.code}"
                )
                await db.commit()
                raise HTTPException(
                    status_code=409,
                    detail=f"图像来源无法冻结：{exc.code}",
                ) from exc
            # Busy/storage failures may have happened after the owner reserved the
            # canonical row. Keep the job pending so the same-key replay can find
            # it without observing a newer Task URL.
            raise HTTPException(
                status_code=503,
                detail="冻结图像输入暂时不可用，请重试本次请求",
                headers={"Retry-After": "5"},
            ) from exc
        except Exception as exc:
            # A lost owner response is ambiguous. Retain the pending job and replay
            # this same (job, task, actor) identity on the next request.
            raise HTTPException(
                status_code=503,
                detail="冻结图像输入暂时不可用，请重试本次请求",
                headers={"Retry-After": "5"},
            ) from exc

        if (
            admission.job_id != job_id
            or admission.task_id != accepted_input_task_id
            or admission.actor_id != current_user.id
            or admission.status in {"failed", "expired"}
        ):
            error_code = admission.error or admission.status
            await async_job_svc.mark_dispatch_failed(
                db, job_id, error=f"accepted_input_{error_code}"
            )
            await db.commit()
            raise HTTPException(
                status_code=409,
                detail=f"冻结图像输入不可用：{error_code}",
            )

        accepted_inputs_protocol = "aap-assets-inputs/v1"
        accepted_input_ids = {task_key: str(admission.input_id)}
        if raw_input_ids and raw_input_ids != accepted_input_ids:
            await async_job_svc.mark_dispatch_failed(
                db, job_id, error="accepted_input_identity_mismatch"
            )
            await db.commit()
            raise HTTPException(status_code=409, detail="冻结输入身份与作业不匹配")
        if raw_input_ids != accepted_input_ids:
            await async_job_svc.update_payload(
                db,
                job_id,
                {
                    "accepted_inputs_protocol": accepted_inputs_protocol,
                    "accepted_input_ids": accepted_input_ids,
                },
            )
            await db.commit()

    soft_time_limit = (
        settings.tracker_soft_time_limit_seconds if has_tracker_stage else None
    )
    batch_request = BatchPredictionRequest(
        project_id=str(project.id),
        ml_backend_id=str(source_backend_id),
        task_ids=(
            [str(tid) for tid in explicit_task_ids] if explicit_task_ids else None
        ),
        prompt=body.prompt,
        output_mode=body.output_mode,
        batch_id=str(body.batch_id) if body.batch_id else None,
        user_id=str(current_user.id),
        params=body.params or None,
        predict_mode=body.predict_mode,
        # v0.14.9 · protocol v2 resolves the durable controlled model identity here.
        model_id=(
            f"trained:{frozen_model_ref['model_version_id']}"
            if frozen_model_ref
            else body.model_id
        ),
        task=body.task,
        prompt_type=body.prompt_type,
        model_variants=body.model_variants,
        class_filter=body.class_filter,
        pipeline_stages=pipeline_stages_payload,
        execution_unit=execution_unit,
        execution_scope=body.execution_scope,
        async_job_id=str(precreated_job.id) if precreated_job is not None else None,
        frozen_model_ref=frozen_model_ref,
        accepted_inputs_protocol=accepted_inputs_protocol,
        accepted_input_ids=accepted_input_ids,
        accepted_input_manifest_protocol=accepted_input_manifest_protocol,
        accepted_input_manifest_id=accepted_input_manifest_id,
        accepted_input_manifest_digest=accepted_input_manifest_digest,
        accepted_video_inputs_protocol=accepted_video_inputs_protocol,
        accepted_video_manifest_id=accepted_video_manifest_id,
        accepted_video_selection_digest=accepted_video_selection_digest,
        accepted_video_selection=accepted_video_selection,
    )
    if precreated_job is not None and execution_unit not in {"frame", "video", "scene"}:
        from app.application.preannotation_results import preannotation_request_digest

        digest = preannotation_request_digest(batch_request)
        locked_job = (
            await db.execute(
                job_lock_statement(
                    precreated_job.id, mode="update", populate_existing=True
                )
            )
        ).scalar_one()
        previous_digest = (locked_job.payload or {}).get("preannotation_request_digest")
        if previous_digest is not None and previous_digest != digest:
            raise HTTPException(
                status_code=409, detail="排队参数已冻结，请重新发起作业"
            )
        await async_job_svc.update_payload(
            db,
            locked_job.id,
            {
                "preannotation_request_digest": digest,
                "predict_mode": batch_request.predict_mode,
            },
        )
    from app.services.jobs_dispatch import (
        prepare_batch_prediction_dispatch,
        publish_prepared_dispatch,
    )

    await db.flush()
    dispatch_lease = await prepare_batch_prediction_dispatch(
        db, batch_request, queue=queue, soft_time_limit=soft_time_limit
    )
    # Input admission owns its existing checkpoints. Complete queue acceptance
    # freezes the exact message before the broker can receive it.
    await db.commit()
    try:
        dispatched_id = await publish_prepared_dispatch(
            db,
            dispatch_lease,
            publish=lambda: batch_dispatcher.dispatch(
                batch_request,
                queue=queue,
                soft_time_limit=soft_time_limit,
            ),
        )
    except Exception as exc:
        if precreated_job is not None:
            await async_job_svc.mark_dispatch_failed(
                db,
                precreated_job.id,
                error=f"dispatch failed: {type(exc).__name__}: {exc}",
            )
            await db.commit()
        raise
    if precreated_job is not None:
        # ``task_id`` above is the durable async job id; keep that identity even
        # if a test broker returns a different result wrapper id.
        async_job_svc.bind_execution_id(precreated_job, str(precreated_job.id))
    # B-5 · AI 预标注触发审计 — 让超管在 /audit 看到 谁/何时/对哪个 batch 跑了 AI
    await AuditService.log(
        db,
        actor=current_user,
        action="ai.preannotate.triggered",
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail={
            "job_id": str(precreated_job.id) if precreated_job else dispatched_id,
            "ml_backend_id": str(source_backend_id),
            "batch_id": str(body.batch_id) if body.batch_id else None,
            "task_count": (
                len(explicit_task_ids) if explicit_task_ids else total_tasks_hint
            ),
            "prompt": (body.prompt or "")[:200],
            # output_mode 仅文本 prompt 路径生效; 几何路径不读它, 留 None 免误导 (与 job payload 一致)
            "output_mode": body.output_mode if body.prompt else None,
            # v0.14.10 · 协议 v2 多模型路由溯源: 记录具体 model / task 类型
            # v0.14.18 · 增记 model_variants (实际 variant, 如 series/size=yolov8/l)
            "model_id": body.model_id,
            "task": body.task,
            "prompt_type": body.prompt_type,
            "model_variants": body.model_variants,
        },
    )
    await db.commit()
    # v0.20.21 · both 多阶段引导: output=both 仅文本 prompt 路径生效 (见上文 job payload 注释),
    # 且会对同一实例产出框+多边形两条几何; 若还配了下游阶段, 编排会把两条各当一个父框各处理
    # 一次 (重复裁剪/推理/富集) 并落两条 region。软提示、不拦截 (both 仍可跑)。
    warnings: list[str] = []
    if body.output_mode == "both" and body.prompt and body.pipeline_stages:
        msg = (
            "源阶段 output=both 会对同一实例产出「框 + 多边形」两条几何, 多阶段下游会各处理"
            "一次 (重复裁剪 / 推理 / 富集) 并落两条 region; 建议源阶段改用 box 或 mask。"
        )
        warnings.append(msg)
        logger.warning("[ai-pre] %s", msg)
    return {
        "job_id": str(precreated_job.id) if precreated_job else dispatched_id,
        "status": "queued",
        "total_tasks": total_tasks_hint,
        "channel": f"project:{project.id}:preannotate",
        **({"celery_task_id": dispatched_id} if precreated_job else {}),
        "warnings": warnings,
    }


@router.get("/{project_id}/orphan-tasks/preview")
async def preview_orphan_tasks(
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
):
    """v0.6.7 二修 B-10：预览本项目中「无源 task」（dataset_item_id 为空 / 指向已 unlink 的数据集 / 指向已删除 dataset_item）。"""
    from aap_labeling.task_maintenance_contract import ReadSourceLessTaskCandidates
    from app.services.labeling_task_maintenance import LabelingTaskMaintenanceService

    candidates = await LabelingTaskMaintenanceService(db).command(
        ReadSourceLessTaskCandidates(
            schema="aap-labeling-task-maintenance/v1",
            operation="read_source_less_task_candidates",
            project_id=project.id,
        ),
        flush_before=True,
    )
    orphan_task_ids = candidates.task_ids
    task_count = len(orphan_task_ids)
    ann_count = 0
    if orphan_task_ids:
        ann_count = (
            await db.execute(
                select(func.count(annotation_row.id)).where(
                    annotation_row.task_id.in_(orphan_task_ids)
                )
            )
        ).scalar() or 0
    return {"orphan_tasks": task_count, "orphan_annotations": int(ann_count)}


@router.post("/{project_id}/orphan-tasks/cleanup")
async def cleanup_orphan_tasks(
    request: Request,
    project: Project = Depends(require_project_owner),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.6.7 二修 B-10：删除本项目所有「无源 task」（含 annotations / locks / comments），重算 counters。

    适用场景：v0.6.0~v0.6.6 期间 link_project 写 dataset_item_id 但未持久化 batch 关系，
    后续数据集被 unlink / 删除留下的孤儿 task。
    """
    result = await cleanup_source_less_tasks(db, project.id)
    if result["deleted_tasks"] == 0:
        return result

    from app.services.audit import AuditService

    await AuditService.log(
        db,
        actor=current_user,
        action="project.cleanup_orphans",
        target_type="project",
        target_id=str(project.id),
        request=request,
        status_code=200,
        detail=result,
    )
    await db.commit()
    return result


# ── v0.7.3 · 项目侧关联数据集 ────────────────────────────────────────────


@router.get("/{project_id}/datasets", response_model=list[ProjectDatasetSummary])
async def list_project_datasets(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """List linked datasets after the Assets owner rechecks current project visibility."""
    result = await AssetsCatalogGateway(db).list_project_datasets(
        ListProjectDatasetsRequest(actor_id=current_user.id, project_id=project_id)
    )
    return result.datasets
