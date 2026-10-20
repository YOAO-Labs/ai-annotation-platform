"""Portable orchestration for Project and ProjectTemplate configuration writes."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, AsyncContextManager, Protocol
from uuid import UUID

from aap_inference import (
    BackendSetupLocator,
    CapabilityLimits,
    PipelineValidationError,
    derive_modalities,
    probe_backend_capabilities,
    validate_saved_pipeline,
)

from .configuration_columns import dirty_config_columns, encode_config_columns
from .configuration_contract import (
    ConfigurationError,
    ConfigurationInvalidRequest,
    ConfigurationUnavailable,
    ConfigurationRequest,
    ConfigurationStepFailure,
    configuration_failure_reply,
    configuration_payload,
    configuration_success_reply,
    decode_configuration_request,
    decode_configuration_reply,
    decode_configuration_step_reply,
)
from .configuration_policy import (
    CLONEABLE_PROJECT_FIELDS,
    TEMPLATE_PAYLOAD_FIELDS,
    ConfigurationPolicyError,
    assert_project_kind_consistent,
    assert_template_creation_scope,
    can_edit_template,
    coalesce_legacy_into_tool_bindings,
    dataset_kind,
    discard_legacy_project_configuration_fields,
    dump_project_to_template_payload,
    kind_mismatch_detail,
    merge_from_source,
    merge_template_into_payload,
    prepare_project_create_payload,
    project_kind,
    resolve_project_kind,
    scene_mode_allowed,
)
from .configuration_video import validate_project_collaboration_update
from .configuration_values import VideoCollaborationConfig
from .mask_configuration_values import MaskQCConfig
from .point_quality_configuration_values import PointCloudQualityConfig

_MANAGER_ROLES = frozenset({"super_admin", "project_admin"})
_PROJECT_CREATE_COLUMNS = frozenset(
    {
        "name",
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "ai_enabled",
        "label_config",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "rendering_config",
        "annotation_guide",
        "guide_assets",
        "due_date",
        "ai_interactive_enabled",
        "raster_mask_native_editing_enabled",
        "video_sampling",
        "video_collaboration",
        "scene_mode",
        "prefer_same_scene_continuation",
    }
)
_TEMPLATE_INSERT_COLUMNS = frozenset(
    {
        "name",
        "description",
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "label_config",
        "ai_enabled",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "rendering_config",
        "annotation_guide",
        "scope",
        "organization_id",
    }
)


class ConfigurationCommandFailure(Exception):
    """Carry a public configuration failure across the transaction boundary."""

    def __init__(self, error: ConfigurationError):
        self.error = error
        super().__init__("Configuration operation rejected")


class ConfigurationTransaction(Protocol):
    autoflush: bool

    async def step(
        self,
        request_json: dict[str, Any],
        action: str,
        args: dict[str, Any],
        columns: list[str],
        values: list[str | None],
    ) -> Any: ...

    async def before_project_insert(self) -> None: ...


class ConfigurationData(Protocol):
    def transaction(
        self, *, write: bool
    ) -> AsyncContextManager[ConfigurationTransaction]: ...


SetupProbe = Callable[..., Awaitable[dict[str, Any] | None]]


class ConfigurationManager:
    """Compose six configuration operations over one owner transaction."""

    def __init__(
        self,
        data: ConfigurationData,
        *,
        setup_probe: SetupProbe | None = None,
        capability_limits: CapabilityLimits = CapabilityLimits(),
        video_segment_size_frames: int = 18_000,
        setup_timeout: float = 10.0,
    ) -> None:
        self.data = data
        self.setup_probe = setup_probe or probe_backend_capabilities
        self.capability_limits = capability_limits
        self.video_segment_size_frames = video_segment_size_frames
        self.setup_timeout = setup_timeout

    async def execute(self, request: ConfigurationRequest | dict | bytes | str):
        request = decode_configuration_request(request)
        try:
            async with self.data.transaction(write=True) as transaction:
                reply = await self._execute_transaction(request, transaction)
        except ConfigurationCommandFailure as failure:
            return configuration_failure_reply(request, failure.error)
        return reply

    async def _execute_transaction(
        self,
        request: ConfigurationRequest,
        transaction: ConfigurationTransaction,
    ):
        request_json = request.model_dump(mode="json", by_alias=True)
        begin = await self._step(transaction, request, request_json, "begin", {})
        replay = begin.get("replay")
        if replay is not None:
            template_id = (
                request.selectors.template_id
                if request.operation == "create_project"
                else None
            )
            if (
                request.operation == "create_project"
                and (
                    self._fact_uuid(replay["template_id"])
                    if replay.get("template_id") is not None
                    else None
                )
                != template_id
            ):
                raise ConfigurationUnavailable()
            return decode_configuration_reply(
                request,
                configuration_success_reply(
                    request,
                    resource_id=self._fact_uuid(replay["resource_id"]),
                    template_id=template_id,
                ),
            )

        # This decoder verifies the canonical opaque payload and selector coherence.
        # It must remain after `begin` so hidden resources and initial authority win.
        try:
            payload = configuration_payload(request)
        except ConfigurationInvalidRequest as error:
            raise ConfigurationUnavailable() from error
        operation = request.operation
        try:
            if operation == "create_project":
                result = await self._create_project(
                    request, payload, begin, request_json, transaction
                )
            elif operation == "update_project":
                result = await self._update_project(
                    request, payload, begin, request_json, transaction
                )
            elif operation == "create_template":
                result = await self._create_template(
                    request, payload, begin, request_json, transaction
                )
            elif operation == "update_template":
                result = await self._update_template(
                    request, payload, begin, request_json, transaction
                )
            elif operation == "delete_template":
                result = await self._delete_template(
                    request, begin, request_json, transaction
                )
            elif operation == "duplicate_template":
                result = await self._duplicate_template(
                    request, begin, request_json, transaction
                )
            else:  # request decoder already closes the operation set
                raise ConfigurationUnavailable()
        except ConfigurationPolicyError as error:
            raise ConfigurationCommandFailure(self._policy_error(error)) from error

        finished = await self._step(transaction, request, request_json, "finish", {})
        if "resource_id" not in finished or "template_id" not in finished:
            raise ConfigurationUnavailable()
        resource_id = self._fact_uuid(finished["resource_id"])
        expected_resource_id = self._fact_uuid(result)
        if resource_id != expected_resource_id:
            raise ConfigurationUnavailable()
        template_id = (
            request.selectors.template_id
            if request.operation == "create_project"
            else None
        )
        returned_template_id = (
            self._fact_uuid(finished["template_id"])
            if finished["template_id"] is not None
            else None
        )
        if returned_template_id != template_id:
            raise ConfigurationUnavailable()
        return decode_configuration_reply(
            request,
            configuration_success_reply(
                request, resource_id=resource_id, template_id=template_id
            ),
        )

    async def _create_project(
        self,
        request: ConfigurationRequest,
        payload: dict[str, Any],
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        selectors = request.selectors
        actor = begin["actor"]
        self._assert_project_creator(actor)
        payload = dict(payload)
        copy_guide = payload.pop("copy_annotation_guide", False)
        payload.pop("template_id", None)
        payload.pop("source_project_id", None)

        source_project = begin.get("source_project")
        template = begin.get("template")
        inherited_kind = source_project or template
        prepare_project_create_payload(
            payload,
            inherited_type_key=_get(inherited_kind, "type_key"),
            inherited_data_type=_get(inherited_kind, "data_type"),
        )

        if "video_collaboration" in payload:
            collaboration = VideoCollaborationConfig.model_validate(
                payload["video_collaboration"]
            )
            await validate_project_collaboration_update(
                requested=collaboration,
                project=None,
                data_type=payload["data_type"],
                step=lambda action, args: self._step(
                    transaction, request, request_json, action, args
                ),
                video_segment_size_frames=self.video_segment_size_frames,
                requested_video_sampling=payload.get("video_sampling"),
                requested_tool_bindings=payload.get("tool_bindings"),
            )
            if collaboration.enabled and payload["type_key"] != "video-track":
                raise ConfigurationPolicyError(
                    422, "video_collaboration 只能用于 video-track 项目"
                )

        source_backend_id = selectors.ml_backend_source_id
        requested_main_backend_id = selectors.ml_backend_id
        source_backend: Mapping[str, Any] | None = None
        if source_backend_id is not None:
            backend_facts = await self._step(
                transaction,
                request,
                request_json,
                "backend",
                {"registry_id": str(source_backend_id)},
            )
            source_backend = backend_facts.get("backend")
            if source_backend is None:
                raise ConfigurationPolicyError(400, "ml_backend_source_id not found")

        if source_project is not None:
            source_fields = {
                field: source_project.get(field) for field in CLONEABLE_PROJECT_FIELDS
            }
            merge_from_source(payload, source_fields)
            if copy_guide:
                if source_project.get("annotation_guide") is not None:
                    payload.setdefault(
                        "annotation_guide", source_project["annotation_guide"]
                    )
                if source_project.get("guide_assets"):
                    payload.setdefault(
                        "guide_assets", copy.deepcopy(source_project["guide_assets"])
                    )
            if (
                source_backend is None
                and source_project.get("ml_backend_pool_id") is not None
            ):
                pool_result = await self._step(
                    transaction,
                    request,
                    request_json,
                    "source_pool",
                    {"pool_id": str(source_project["ml_backend_pool_id"])},
                )
                legacy_registry_id = pool_result.get("legacy_registry_id")
                if pool_result.get("found") and legacy_registry_id is not None:
                    pool_backend = await self._step(
                        transaction,
                        request,
                        request_json,
                        "backend",
                        {"registry_id": str(legacy_registry_id)},
                    )
                    source_backend = pool_backend.get("backend")
        elif copy_guide:
            raise ConfigurationPolicyError(
                400, "copy_annotation_guide requires source_project_id"
            )

        if template is not None:
            merge_template_into_payload(
                {
                    **{field: template.get(field) for field in TEMPLATE_PAYLOAD_FIELDS},
                    "annotation_guide": template.get("annotation_guide"),
                },
                payload,
            )

        if requested_main_backend_id:
            await self._validate_backend_modality(
                request,
                request_json,
                transaction,
                requested_main_backend_id,
                payload["data_type"],
            )

        if source_backend is None:
            authority = await self._step(
                transaction,
                request,
                request_json,
                "authority",
                {"autoflush": transaction.autoflush},
            )
            self._assert_project_creator(authority["actor"])

        allocated = await self._step(transaction, request, request_json, "allocate", {})
        await transaction.before_project_insert()
        insert_payload = self._select_insert_payload(
            "project", payload, _PROJECT_CREATE_COLUMNS
        )
        inserted = await self._step(
            transaction,
            request,
            request_json,
            "insert",
            {},
            insert_payload,
        )
        project_id = self._fact_uuid(inserted.get("resource_id"))
        if project_id != self._fact_uuid(allocated.get("resource_id")):
            raise ConfigurationUnavailable()

        if source_backend is not None:
            pool_id = await self._enable_backend(
                transaction,
                request,
                request_json,
                source_backend["id"],
                create_fallback=True,
            )
            await self._step(
                transaction,
                request,
                request_json,
                "binding",
                {"pool_id": None if pool_id is None else str(pool_id)},
            )
            await self._validate_backend_modality(
                request,
                request_json,
                transaction,
                source_backend["id"],
                payload["data_type"],
                backend_facts=source_backend,
            )
            authority = await self._step(
                transaction,
                request,
                request_json,
                "authority",
                {"autoflush": transaction.autoflush},
            )
            self._assert_project_creator(authority["actor"])
        elif requested_main_backend_id is not None:
            pool_id = await self._enable_backend(
                transaction,
                request,
                request_json,
                requested_main_backend_id,
                create_fallback=True,
            )
            await self._step(
                transaction,
                request,
                request_json,
                "binding",
                {"pool_id": None if pool_id is None else str(pool_id)},
            )
        return UUID(str(project_id))

    async def _update_project(
        self,
        request: ConfigurationRequest,
        payload: dict[str, Any],
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        project_id = request.target_id
        project = begin["project"]
        self._assert_project_manager(begin)
        payload = dict(payload)

        if any(
            field in payload
            for field in ("video_collaboration", "video_sampling", "tool_bindings")
        ):
            collaboration = VideoCollaborationConfig.model_validate(
                payload.get(
                    "video_collaboration", project.get("video_collaboration") or {}
                )
            )
            await validate_project_collaboration_update(
                requested=collaboration,
                project=project,
                data_type=payload.get("data_type", project.get("data_type")),
                step=lambda action, args: self._step(
                    transaction, request, request_json, action, args
                ),
                video_segment_size_frames=self.video_segment_size_frames,
                video_sampling_changed=(
                    "video_sampling" in payload
                    and payload["video_sampling"]
                    != (project.get("video_sampling") or {})
                ),
                requested_video_sampling=payload.get(
                    "video_sampling", project.get("video_sampling") or {}
                ),
                requested_tool_bindings=payload.get(
                    "tool_bindings", project.get("tool_bindings") or {}
                ),
            )
            if (
                collaboration.enabled
                and payload.get("type_key", project.get("type_key")) != "video-track"
            ):
                raise ConfigurationPolicyError(
                    422, "video_collaboration 只能用于 video-track 项目"
                )

        if "mask_qc_config" in payload:
            locked_facts = await self._step(
                transaction,
                request,
                request_json,
                "lock_config",
                {"kind": "mask_qc"},
            )
            project = locked_facts.get("project", locked_facts)
            current_config = MaskQCConfig.model_validate(
                project.get("mask_qc_config") or {}
            )
            requested_config = MaskQCConfig.model_validate(payload["mask_qc_config"])
            if requested_config.config_revision != current_config.config_revision:
                raise ConfigurationPolicyError(
                    409,
                    {
                        "reason": "mask_qc_config_revision_conflict",
                        "expected": requested_config.config_revision,
                        "actual": current_config.config_revision,
                    },
                )
            requested_config.config_revision = current_config.config_revision + 1
            payload["mask_qc_config"] = requested_config.model_dump(mode="json")

        if "point_cloud_quality_config" in payload:
            locked_facts = await self._step(
                transaction,
                request,
                request_json,
                "lock_config",
                {"kind": "point_cloud"},
            )
            project = locked_facts.get("project", locked_facts)
            current_config = PointCloudQualityConfig.model_validate(
                project.get("point_cloud_quality_config") or {}
            )
            requested_config = PointCloudQualityConfig.model_validate(
                payload["point_cloud_quality_config"]
            )
            if requested_config.config_revision != current_config.config_revision:
                raise ConfigurationPolicyError(
                    409,
                    {
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
                payload.get("type_key", project.get("type_key")),
                payload.get("data_type", project.get("data_type")),
            )
        await self._assert_project_kind_update_allowed(
            request, request_json, transaction, project, payload
        )
        if payload.get("preannotate_pipeline") is not None:
            try:
                payload["preannotate_pipeline"] = validate_saved_pipeline(
                    payload["preannotate_pipeline"]
                )
            except PipelineValidationError as error:
                raise ConfigurationPolicyError(422, error.detail) from error

        main_binding_requested = request.selectors.ml_backend_present
        requested_main_backend_id = (
            request.selectors.ml_backend_id if main_binding_requested else None
        )
        if requested_main_backend_id:
            await self._validate_backend_modality(
                request,
                request_json,
                transaction,
                requested_main_backend_id,
                payload.get("data_type") or project.get("data_type"),
            )

        authority = await self._step(
            transaction,
            request,
            request_json,
            "authority",
            {"autoflush": transaction.autoflush},
        )
        project = authority["project"]
        self._assert_project_manager(authority)

        if main_binding_requested:
            if requested_main_backend_id:
                pool_id = await self._enable_backend(
                    transaction,
                    request,
                    request_json,
                    requested_main_backend_id,
                    create_fallback=False,
                )
            else:
                pool_id = None
            await self._step(
                transaction,
                request,
                request_json,
                "binding",
                {"pool_id": None if pool_id is None else str(pool_id)},
            )

        coalesce_legacy_into_tool_bindings(
            payload,
            project.get("tool_bindings"),
            payload.get("type_key") or project.get("type_key"),
        )
        discard_legacy_project_configuration_fields(payload)
        payload.pop("ml_backend_id", None)
        comparable_project = self._comparable_values("project", project)
        changed = self._dirty_payload("project", comparable_project, payload)
        await self._step(
            transaction,
            request,
            request_json,
            "update",
            {},
            changed,
        )
        return UUID(str(project_id))

    async def _create_template(
        self,
        request: ConfigurationRequest,
        payload: dict[str, Any],
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        actor = begin["actor"]
        self._assert_template_creator(actor)
        payload = dict(payload)
        source_project_id = request.selectors.source_project_id
        scope = payload.get("scope", "private")
        organization_id = payload.get("organization_id")
        assert_template_creation_scope(
            scope, self._uuid_or_none(organization_id), _actor_values(actor)
        )

        authority = await self._step(
            transaction,
            request,
            request_json,
            "authority",
            {"autoflush": transaction.autoflush},
        )
        actor = authority["actor"]
        self._assert_template_creator(actor)
        source_project = authority.get("source_project") if source_project_id else None

        payload.pop("source_project_id", None)
        payload["type_key"], payload["data_type"] = resolve_project_kind(
            payload.get("type_key"),
            payload.get("data_type"),
            inherited_type_key=_get(source_project, "type_key"),
            inherited_data_type=_get(source_project, "data_type"),
        )
        coalesce_legacy_into_tool_bindings(payload, None, payload["type_key"])
        discard_legacy_project_configuration_fields(payload)

        if source_project_id is not None:
            source_fields = {
                field: source_project.get(field) for field in TEMPLATE_PAYLOAD_FIELDS
            }
            source_fields["annotation_guide"] = source_project.get("annotation_guide")
            merged = dump_project_to_template_payload(source_fields)
            for key, value in merged.items():
                payload.setdefault(key, value)
            payload.setdefault("name", f"{source_project['name']} 模板")
            payload.setdefault("type_label", source_project.get("type_label"))

        if (
            not payload.get("name")
            or not payload.get("type_label")
            or not payload.get("type_key")
        ):
            raise ConfigurationPolicyError(
                400,
                "name / type_label / type_key 必填 (或通过 source_project_id 兜底)",
            )

        allocated = await self._step(transaction, request, request_json, "allocate", {})
        insert_payload = self._select_insert_payload(
            "template", payload, _TEMPLATE_INSERT_COLUMNS
        )
        inserted = await self._step(
            transaction,
            request,
            request_json,
            "insert",
            {},
            insert_payload,
        )
        resource_id = self._fact_uuid(inserted.get("resource_id"))
        if resource_id != self._fact_uuid(allocated.get("resource_id")):
            raise ConfigurationUnavailable()
        return resource_id

    async def _update_template(
        self,
        request: ConfigurationRequest,
        payload: dict[str, Any],
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        actor = begin["actor"]
        template = begin["template"]
        payload = dict(payload)
        self._assert_template_editable(template, actor)

        new_scope = payload.get("scope", template.get("scope"))
        if new_scope != template.get("scope"):
            assert_template_creation_scope(
                new_scope,
                self._uuid_or_none(
                    payload.get("organization_id", template.get("organization_id"))
                ),
                _actor_values(actor),
            )
        coalesce_legacy_into_tool_bindings(
            payload,
            template.get("tool_bindings"),
            payload.get("type_key") or template.get("type_key"),
        )
        discard_legacy_project_configuration_fields(payload)
        current = self._comparable_values("template", template)
        changed = self._dirty_payload("template", current, payload)
        await self._step(
            transaction,
            request,
            request_json,
            "update",
            {},
            changed,
        )
        return UUID(str(request.target_id))

    async def _delete_template(
        self,
        request: ConfigurationRequest,
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        actor = begin["actor"]
        template = begin["template"]
        self._assert_template_editable(template, actor, deleting=True)
        deleted = await self._step(
            transaction,
            request,
            request_json,
            "delete",
            {},
        )
        return UUID(str(deleted.get("resource_id", request.target_id)))

    async def _duplicate_template(
        self,
        request: ConfigurationRequest,
        begin: Mapping[str, Any],
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
    ) -> UUID:
        actor = begin["actor"]
        source = begin["template"]
        self._assert_project_creator(actor, duplicate=True)
        payload = {
            field: copy.deepcopy(source.get(field)) for field in TEMPLATE_PAYLOAD_FIELDS
        }
        payload.update(
            {
                "name": f"{source['name']} (副本)",
                "description": source.get("description"),
                "annotation_guide": source.get("annotation_guide"),
                "scope": "private",
                "organization_id": None,
            }
        )
        allocated = await self._step(transaction, request, request_json, "allocate", {})
        insert_payload = self._select_insert_payload(
            "template", payload, _TEMPLATE_INSERT_COLUMNS
        )
        inserted = await self._step(
            transaction,
            request,
            request_json,
            "insert",
            {},
            insert_payload,
        )
        resource_id = self._fact_uuid(inserted.get("resource_id"))
        if resource_id != self._fact_uuid(allocated.get("resource_id")):
            raise ConfigurationUnavailable()
        return resource_id

    async def _enable_backend(
        self,
        transaction: ConfigurationTransaction,
        request: ConfigurationRequest,
        request_json: dict[str, Any],
        backend_id: Any,
        *,
        create_fallback: bool,
    ):
        result = await self._step(
            transaction,
            request,
            request_json,
            "enable_backend",
            {"registry_id": str(backend_id)},
        )
        pool_id = result.get("pool_id")
        if pool_id is None and create_fallback:
            return backend_id
        return pool_id

    async def _validate_backend_modality(
        self,
        request: ConfigurationRequest,
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
        backend_id: Any,
        data_type: str,
        *,
        backend_facts: Mapping[str, Any] | None = None,
    ) -> None:
        if data_type not in ("image", "video"):
            return
        if backend_facts is None:
            result = await self._step(
                transaction,
                request,
                request_json,
                "backend",
                {"registry_id": str(backend_id)},
            )
            backend = result.get("backend")
        else:
            backend = backend_facts
        if backend is None:
            return
        locator = BackendSetupLocator(
            id=UUID(str(backend["id"])),
            url=backend["url"],
            auth_method=backend["auth_method"],
            auth_token=backend.get("auth_token"),
            extra_params=backend.get("extra_params") or {},
        )
        capabilities = await self.setup_probe(
            locator,
            timeout=self.setup_timeout,
            limits=self.capability_limits,
        )
        if capabilities is None:
            return
        modalities = derive_modalities(capabilities)
        if not modalities or data_type in modalities:
            return
        raise ConfigurationPolicyError(
            422,
            (
                f"该 ML Backend 不支持「{data_type}」模态 (检测到: "
                f"{modalities or '无'}); 视频项目需绑定自报 supported_trackers 的 backend."
            ),
        )

    async def _assert_project_kind_update_allowed(
        self,
        request: ConfigurationRequest,
        request_json: dict[str, Any],
        transaction: ConfigurationTransaction,
        project: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        if "scene_mode" in payload and payload["scene_mode"] != project.get(
            "scene_mode"
        ):
            if "total_tasks" not in project:
                raise ConfigurationUnavailable()
            if int(project["total_tasks"]) > 0:
                raise ConfigurationPolicyError(
                    422,
                    "已建 task 的项目不可切换 scene 模式,请先解绑数据集",
                )

        target_data_type = payload.get("data_type", project.get("data_type"))
        target_scene_mode = bool(payload.get("scene_mode", project.get("scene_mode")))
        if target_scene_mode and not scene_mode_allowed(target_data_type):
            raise ConfigurationPolicyError(422, "scene_mode 仅支持 image/lidar 项目")

        if "data_type" not in payload and "scene_mode" not in payload:
            return
        datasets = await self._step(transaction, request, request_json, "datasets", {})
        target = project_kind(target_data_type, target_scene_mode)
        for dataset in datasets["datasets"]:
            scene = await self._step(
                transaction,
                request,
                request_json,
                "dataset_scenes",
                {"dataset_id": str(dataset["id"])},
            )
            mismatch = kind_mismatch_detail(
                target,
                dataset_kind(dataset["data_type"], bool(scene["has_scenes"])),
            )
            if mismatch:
                raise ConfigurationPolicyError(
                    422,
                    (
                        f"已有数据集 {dataset['display_id']} 与目标项目 kind 不匹配: "
                        f"{mismatch}; 请先解绑数据集"
                    ),
                )

    async def _step(
        self,
        transaction: ConfigurationTransaction,
        request: ConfigurationRequest,
        request_json: dict[str, Any],
        action: str,
        args: dict[str, Any],
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        columns: list[str] = []
        values: list[str | None] = []
        if payload is not None:
            resource_kind = (
                "project"
                if request.operation in {"create_project", "update_project"}
                else "template"
            )
            columns, values = encode_config_columns(resource_kind, payload)
        raw = await transaction.step(request_json, action, args, columns, values)
        native = decode_configuration_step_reply(request, action, raw)
        if isinstance(native, ConfigurationStepFailure):
            error = native.error
            raise ConfigurationCommandFailure(
                ConfigurationError(
                    status_code=error.status_code,
                    detail=error.detail,
                    headers=error.headers,
                    rollback=error.rollback,
                )
            )
        return native.facts

    @staticmethod
    def _select_insert_payload(
        resource_kind: str,
        payload: Mapping[str, Any],
        allowed: frozenset[str],
    ) -> dict[str, Any]:
        selected = {key: value for key, value in payload.items() if key in allowed}
        if resource_kind == "project":
            # Explicit nulls on creation historically let the database defaults win.
            for field in ("box_threshold", "text_threshold"):
                if selected.get(field, object()) is None:
                    selected.pop(field)
        return selected

    @staticmethod
    def _comparable_values(
        resource_kind: str, facts: Mapping[str, Any]
    ) -> dict[str, Any]:
        current = dict(facts)
        if resource_kind == "project" and isinstance(current.get("due_date"), str):
            current["due_date"] = date.fromisoformat(current["due_date"])
        if resource_kind == "template" and current.get("organization_id") is not None:
            current["organization_id"] = UUID(str(current["organization_id"]))
        return current

    @classmethod
    def _dirty_payload(
        cls,
        resource_kind: str,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        comparable_payload = dict(payload)
        if resource_kind == "project" and isinstance(
            comparable_payload.get("due_date"), str
        ):
            comparable_payload["due_date"] = date.fromisoformat(
                comparable_payload["due_date"]
            )
        if (
            resource_kind == "template"
            and comparable_payload.get("organization_id") is not None
        ):
            comparable_payload["organization_id"] = UUID(
                str(comparable_payload["organization_id"])
            )
        dirty = dirty_config_columns(current, comparable_payload)
        return {field: payload[field] for field in dirty}

    @staticmethod
    def _policy_error(error: ConfigurationPolicyError) -> ConfigurationError:
        return ConfigurationError(
            status_code=error.status_code,
            detail=error.detail,
            headers=error.headers,
            rollback=False,
        )

    @staticmethod
    def _assert_project_creator(
        actor: Mapping[str, Any], *, duplicate: bool = False
    ) -> None:
        if actor.get("role") in _MANAGER_ROLES:
            return
        detail = (
            "仅项目管理员或超级管理员可克隆模板"
            if duplicate
            else "需要角色权限: super_admin或project_admin"
        )
        raise ConfigurationPolicyError(403, detail)

    @staticmethod
    def _assert_template_creator(actor: Mapping[str, Any]) -> None:
        if actor.get("role") not in _MANAGER_ROLES:
            raise ConfigurationPolicyError(403, "仅项目管理员或超级管理员可创建模板")

    @staticmethod
    def _assert_project_manager(authority: Mapping[str, Any]) -> None:
        project = authority.get("project") or {}
        is_manager = authority.get("is_manager", project.get("is_manager", False))
        if not is_manager:
            raise ConfigurationPolicyError(403, "仅项目负责人或超级管理员可执行")

    @staticmethod
    def _assert_template_editable(
        template: Mapping[str, Any], actor: Mapping[str, Any], *, deleting: bool = False
    ) -> None:
        if not can_edit_template(
            {"created_by": _uuid_or_value(template.get("created_by"))},
            _actor_values(actor),
        ):
            action = "删除" if deleting else "编辑"
            raise ConfigurationPolicyError(403, f"仅模板创建者或超级管理员可{action}")

    @staticmethod
    def _uuid_or_none(value: Any) -> UUID | None:
        if value is None:
            return None
        return UUID(str(value))

    @staticmethod
    def _fact_uuid(value: Any) -> UUID:
        try:
            return value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise ConfigurationUnavailable() from None


def _actor_values(actor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _uuid_or_value(actor.get("id")),
        "role": actor.get("role"),
    }


def _uuid_or_value(value: Any) -> Any:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return value


def _get(value: Mapping[str, Any] | None, key: str) -> Any:
    return None if value is None else value.get(key)
