from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from aap_inference.pipeline import PipelineValidationError, validate_saved_pipeline
from aap_workspace.configuration_policy import (
    CLONEABLE_PROJECT_FIELDS,
    TEMPLATE_PAYLOAD_FIELDS,
    assert_project_kind_consistent,
    assert_template_creation_scope,
    can_edit_template,
    merge_from_source,
    merge_template_into_payload,
    prepare_project_create_payload,
)
from aap_workspace.configuration_values import VideoCollaborationConfig

from app.db.enums import UserRole
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.project import Project
from app.db.models.point_cloud_quality import PointCloudQualityEvaluation
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.project_workspace import (
    apply_project_update,
    assert_project_kind_update_allowed,
    create_project_record,
    create_template as create_template_command,
    delete_template as delete_template_command,
    duplicate_template as duplicate_template_command,
    increment_template_usage,
    set_point_cloud_quality_config,
    update_template as update_template_command,
)
from app.services.project_access import (
    lock_current_project_access,
    resolve_project_access_by_id,
)
from app.services.project_template import (
    assert_template_visible,
)
from app.services.project_write_guard import lock_actor_account_facts
from app.services.identity_contracts import IdentityActorFacts
from app.services.inference_limits import capability_limits_from_settings
from app.services.video_collaboration import validate_project_collaboration_update
from app.services.point_cloud_quality.evaluation import (
    prepare_evaluation_promotion,
    record_evaluation_promotion,
)
from app.services.point_cloud_quality.service import PointCloudQualityError

_MANAGER_ROLES = (UserRole.SUPER_ADMIN.value, UserRole.PROJECT_ADMIN.value)
_BUSY_SQLSTATES = {"55P03", "40001", "40P01"}


def _template_actor_values(actor: User | IdentityActorFacts) -> dict:
    return {"id": actor.id, "role": getattr(actor.role, "value", actor.role)}


