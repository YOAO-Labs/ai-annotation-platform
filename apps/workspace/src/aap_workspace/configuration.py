"""Portable orchestration for Project and ProjectTemplate configuration writes."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from typing import Any, AsyncContextManager, Protocol
from uuid import UUID

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BackendProbeInvalidRequest,
    BackendProbeUnavailable,
)
from aap_inference_contract.pipeline import (
    PipelineValidationError,
    validate_saved_pipeline,
)

from .configuration_columns import dirty_config_columns, encode_config_columns
from .configuration_contract import (
    GUIDE_OPERATIONS,
    ConfigurationError,
    ConfigurationInvalidRequest,
    ConfigurationUnavailable,
    ConfigurationRequest,
    ConfigurationGuideSuccess,
    ConfigurationStepFailure,
    configuration_guide_payload,
    configuration_guide_success_reply,
    configuration_failure_reply,
    configuration_rename_success_reply,
    configuration_rename_payload,
    configuration_payload,
    configuration_success_reply,
    decode_configuration_request,
    decode_configuration_reply,
    decode_configuration_step_reply,
)
from .guide_asset_policy import (
    MAX_GUIDE_ASSET_SIZE_BYTES,
    assert_guide_asset_upload_target,
    assert_registered_guide_asset_key,
    new_guide_asset_key,
    record_guide_asset_upload,
    remove_guide_asset,
)
from .guide_assets import GuideAssetObjectPort, guide_assets_ascii
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
from aap_workspace_contract.access import (
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
)
from .class_rename import rename_class_bindings
from .configuration_video import validate_project_collaboration_update
from .configuration_values import VideoCollaborationConfig
from aap_workspace_contract.member_values import (
    ProjectMemberRowsUnavailable,
    decode_project_member_rows,
)
from .mask_configuration_values import MaskQCConfig
from .point_quality_configuration_values import PointCloudQualityConfig
from aap_workspace_contract.project_query_contract import (
    ProjectClassRenameEvidence,
    ProjectQueryInvalidRequest,
    ProjectQueryFailure,
    ProjectQueryReply,
    ProjectQueryRequest,
    ProjectQuerySuccess,
    ProjectQueryUnavailable,
    decode_project_query_reply,
    project_query_request,
)
from aap_workspace_contract.project_query_values import (
    decode_project_row,
    project_json_value,
)

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


class ConfigurationPostCommitFailure(RuntimeError):
    """A Project refresh failed after a guide mutation already committed."""

    def __init__(self):
        super().__init__("Guide asset mutation committed but Project refresh failed")


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
    independent: bool

    def transaction(
        self, *, write: bool
    ) -> AsyncContextManager[ConfigurationTransaction]: ...


class BackendProbe(Protocol):
    async def __call__(
        self,
        registry_id: UUID,
        probe_subject: str,
        *,
        timeout: float,
        limits: CapabilityLimits,
    ) -> list[str] | None: ...


NoOpProjection = Callable[[ProjectQueryRequest], Awaitable[ProjectQueryReply]]


class ConfigurationManager:
    """Compose Workspace configuration commands over one owner transaction."""

    def __init__(
        self,
        data: ConfigurationData,
        *,
        noop_projection: NoOpProjection | None = None,
        guide_objects: GuideAssetObjectPort | None = None,
        backend_probe: BackendProbe | None = None,
        capability_limits: CapabilityLimits = CapabilityLimits(),
        video_segment_size_frames: int = 18_000,
        setup_timeout: float = 10.0,
    ) -> None:
        self.data = data
        self.noop_projection = noop_projection
        self.guide_objects = guide_objects
        self.backend_probe = backend_probe
        self.capability_limits = capability_limits
        self.video_segment_size_frames = video_segment_size_frames
        self.setup_timeout = setup_timeout

    async def execute(self, request: ConfigurationRequest | dict | bytes | str):
        request = decode_configuration_request(request)
        try:
            if request.operation in GUIDE_OPERATIONS:
                reply = await self._execute_guide(request)
            elif request.operation == "rename_project_class":
                reply = await self._execute_class_rename(request)
            else:
                async with self.data.transaction(write=True) as transaction:
                    reply = await self._execute_transaction(request, transaction)
        except ConfigurationCommandFailure as failure:
            return configuration_failure_reply(request, failure.error)
        except ConfigurationPolicyError as error:
            if (
                request.operation != "rename_project_class"
                and request.operation not in GUIDE_OPERATIONS
            ):
                raise
            return configuration_failure_reply(request, self._policy_error(error))
        return reply

    async def _execute_guide(
        self, request: ConfigurationRequest
    ) -> ConfigurationGuideSuccess:
        if request.operation not in GUIDE_OPERATIONS or request.target_id is None:
            raise ConfigurationUnavailable()
        independent = getattr(self.data, "independent", None)
        if type(independent) is not bool:
            raise ConfigurationUnavailable()

        request_json = request.model_dump(mode="json", by_alias=True)
        async with self.data.transaction(write=True) as transaction:
            if request.operation == "guide_asset_upload_init":
                data = await self._guide_upload_init(request, request_json, transaction)
            elif request.operation == "guide_asset_upload_complete":
                data = await self._guide_upload_complete(
                    request, request_json, transaction
                )
            elif request.operation == "guide_asset_delete":
                data = await self._guide_delete(request, request_json, transaction)
            elif request.operation == "guide_asset_sign":
                data = await self._guide_sign(request, request_json, transaction)
            else:
                raise ConfigurationUnavailable()

            reply = configuration_guide_success_reply(
                request,
                resource_id=request.target_id,
                data=data,
                refreshed=False,
            )
            if independent and request.operation in {
                "guide_asset_upload_init",
                "guide_asset_sign",
            }:
                rollback_on_exit = getattr(transaction, "rollback_on_exit", None)
                if type(rollback_on_exit) is not bool:
                    raise ConfigurationUnavailable()
                transaction.rollback_on_exit = True

        if independent and request.operation in {
            "guide_asset_upload_complete",
            "guide_asset_delete",
        }:
            await self.refresh_guide(request)
            reply = configuration_guide_success_reply(
                request,
                resource_id=request.target_id,
                data=data,
                refreshed=True,
            )
        return decode_configuration_reply(request, reply)

    async def _guide_upload_init(self, request, request_json, transaction):
        actor, project, _access = await self._guide_shared_authority(
            request, request_json, transaction, manager_required=True
        )
        del actor
        payload = configuration_guide_payload(request)
        key = new_guide_asset_key(
            request.target_id,
            filename=payload["filename"],
            content_type=payload["content_type"],
            size=payload["size"],
        )
        objects = self._guide_object_port()
        upload_url = await objects.generate_upload_url(
            request.target_id,
            key,
            content_type=payload["content_type"],
        )
        return {"key": key, "upload_url": upload_url, "expires_in": 900}

    async def _guide_upload_complete(self, request, request_json, transaction):
        account = await self._step(
            transaction, request, request_json, "guide_account_exists", {}
        )
        if type(account) is not dict or set(account) != {"exists"}:
            raise ConfigurationUnavailable()
        if type(account["exists"]) is not bool:
            raise ConfigurationUnavailable()
        if not account["exists"]:
            raise self._guide_inactive_account()

        first_project_facts = await self._step(
            transaction, request, request_json, "guide_read_project", {}
        )
        self._decode_guide_project(first_project_facts, request.target_id)

        actor_facts = await self._step(
            transaction, request, request_json, "guide_actor_status", {}
        )
        actor = self._decode_guide_actor_status(
            actor_facts, expected_actor_id=request.actor_id
        )

        project_facts = await self._step(
            transaction, request, request_json, "guide_read_project", {}
        )
        project = self._decode_guide_project(project_facts, request.target_id)
        await self._guide_check_manager(
            request,
            request_json,
            transaction,
            actor,
            project,
            member_action="guide_read_member",
        )

        payload = configuration_guide_payload(request)
        key = payload["key"]
        content_type = payload["content_type"]
        assert_guide_asset_upload_target(request.target_id, key, content_type)

        objects = self._guide_object_port()
        size = await objects.verify_upload(request.target_id, key)
        if size is None:
            raise ConfigurationPolicyError(400, "File not found in storage")

        actor, _project, _access = await self._guide_shared_authority(
            request, request_json, transaction, manager_required=True
        )
        locked_facts = await self._step(
            transaction, request, request_json, "guide_lock", {}
        )
        locked_project = self._decode_guide_project(locked_facts, request.target_id)

        if size <= 0 or size > MAX_GUIDE_ASSET_SIZE_BYTES:
            await objects.delete_object(request.target_id, key)
            raise ConfigurationPolicyError(400, "Invalid upload size")

        _entry, guide_assets = record_guide_asset_upload(
            request.target_id,
            project_json_value(locked_project, "guide_assets"),
            key=key,
            original_name=payload["original_name"],
            content_type=content_type,
            size=size,
        )
        await self._guide_apply(transaction, request, request_json, guide_assets)
        return _entry

    async def _guide_delete(self, request, request_json, transaction):
        _actor, _project, _access = await self._guide_shared_authority(
            request, request_json, transaction, manager_required=True
        )
        payload = configuration_guide_payload(request)
        key = payload["key"]
        locked_facts = await self._step(
            transaction, request, request_json, "guide_lock", {}
        )
        locked_project = self._decode_guide_project(locked_facts, request.target_id)
        result, guide_assets = remove_guide_asset(
            request.target_id,
            project_json_value(locked_project, "guide_assets"),
            key,
        )
        objects = self._guide_object_port()
        try:
            await objects.delete_object(request.target_id, key)
        except Exception:  # noqa: BLE001 - preserve legacy best-effort byte deletion
            pass
        await self._guide_apply(transaction, request, request_json, guide_assets)
        return result

    async def _guide_sign(self, request, request_json, transaction):
        _actor, project, _access = await self._guide_shared_authority(
            request, request_json, transaction, manager_required=False
        )
        payload = configuration_guide_payload(request)
        key = payload["key"]
        assert_registered_guide_asset_key(
            request.target_id, project_json_value(project, "guide_assets"), key
        )
        objects = self._guide_object_port()
        url = await objects.generate_download_url(request.target_id, key)
        return {"url": url, "expires_in": 3600}

    async def _guide_shared_authority(
        self,
        request,
        request_json,
        transaction,
        *,
        manager_required: bool,
    ):
        facts = await self._step(transaction, request, request_json, "guide_shared", {})
        actor, project = self._decode_guide_shared(
            facts, request.target_id, expected_actor_id=request.actor_id
        )
        manager = self._guide_manager_access(actor, project, request.target_id)
        if manager is not None:
            return actor, project, manager

        member_action = await self._step(
            transaction, request, request_json, "guide_member", {}
        )
        member = self._decode_guide_member(
            member_action, request.target_id, request.actor_id
        )
        access = self._guide_member_access(actor, request.target_id, member)
        if manager_required and not access.is_manager:
            raise ConfigurationPolicyError(403, "仅项目负责人或超级管理员可执行")
        return actor, project, access

    async def _guide_check_manager(
        self,
        request,
        request_json,
        transaction,
        actor,
        project,
        *,
        member_action: str,
    ):
        manager = self._guide_manager_access(actor, project, request.target_id)
        if manager is not None:
            return manager
        member_facts = await self._step(
            transaction, request, request_json, member_action, {}
        )
        member = self._decode_guide_member(
            member_facts, request.target_id, request.actor_id
        )
        access = self._guide_member_access(actor, request.target_id, member)
        if not access.is_manager:
            raise ConfigurationPolicyError(403, "仅项目负责人或超级管理员可执行")
        return access

    async def _guide_apply(self, transaction, request, request_json, guide_assets):
        facts = await self._step(
            transaction,
            request,
            request_json,
            "guide_apply",
            {"guide_assets_ascii": guide_assets_ascii(guide_assets)},
        )
        if facts:
            raise ConfigurationUnavailable()

    def _guide_object_port(self) -> GuideAssetObjectPort:
        if self.guide_objects is None:
            raise ConfigurationUnavailable()
        return self.guide_objects

    @staticmethod
    def _guide_inactive_account() -> ConfigurationPolicyError:
        return ConfigurationPolicyError(
            401,
            "账号已停用或不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @classmethod
    def _decode_guide_actor_status(cls, facts, *, expected_actor_id):
        if type(facts) is not dict or set(facts) != {"actor"}:
            raise ConfigurationUnavailable()
        if facts["actor"] is None:
            raise cls._guide_inactive_account()
        actor = cls._decode_guide_actor(facts["actor"])
        if actor["actor_id"] != expected_actor_id:
            raise ConfigurationUnavailable()
        return actor

    @classmethod
    def _decode_guide_shared(cls, facts, project_id, *, expected_actor_id):
        if type(facts) is not dict or set(facts) != {"actor", "row_json"}:
            raise ConfigurationUnavailable()
        actor = cls._decode_guide_actor(facts["actor"])
        if actor["actor_id"] != expected_actor_id:
            raise ConfigurationUnavailable()
        project = cls._decode_guide_project({"row_json": facts["row_json"]}, project_id)
        return actor, project

    @staticmethod
    def _decode_guide_actor(value):
        if value is None:
            raise ConfigurationPolicyError(
                401,
                "账号已停用或不存在",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if type(value) is not dict or set(value) != {
            "actor_id",
            "platform_role",
            "is_active",
        }:
            raise ConfigurationUnavailable()
        if (
            type(value["actor_id"]) is not str
            or type(value["platform_role"]) is not str
            or type(value["is_active"]) is not bool
        ):
            raise ConfigurationUnavailable()
        try:
            actor_id = UUID(value["actor_id"])
        except ValueError:
            raise ConfigurationUnavailable() from None
        if not value["is_active"]:
            raise ConfigurationPolicyError(
                401,
                "账号已停用或不存在",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {
            "actor_id": actor_id,
            "platform_role": value["platform_role"],
            "is_active": True,
        }

    @staticmethod
    def _decode_guide_project(facts, project_id):
        if type(facts) is not dict or set(facts) != {"row_json"}:
            raise ConfigurationUnavailable()
        if facts["row_json"] is not None and type(facts["row_json"]) is not str:
            raise ConfigurationUnavailable()
        try:
            project = decode_project_row(facts["row_json"], target_id=project_id)
        except ProjectQueryUnavailable:
            raise ConfigurationUnavailable() from None
        if project is None:
            raise ConfigurationPolicyError(404, "项目不存在")
        return project

    @staticmethod
    def _decode_guide_member(facts, project_id, actor_id):
        if type(facts) is not dict or set(facts) != {"rows_json"}:
            raise ConfigurationUnavailable()
        try:
            rows = decode_project_member_rows(
                facts["rows_json"],
                target_id=project_id,
                user_id=actor_id,
                max_rows=1,
            )
        except ProjectMemberRowsUnavailable:
            raise ConfigurationUnavailable() from None
        return rows[0] if rows else None

    @staticmethod
    def _guide_manager_access(actor, project, project_id):
        try:
            return project_manager_access(
                actor_id=actor["actor_id"],
                platform_role=actor["platform_role"],
                project_id=project_id,
                owner_id=(
                    UUID(project["owner_id"])
                    if project["owner_id"] is not None
                    else None
                ),
            )
        except ProjectAccessPolicyFailure as error:
            raise ConfigurationPolicyError(error.status_code, error.detail) from None

    @staticmethod
    def _guide_member_access(actor, project_id, member):
        try:
            return project_member_access(
                actor_id=actor["actor_id"],
                platform_role=actor["platform_role"],
                project_id=project_id,
                membership_id=member.id if member is not None else None,
                project_role=member.role if member is not None else None,
                membership_version=member.version if member is not None else None,
            )
        except ProjectAccessPolicyFailure as error:
            raise ConfigurationPolicyError(error.status_code, error.detail) from None

    async def refresh_guide(self, request: ConfigurationRequest) -> dict[str, Any]:
        """Read the committed Project after complete/delete without rechecking authority."""
        request = decode_configuration_request(request)
        if (
            request.operation
            not in {"guide_asset_upload_complete", "guide_asset_delete"}
            or request.target_id is None
        ):
            raise ConfigurationPostCommitFailure()
        try:
            async with self.data.transaction(write=True) as transaction:
                facts = await self._step(
                    transaction,
                    request,
                    request.model_dump(mode="json", by_alias=True),
                    "guide_refresh",
                    {},
                )
                project = self._decode_guide_project(facts, request.target_id)
                independent = getattr(self.data, "independent", None)
                if type(independent) is not bool:
                    raise ConfigurationUnavailable()
                if independent:
                    rollback_on_exit = getattr(transaction, "rollback_on_exit", None)
                    if type(rollback_on_exit) is not bool:
                        raise ConfigurationUnavailable()
                    transaction.rollback_on_exit = True
                return project
        except (
            ConfigurationUnavailable,
            ConfigurationCommandFailure,
            ConfigurationPolicyError,
            ProjectQueryUnavailable,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ):
            raise ConfigurationPostCommitFailure() from None

    async def _execute_class_rename(self, request: ConfigurationRequest):
        old_name, new_name, tool_unit_id = configuration_rename_payload(request)
        if not old_name or not new_name:
            raise ConfigurationPolicyError(400, "名称不能为空")
        if request.target_id is None:
            raise ConfigurationUnavailable()

        independent = getattr(self.data, "independent", None)
        if type(independent) is not bool:
            raise ConfigurationUnavailable()

        request_json = request.model_dump(mode="json", by_alias=True)
        async with self.data.transaction(write=True) as transaction:
            prepare = await self._step(
                transaction, request, request_json, "class_rename_prepare", {}
            )
            actor, project = self._decode_class_rename_prepare(
                prepare, actor_id=request.actor_id, project_id=request.target_id
            )
            manager = self._class_rename_manager_access(actor, project)
            if manager is None:
                member_facts = await self._step(
                    transaction, request, request_json, "class_rename_member", {}
                )
                member = self._decode_class_rename_member(
                    member_facts,
                    actor_id=request.actor_id,
                    project_id=request.target_id,
                )
                manager = self._class_rename_member_access(
                    actor, request.target_id, member
                )
            if not manager.is_manager:
                raise ConfigurationPolicyError(403, "仅项目负责人或超级管理员可执行")

            changed = old_name != new_name
            if changed:
                locked = await self._step(
                    transaction, request, request_json, "class_rename_lock", {}
                )
                project = self._decode_class_rename_locked_project(
                    locked, request.target_id
                )
                try:
                    tool_bindings = project_json_value(project, "tool_bindings")
                    renamed_bindings = rename_class_bindings(
                        tool_bindings,
                        old_name=old_name,
                        new_name=new_name,
                        tool_unit_id=tool_unit_id,
                    )
                except ProjectQueryUnavailable:
                    raise ConfigurationUnavailable() from None
                except ConfigurationPolicyError:
                    raise
                await self._apply_class_rename(
                    transaction,
                    request,
                    request_json,
                    old_name=old_name,
                    new_name=new_name,
                    tool_bindings=renamed_bindings,
                )

            evidence = self._class_rename_evidence(
                request, actor=actor, project=project
            )
            projection = None
            if not changed and independent:
                projection = await self._class_rename_noop_projection(request, evidence)
                rollback_on_exit = getattr(transaction, "rollback_on_exit", None)
                if type(rollback_on_exit) is not bool:
                    raise ConfigurationUnavailable()
                transaction.rollback_on_exit = True

            reply = configuration_rename_success_reply(
                request,
                resource_id=request.target_id,
                changed=changed,
                evidence=evidence,
                projection=projection,
            )
            return decode_configuration_reply(request, reply)

    @staticmethod
    def _decode_class_rename_prepare(
        facts: Mapping[str, Any], *, actor_id: UUID, project_id: UUID
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if type(facts) is not dict or set(facts) != {"actor", "row_json"}:
            raise ConfigurationUnavailable()
        actor = facts["actor"]
        if type(actor) is not dict or set(actor) != {
            "actor_id",
            "platform_role",
            "is_active",
        }:
            raise ConfigurationUnavailable()
        if (
            type(actor["actor_id"]) is not str
            or type(actor["platform_role"]) is not str
            or type(actor["is_active"]) is not bool
        ):
            raise ConfigurationUnavailable()
        try:
            returned_actor_id = UUID(actor["actor_id"])
        except ValueError:
            raise ConfigurationUnavailable() from None
        if returned_actor_id != actor_id:
            raise ConfigurationUnavailable()
        if not actor["is_active"]:
            raise ConfigurationCommandFailure(
                ConfigurationError(
                    401,
                    "账号已停用或不存在",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            )
        if facts["row_json"] is not None and type(facts["row_json"]) is not str:
            raise ConfigurationUnavailable()
        try:
            project = decode_project_row(facts["row_json"], target_id=project_id)
        except ProjectQueryUnavailable:
            raise ConfigurationUnavailable() from None
        if project is None:
            raise ConfigurationPolicyError(404, "项目不存在")
        return {**actor, "actor_id": returned_actor_id}, project

    @staticmethod
    def _decode_class_rename_locked_project(
        facts: Mapping[str, Any], project_id: UUID
    ) -> dict[str, Any]:
        if type(facts) is not dict or set(facts) != {"row_json"}:
            raise ConfigurationUnavailable()
        if facts["row_json"] is not None and type(facts["row_json"]) is not str:
            raise ConfigurationUnavailable()
        try:
            project = decode_project_row(facts["row_json"], target_id=project_id)
        except ProjectQueryUnavailable:
            raise ConfigurationUnavailable() from None
        if project is None:
            raise ConfigurationPolicyError(404, "项目不存在")
        return project

    @staticmethod
    def _decode_class_rename_member(
        facts: Mapping[str, Any], *, actor_id: UUID, project_id: UUID
    ):
        if type(facts) is not dict or set(facts) != {"rows_json"}:
            raise ConfigurationUnavailable()
        try:
            rows = decode_project_member_rows(
                facts["rows_json"],
                target_id=project_id,
                user_id=actor_id,
                max_rows=1,
            )
        except ProjectMemberRowsUnavailable:
            raise ConfigurationUnavailable() from None
        return rows[0] if rows else None

    @staticmethod
    def _class_rename_manager_access(actor, project):
        owner_value = project["owner_id"]
        owner_id = UUID(owner_value) if owner_value is not None else None
        try:
            return project_manager_access(
                actor_id=actor["actor_id"],
                platform_role=actor["platform_role"],
                project_id=UUID(project["id"]),
                owner_id=owner_id,
            )
        except ProjectAccessPolicyFailure as error:
            raise ConfigurationPolicyError(error.status_code, error.detail) from None

    @staticmethod
    def _class_rename_member_access(actor, project_id, member):
        try:
            return project_member_access(
                actor_id=actor["actor_id"],
                platform_role=actor["platform_role"],
                project_id=project_id,
                membership_id=member.id if member is not None else None,
                project_role=member.role if member is not None else None,
                membership_version=member.version if member is not None else None,
            )
        except ProjectAccessPolicyFailure as error:
            raise ConfigurationPolicyError(error.status_code, error.detail) from None

    @staticmethod
    def _class_rename_evidence(request, *, actor, project):
        owner_id = project["owner_id"]
        if owner_id is None:
            raise ValueError("Class rename Project owner_id is null")
        return ProjectClassRenameEvidence(
            source="class_rename",
            operation_id=request.operation_id,
            source_operation="rename_class",
            actor_id=actor["actor_id"],
            project_id=request.target_id,
            owner_id=UUID(owner_id),
            platform_role=actor["platform_role"],
            is_active=actor["is_active"],
        )

    async def _apply_class_rename(
        self,
        transaction,
        request,
        request_json,
        *,
        old_name: str,
        new_name: str,
        tool_bindings: Mapping[str, Any],
    ) -> None:
        import json

        encoded = json.dumps(
            tool_bindings,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        facts = await self._step(
            transaction,
            request,
            request_json,
            "class_rename_apply",
            {
                "old_name": old_name,
                "new_name": new_name,
                "tool_bindings_ascii": encoded,
            },
        )
        if facts:
            raise ConfigurationUnavailable()

    async def _class_rename_noop_projection(
        self,
        request: ConfigurationRequest,
        evidence: ProjectClassRenameEvidence,
    ) -> ProjectQuerySuccess:
        if self.noop_projection is None:
            raise ConfigurationUnavailable()
        try:
            projection_request = project_query_request(
                "get_project_projection",
                evidence.actor_id,
                evidence.platform_role,
                target_id=evidence.project_id,
                projection_evidence=evidence,
                request_id=request.operation_id,
            )
            raw_reply = await self.noop_projection(projection_request)
            projection = decode_project_query_reply(projection_request, raw_reply)
        except (ProjectQueryInvalidRequest, ProjectQueryUnavailable):
            raise ConfigurationCommandFailure(
                ConfigurationError(503, "项目查询服务暂不可用")
            ) from None
        if isinstance(projection, ProjectQueryFailure):
            error = projection.error
            raise ConfigurationCommandFailure(
                ConfigurationError(
                    error.status_code,
                    error.detail,
                    headers=error.headers,
                )
            )
        if not isinstance(projection, ProjectQuerySuccess):
            raise ConfigurationUnavailable()
        return projection

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
    ):
        result = await self._step(
            transaction,
            request,
            request_json,
            "enable_backend",
            {"registry_id": str(backend_id)},
        )
        pool_id = result.get("pool_id")
        if pool_id is None:
            raise ValueError(
                f"registry {backend_id} has no service pool; "
                "singleton backfill (alembic 0132) must run first"
            )
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
        if not isinstance(backend, Mapping) or set(backend) != {
            "id",
            "probe_subject",
        }:
            raise ConfigurationUnavailable()
        if type(backend.get("id")) is not str:
            raise ConfigurationUnavailable()
        try:
            fact_registry_id = UUID(backend["id"])
            requested_registry_id = UUID(str(backend_id))
        except (TypeError, ValueError):
            raise ConfigurationUnavailable() from None
        if (
            str(fact_registry_id) != backend["id"]
            or fact_registry_id != requested_registry_id
            or type(backend.get("probe_subject")) is not str
            or not backend["probe_subject"]
        ):
            raise ConfigurationUnavailable()
        if self.backend_probe is None:
            raise ConfigurationUnavailable()
        try:
            modalities = await self.backend_probe(
                fact_registry_id,
                backend["probe_subject"],
                timeout=self.setup_timeout,
                limits=self.capability_limits,
            )
        except (BackendProbeInvalidRequest, BackendProbeUnavailable):
            raise ConfigurationUnavailable() from None
        if modalities is None or not modalities:
            return
        if data_type in modalities:
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