class WorkspaceConfigurationWorkflow:
    """Compose project and template configuration commands in one request transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_project(
        self, payload: dict, *, actor_id: uuid.UUID
    ) -> tuple[Project, ProjectTemplate | None]:
        payload = dict(payload)
        actor = await self._current_actor(actor_id)
        self._assert_project_creator(actor)
        template_id = payload.pop("template_id", None)
        source_project_id = payload.pop("source_project_id", None)
        copy_guide = payload.pop("copy_annotation_guide", False)
        source_project: Project | None = None
        if source_project_id is not None:
            source_project, _access = await resolve_project_access_by_id(
                self.db, actor_id=actor.id, project_id=source_project_id
            )
        template: ProjectTemplate | None = None
        if template_id is not None:
            template = await self._visible_template_for_actor(template_id, actor)

        inherited_kind = source_project or template
        prepare_project_create_payload(
            payload,
            inherited_type_key=getattr(inherited_kind, "type_key", None),
            inherited_data_type=getattr(inherited_kind, "data_type", None),
        )

        if "video_collaboration" in payload:
            collaboration = VideoCollaborationConfig.model_validate(
                payload["video_collaboration"]
            )
            await validate_project_collaboration_update(
                self.db,
                None,
                collaboration,
                data_type=payload["data_type"],
                requested_video_sampling=payload.get("video_sampling"),
                requested_tool_bindings=payload.get("tool_bindings"),
            )
            if collaboration.enabled and payload["type_key"] != "video-track":
                raise HTTPException(
                    status_code=422,
                    detail="video_collaboration 只能用于 video-track 项目",
                )

        source_id = payload.pop("ml_backend_source_id", None)
        requested_main_backend_id = payload.pop("ml_backend_id", None)
        source_backend: MLBackendRegistry | None = None
        if source_id is not None:
            source_backend = await self.db.get(MLBackendRegistry, source_id)
            if source_backend is None:
                raise HTTPException(
                    status_code=400, detail="ml_backend_source_id not found"
                )

        if source_project is not None:
            payload = merge_from_source(
                payload,
                {
                    field: getattr(source_project, field)
                    for field in CLONEABLE_PROJECT_FIELDS
                },
            )
            if copy_guide:
                if source_project.annotation_guide is not None:
                    payload.setdefault(
                        "annotation_guide", source_project.annotation_guide
                    )
                if source_project.guide_assets:
                    payload.setdefault(
                        "guide_assets", copy.deepcopy(source_project.guide_assets)
                    )
            if source_backend is None and source_project.ml_backend_pool_id is not None:
                source_pool = await self.db.get(
                    MLBackendServicePool, source_project.ml_backend_pool_id
                )
                if (
                    source_pool is not None
                    and source_pool.legacy_instance_id is not None
                ):
                    source_backend = await self.db.get(
                        MLBackendRegistry, source_pool.legacy_instance_id
                    )
        elif copy_guide:
            raise HTTPException(
                status_code=400,
                detail="copy_annotation_guide requires source_project_id",
            )

        if template is not None:
            payload = merge_template_into_payload(
                {
                    **{
                        field: getattr(template, field)
                        for field in TEMPLATE_PAYLOAD_FIELDS
                    },
                    "annotation_guide": template.annotation_guide,
                },
                payload,
            )

        if requested_main_backend_id:
            await self._validate_backend_modality(
                requested_main_backend_id, payload["data_type"]
            )

        main_backend_to_enable = (
            requested_main_backend_id if source_backend is None else None
        )

        # A source backend is probed only after the project and its enablement row
        # have been staged, matching the previous transaction/IO sequence. Other
        # create paths can take their current-actor/source/template fence first.
        if source_backend is None:
            actor, source_project, template = await self._lock_create_authority(
                actor_id=actor_id,
                source_project_id=source_project_id,
                template_id=template_id,
            )

        project = await create_project_record(
            self.db, owner_id=actor.id, payload=payload
        )
        if source_backend is not None:
            pool_id = await self._enable_backend_for_new_project(
                project.id, source_backend.id
            )
            from app.services.project_workspace import set_primary_backend_binding

            set_primary_backend_binding(project=project, pool_id=pool_id)
            await self._validate_backend_modality(source_backend.id, project.data_type)
            actor, source_project, template = await self._lock_create_authority(
                actor_id=actor_id,
                source_project_id=source_project_id,
                template_id=template_id,
            )
        elif main_backend_to_enable is not None:
            pool_id = await self._enable_backend_for_new_project(
                project.id, main_backend_to_enable
            )
            from app.services.project_workspace import set_primary_backend_binding

            set_primary_backend_binding(project=project, pool_id=pool_id)

        if template is not None:
            increment_template_usage(template)
        return project, template

    async def update_project(
        self,
        project_id: uuid.UUID,
        payload: dict,
        *,
        actor_id: uuid.UUID,
    ) -> Project:
        payload = dict(payload)
        actor = await self._current_actor(actor_id)
        project, access = await resolve_project_access_by_id(
            self.db, actor_id=actor.id, project_id=project_id
        )
        self._assert_project_manager(access.is_manager)

        if (
            "video_collaboration" in payload
            or "video_sampling" in payload
            or "tool_bindings" in payload
        ):
            collaboration = VideoCollaborationConfig.model_validate(
                payload.get("video_collaboration", project.video_collaboration or {})
            )
            await validate_project_collaboration_update(
                self.db,
                project,
                collaboration,
                data_type=payload.get("data_type", project.data_type),
                video_sampling_changed=(
                    "video_sampling" in payload
                    and payload["video_sampling"] != (project.video_sampling or {})
                ),
                requested_video_sampling=payload.get(
                    "video_sampling", project.video_sampling or {}
                ),
                requested_tool_bindings=payload.get(
                    "tool_bindings", project.tool_bindings or {}
                ),
            )
            if (
                collaboration.enabled
                and payload.get("type_key", project.type_key) != "video-track"
            ):
                raise HTTPException(
                    status_code=422,
                    detail="video_collaboration 只能用于 video-track 项目",
                )

        if "mask_qc_config" in payload:
            project = await self._lock_project_configuration(project_id)
            from app.services.mask_qc.config import load_mask_qc_config

            current_config = load_mask_qc_config(project.mask_qc_config)
            requested_config = load_mask_qc_config(payload["mask_qc_config"])
            if requested_config.config_revision != current_config.config_revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "mask_qc_config_revision_conflict",
                        "expected": requested_config.config_revision,
                        "actual": current_config.config_revision,
                    },
                )
            requested_config.config_revision = current_config.config_revision + 1
            payload["mask_qc_config"] = requested_config.model_dump(mode="json")
        if "point_cloud_quality_config" in payload:
            project = await self._lock_project_configuration(project_id)
            from app.services.point_cloud_quality.config import (
                load_point_cloud_quality_config,
            )

            current_config = load_point_cloud_quality_config(
                project.point_cloud_quality_config
            )
            requested_config = load_point_cloud_quality_config(
                payload["point_cloud_quality_config"]
            )
            if requested_config.config_revision != current_config.config_revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "reason": "point_cloud_quality_config_revision_conflict",
                        "expected": requested_config.config_revision,
                        "actual": current_config.config_revision,
                    },
                )
            requested_config.config_revision = current_config.config_revision + 1
            payload["point_cloud_quality_config"] = requested_config.model_dump(
                mode="json"
            )

        if "type_key" in payload or "data_type" in payload:
            assert_project_kind_consistent(
                payload.get("type_key", project.type_key),
                payload.get("data_type", project.data_type),
            )
        await assert_project_kind_update_allowed(self.db, project, payload)
        if payload.get("preannotate_pipeline") is not None:
            try:
                payload["preannotate_pipeline"] = validate_saved_pipeline(
                    payload["preannotate_pipeline"]
                )
            except PipelineValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.detail) from exc

        main_binding_requested = "ml_backend_id" in payload
        requested_main_backend_id = (
            payload.pop("ml_backend_id") if main_binding_requested else None
        )
        if requested_main_backend_id:
            await self._validate_backend_modality(
                requested_main_backend_id,
                payload.get("data_type") or project.data_type,
            )

        actor, project, access = await lock_current_project_access(
            self.db, actor_id=actor_id, project_id=project_id
        )
        self._assert_project_manager(access.is_manager)

        if main_binding_requested:
            from app.services.project_workspace import set_primary_backend_binding

            if requested_main_backend_id:
                from app.services.ml_backend import MLBackendService

                service = MLBackendService(self.db)
                await service.set_enabled(
                    project.id, requested_main_backend_id, enabled=True
                )
                pool = await service._pool_for_registry(requested_main_backend_id)
                pool_id = pool.id if pool is not None else None
            else:
                pool_id = None
            set_primary_backend_binding(project=project, pool_id=pool_id)

        apply_project_update(project, payload)
        return project

    async def promote_point_cloud_quality_evaluation(
        self,
        *,
        project_id: uuid.UUID,
        evaluation_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> tuple[PointCloudQualityEvaluation, Project]:
        project = (
            await self.db.execute(
                select(Project).where(Project.id == project_id).with_for_update()
            )
        ).scalar_one_or_none()
        evaluation = await self.db.get(PointCloudQualityEvaluation, evaluation_id)
        if project is None or evaluation is None or evaluation.project_id != project_id:
            raise PointCloudQualityError(
                404, "point_cloud_quality_evaluation_not_found"
            )
        candidate = prepare_evaluation_promotion(
            evaluation,
            current_config=project.point_cloud_quality_config,
        )
        set_point_cloud_quality_config(
            project=project, config=candidate.model_dump(mode="json")
        )
        record_evaluation_promotion(
            evaluation,
            actor_id=actor_id,
            config_revision=candidate.config_revision,
        )
        await self.db.flush()
        return evaluation, project

    async def create_template(
        self, payload: dict, *, actor_id: uuid.UUID
    ) -> ProjectTemplate:
        actor = await self._current_actor(actor_id)
        self._assert_template_creator(actor)
        assert_template_creation_scope(
            payload.get("scope", "private"),
            payload.get("organization_id"),
            _template_actor_values(actor),
        )
        source_project_id = payload.get("source_project_id")
        source_project: Project | None = None
        if source_project_id is not None:
            actor, source_project, _access = await lock_current_project_access(
                self.db, actor_id=actor_id, project_id=source_project_id
            )
            self._assert_template_creator(actor)
        else:
            actor = await self._lock_current_actor(actor_id)
            self._assert_template_creator(actor)
        return await create_template_command(
            self.db,
            actor=actor,
            payload=dict(payload),
            source_project=source_project,
        )

    async def update_template(
        self,
        template_id: uuid.UUID,
        payload: dict,
        *,
        actor_id: uuid.UUID,
    ) -> ProjectTemplate:
        actor = await self._lock_current_actor(actor_id)
        template = await self._visible_template_for_actor(template_id, actor)
        if not can_edit_template(
            {"created_by": template.created_by}, _template_actor_values(actor)
        ):
            raise HTTPException(
                status_code=403,
                detail="仅模板创建者或超级管理员可编辑",
            )
        template = await self._lock_template(template_id)
        return await update_template_command(
            self.db, template=template, actor=actor, payload=dict(payload)
        )

    async def delete_template(
        self, template_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> None:
        actor = await self._lock_current_actor(actor_id)
        template = await self._visible_template_for_actor(template_id, actor)
        if not can_edit_template(
            {"created_by": template.created_by}, _template_actor_values(actor)
        ):
            raise HTTPException(
                status_code=403,
                detail="仅模板创建者或超级管理员可删除",
            )
        template = await self._lock_template(template_id)
        await delete_template_command(self.db, template=template, actor=actor)

    async def duplicate_template(
        self, template_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> ProjectTemplate:
        actor = await self._lock_current_actor(actor_id)
        self._assert_project_creator(actor, duplicate=True)
        await self._visible_template_for_actor(template_id, actor)
        source = await self._lock_template(template_id)
        return await duplicate_template_command(self.db, source=source, actor=actor)

    async def _enable_backend_for_new_project(
        self, project_id: uuid.UUID, backend_id: uuid.UUID
    ) -> uuid.UUID:
        from app.services.ml_backend import MLBackendService

        service = MLBackendService(self.db)
        await service.set_enabled(project_id, backend_id, enabled=True)
        pool = await service._pool_for_registry(backend_id)
        return pool.id if pool is not None else backend_id

    async def _validate_backend_modality(
        self, backend_id: uuid.UUID, data_type: str
    ) -> None:
        if data_type not in ("image", "video"):
            return
        backend = await self.db.get(MLBackendRegistry, backend_id)
        if backend is None:
            return
        from aap_inference.capabilities import derive_modalities, extract_capabilities
        from app.services.ml_client import MLBackendClient

        try:
            capabilities = extract_capabilities(
                await MLBackendClient(backend).setup(),
                limits=capability_limits_from_settings(),
            )
        except Exception:
            return
        if capabilities is None:
            return
        modalities = derive_modalities(capabilities)
        if not modalities or data_type in modalities:
            return
        raise HTTPException(
            status_code=422,
            detail=(
                f"该 ML Backend 不支持「{data_type}」模态 (检测到: "
                f"{modalities or '无'}); 视频项目需绑定自报 supported_trackers 的 backend."
            ),
        )

    async def _lock_create_authority(
        self,
        *,
        actor_id: uuid.UUID,
        source_project_id: uuid.UUID | None,
        template_id: uuid.UUID | None,
    ) -> tuple[IdentityActorFacts, Project | None, ProjectTemplate | None]:
        if source_project_id is not None:
            actor, source_project, _access = await lock_current_project_access(
                self.db, actor_id=actor_id, project_id=source_project_id
            )
        else:
            actor = await self._lock_current_actor(actor_id)
            source_project = None
        self._assert_project_creator(actor)
        template: ProjectTemplate | None = None
        if template_id is not None:
            await self._visible_template_for_actor(template_id, actor)
            template = await self._lock_template(template_id)
            await assert_template_visible(self.db, template, actor)
        return actor, source_project, template

    async def _current_actor(self, actor_id: uuid.UUID) -> User:
        actor = await self.db.get(User, actor_id, populate_existing=True)
        if actor is None or not actor.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证凭据",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor

    async def _visible_template_for_actor(
        self, template_id: uuid.UUID, actor: User | IdentityActorFacts
    ) -> ProjectTemplate:
        template = await self.db.get(
            ProjectTemplate, template_id, populate_existing=True
        )
        if template is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        await assert_template_visible(self.db, template, actor)
        return template

    async def _lock_current_actor(self, actor_id: uuid.UUID) -> IdentityActorFacts:
        actor = await lock_actor_account_facts(self.db, actor_id)
        if actor is None or not actor.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证凭据",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor

    async def _lock_project_configuration(self, project_id: uuid.UUID) -> Project:
        try:
            project = (
                await self.db.execute(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        except DBAPIError as exc:
            if self._is_busy(exc):
                raise HTTPException(
                    status_code=409, detail={"reason": "project_busy"}
                ) from exc
            raise
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return project

    async def _lock_template(self, template_id: uuid.UUID) -> ProjectTemplate:
        try:
            template = (
                await self.db.execute(
                    select(ProjectTemplate)
                    .where(ProjectTemplate.id == template_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        except DBAPIError as exc:
            if self._is_busy(exc):
                raise HTTPException(
                    status_code=409, detail={"reason": "template_busy"}
                ) from exc
            raise
        if template is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        return template

    @staticmethod
    def _is_busy(exc: DBAPIError) -> bool:
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        return sqlstate in _BUSY_SQLSTATES

    @staticmethod
    def _assert_project_creator(
        actor: User | IdentityActorFacts, *, duplicate: bool = False
    ) -> None:
        if actor.role not in _MANAGER_ROLES:
            detail = (
                "仅项目管理员或超级管理员可克隆模板"
                if duplicate
                else f"需要角色权限: {'或'.join(_MANAGER_ROLES)}"
            )
            raise HTTPException(status_code=403, detail=detail)

    @staticmethod
    def _assert_template_creator(actor: User | IdentityActorFacts) -> None:
        if actor.role not in _MANAGER_ROLES:
            raise HTTPException(
                status_code=403, detail="仅项目管理员或超级管理员可创建模板"
            )

    @staticmethod
    def _assert_project_manager(is_manager: bool) -> None:
        if not is_manager:
            raise HTTPException(
                status_code=403, detail="仅项目负责人或超级管理员可执行"
            )
