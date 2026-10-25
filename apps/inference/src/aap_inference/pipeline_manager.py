"""Portable orchestration for named ProjectPipeline commands and reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
import json
from typing import Any, Protocol
import uuid

from aap_workspace_contract.access import (
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
)
from aap_workspace_contract.member_values import (
    ProjectMemberRowsUnavailable,
    decode_project_member_rows,
)
from aap_workspace_contract.project_query_contract import (
    ProjectQueryRejected,
    ProjectQueryUnavailable,
    decode_project_query_step,
    project_query_request,
)
from aap_workspace_contract.project_query_values import decode_project_row
from aap_workspace_contract.project_read_authority import accessible_project_row
from aap_workspace_contract.wire import _reject_constant, _unique_object

from .pipeline import PipelineValidationError, validate_saved_pipeline
from .pipeline_contract import (
    PIPELINE_PROTOCOL,
    PipelineCommandRequest,
    PipelineErrorDetails,
    PipelineFailure,
    PipelinePostCommitFailure,
    PipelineQueryFailure,
    PipelineQueryRequest,
    PipelineQuerySuccess,
    PipelineRejected,
    PipelineResult,
    PipelineRow,
    PipelineStatusFailure,
    PipelineStatusRequest,
    PipelineStatusSuccess,
    PipelineStepFailure,
    PipelineSuccess,
    PipelineUnavailable,
    decode_pipeline_command_request,
    decode_pipeline_query_reply,
    decode_pipeline_query_request,
    decode_pipeline_reply,
    decode_pipeline_status_reply,
    decode_pipeline_status_request,
    decode_pipeline_step_reply,
)
from .pipeline_policy import (
    assert_can_create_pipeline_scope,
    assert_pipeline_creator_role,
    assert_pipeline_visible,
    can_edit_pipeline,
    copy_pipeline_stages,
    validate_pipeline_scope_owner,
)
from .pipeline_values import (
    decode_pipeline_row,
    decode_pipeline_rows,
    dirty_pipeline_payload,
    encode_pipeline_columns,
)


class PipelineTransaction(Protocol):
    """One borrowed or owner-managed transaction for a pipeline operation."""

    autoflush: bool
    owns_commit: bool

    async def step(
        self,
        request: Any,
        action: str,
        args: Mapping[str, Any] | None = None,
        columns: Sequence[str] = (),
        values: Sequence[str | None] = (),
    ) -> Any: ...


class PipelineData(Protocol):
    def transaction(
        self, *, readonly: bool = False
    ) -> AbstractAsyncContextManager[PipelineTransaction]: ...


class _CommandRollback(Exception):
    def __init__(self, error: PipelineRejected) -> None:
        self.error = error
        super().__init__("Pipeline command rejected")


def _exact_dict(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise PipelineUnavailable()
    return value


def _uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if type(value) is not str:
        raise PipelineUnavailable()
    try:
        return uuid.UUID(value)
    except ValueError:
        raise PipelineUnavailable() from None


def _json_object(value: Any) -> dict[str, Any]:
    try:
        if type(value) is str:
            parsed = _transport_json(value)
        else:
            parsed = value
    except (UnicodeError, TypeError):
        raise PipelineUnavailable() from None
    if type(parsed) is not dict:
        raise PipelineUnavailable()
    return parsed


def _transport_json(value: str) -> Any:
    try:
        return json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError:
        raise PipelineUnavailable() from None
    except ValueError as error:
        message = str(error)
        if message == "Duplicate JSON field" or message.startswith(
            "Invalid JSON number:"
        ):
            raise PipelineUnavailable() from None
        # Oversized stored JSON integers keep the old driver's generic500 phase.
        raise
    except (TypeError, UnicodeError, RecursionError):
        raise PipelineUnavailable() from None


class PipelineManager:
    """Apply legacy pipeline policy over a fixed owner-data transaction port."""

    def __init__(self, data: PipelineData) -> None:
        self.data = data

    async def execute(
        self, command: PipelineCommandRequest | dict | bytes | str
    ) -> PipelineSuccess | PipelineFailure:
        request = decode_pipeline_command_request(command)
        owns_commit: bool | None = None
        body_completed = False
        try:
            async with self.data.transaction(readonly=False) as transaction:
                owns_commit = self._owns_commit(transaction)
                try:
                    resource_id = await self._execute_command(transaction, request)
                except PipelineRejected as error:
                    raise _CommandRollback(error) from error
                except (TypeError, ValueError, OverflowError) as error:
                    # Stored JSON null/scalars and driver-decoded values can
                    # retain legacy plain-500 failures during command phases.
                    raise _CommandRollback(
                        self._legacy_materialization_failure()
                    ) from error
                body_completed = True
        except _CommandRollback as rollback:
            return self._command_failure(
                request,
                rollback.error,
                commit_state=(
                    "caller_pending" if owns_commit is False else "rolled_back"
                ),
            )
        except PipelineUnavailable:
            # Only a failure during transaction exit can be an uncertain commit.
            # Entry/step failures retain their established unavailable phase.
            if body_completed and owns_commit is True:
                return self._command_failure(
                    request,
                    PipelineRejected(
                        503,
                        "项目编排操作结果暂不可确认",
                        rollback=False,
                    ),
                    commit_state="unknown",
                )
            raise
        except PipelineRejected as error:
            # Acknowledged commit/rollback failures from an owner transaction
            # are known outcomes. Transport uncertainty remains PipelineUnavailable.
            if owns_commit is True and error.rollback:
                return self._command_failure(request, error, commit_state="rolled_back")
            raise

        commit_state = "committed" if owns_commit else "caller_pending"
        return self._command_success(request, resource_id, commit_state)

    async def query(
        self, query: PipelineQueryRequest | dict | bytes | str
    ) -> PipelineQuerySuccess | PipelineQueryFailure:
        request = decode_pipeline_query_request(query)
        try:
            async with self.data.transaction(readonly=True) as transaction:
                if request.operation == "list_pipelines":
                    rows = await self._list_pipelines(transaction, request)
                elif request.operation == "get_pipeline":
                    row = await self._get_visible_pipeline(transaction, request)
                    rows = [row]
                elif request.operation == "get_pipeline_projection":
                    row = await self._get_accepted_projection(transaction, request)
                    rows = [row]
                else:  # The closed request decoder should make this unreachable.
                    raise PipelineUnavailable()
        except PipelineRejected as error:
            return self._query_failure(request, error)
        except (TypeError, ValueError, OverflowError):
            return self._query_failure(
                request, self._legacy_materialization_failure(rollback=False)
            )
        return decode_pipeline_query_reply(
            request,
            PipelineQuerySuccess(
                schema=PIPELINE_PROTOCOL,
                ok=True,
                request_id=request.request_id,
                operation=request.operation,
                actor_id=request.admitted_actor.id,
                target_id=request.target_id,
                rows=rows,
            ),
        )

    async def status(
        self, status: PipelineStatusRequest | dict | bytes | str
    ) -> PipelineStatusSuccess | PipelineStatusFailure:
        request = decode_pipeline_status_request(status)
        try:
            async with self.data.transaction(readonly=True) as transaction:
                facts = await self._step(transaction, request, "status", {})
        except PipelineRejected as error:
            return self._status_failure(request, error)
        facts = _exact_dict(facts, {"state", "resource_id"})
        state = facts["state"]
        if state not in {"committed", "unknown"}:
            raise PipelineUnavailable()
        resource_id = facts["resource_id"]
        if state == "committed":
            resource_id = _uuid(resource_id)
        elif resource_id is not None:
            raise PipelineUnavailable()
        return decode_pipeline_status_reply(
            request,
            PipelineStatusSuccess(
                schema=PIPELINE_PROTOCOL,
                ok=True,
                request_id=request.request_id,
                operation=request.operation,
                operation_id=request.command.operation_id,
                command_operation=request.command.operation,
                actor_id=request.command.admitted_actor.id,
                target_id=request.command.target_id,
                state=state,
                resource_id=resource_id,
            ),
        )

    @staticmethod
    def _owns_commit(transaction: PipelineTransaction) -> bool:
        value = getattr(transaction, "owns_commit", None)
        if type(value) is not bool:
            raise PipelineUnavailable()
        return value

    async def _execute_command(
        self, transaction: PipelineTransaction, request: PipelineCommandRequest
    ) -> uuid.UUID:
        begin = _exact_dict(
            await self._step(transaction, request, "begin", {}), {"replay"}
        )
        replay = begin["replay"]
        if replay is not None:
            replay = _exact_dict(replay, {"resource_id"})
            return _uuid(replay["resource_id"])

        if request.operation == "create_pipeline":
            resource_id = await self._create_pipeline(transaction, request)
        elif request.operation == "update_pipeline":
            resource_id = await self._update_pipeline(transaction, request)
        elif request.operation == "delete_pipeline":
            resource_id = await self._delete_pipeline(transaction, request)
        elif request.operation == "apply_pipeline":
            resource_id = await self._apply_pipeline(transaction, request)
        else:
            raise PipelineUnavailable()

        finished = _exact_dict(
            await self._step(
                transaction, request, "finish", {"resource_id": str(resource_id)}
            ),
            {"resource_id"},
        )
        if _uuid(finished["resource_id"]) != resource_id:
            raise PipelineUnavailable()
        return resource_id

    async def _create_pipeline(
        self, transaction: PipelineTransaction, request: PipelineCommandRequest
    ) -> uuid.UUID:
        actor = request.admitted_actor
        assert_pipeline_creator_role(actor)
        payload = self._command_payload(
            request,
            required={
                "name",
                "scope",
                "project_id",
                "organization_id",
                "stages",
                "is_default",
            },
        )
        scope = payload["scope"]
        project_id = self._optional_uuid(payload["project_id"])
        organization_id = self._optional_uuid(payload["organization_id"])

        await assert_can_create_pipeline_scope(
            scope,
            organization_id,
            actor,
            lambda: self._organization_ids(transaction, request, actor.id),
        )
        if project_id is not None:
            await self._assert_project_visible(transaction, request, project_id)

        try:
            stages = validate_saved_pipeline(payload["stages"])
        except PipelineValidationError as error:
            raise PipelineRejected(422, error.detail) from error

        make_default = payload["is_default"]
        if type(make_default) is not bool:
            raise PipelineUnavailable()
        if make_default and project_id is not None:
            await self._default_clear(transaction, request, project_id)

        resource_id = uuid.uuid4()
        insert_payload = {
            "id": resource_id,
            "scope": scope,
            "project_id": project_id,
            "organization_id": organization_id,
            "name": payload["name"],
            "stages": stages,
            "is_default": make_default,
            "created_by": actor.id,
        }
        columns, values = encode_pipeline_columns(insert_payload)
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_insert",
                {},
                columns=columns,
                values=values,
            ),
            {"resource_id"},
        )
        if _uuid(facts["resource_id"]) != resource_id:
            raise PipelineUnavailable()
        return resource_id

    async def _update_pipeline(
        self, transaction: PipelineTransaction, request: PipelineCommandRequest
    ) -> uuid.UUID:
        target_id = request.target_id
        if target_id is None:
            raise PipelineUnavailable()
        current = await self._get_pipeline(transaction, request, target_id)
        actor = request.admitted_actor

        async def org_reader() -> list[uuid.UUID]:
            return await self._organization_ids(transaction, request, actor.id)

        await assert_pipeline_visible(current, actor, org_reader)
        if not await can_edit_pipeline(current, actor, org_reader):
            raise PipelineRejected(403, "无权编辑该编排")

        payload = self._command_payload(
            request,
            allowed=set(
                (
                    "name",
                    "scope",
                    "project_id",
                    "organization_id",
                    "stages",
                    "is_default",
                )
            ),
        )
        current_map = current.model_dump(mode="python")
        scope = payload.get("scope", current.scope)
        project_id = self._optional_uuid(payload.get("project_id", current.project_id))
        organization_id = self._optional_uuid(
            payload.get("organization_id", current.organization_id)
        )
        try:
            validate_pipeline_scope_owner(scope, project_id, organization_id)
        except ValueError as error:
            raise PipelineRejected(422, str(error)) from error

        await assert_can_create_pipeline_scope(
            scope,
            organization_id,
            actor,
            lambda: self._organization_ids(transaction, request, actor.id),
        )
        if project_id is not None:
            await self._assert_project_visible(transaction, request, project_id)

        if payload.get("stages") is not None:
            try:
                payload["stages"] = validate_saved_pipeline(payload["stages"])
            except PipelineValidationError as error:
                raise PipelineRejected(422, error.detail) from error

        if (
            project_id is not None
            and project_id != current.project_id
            and "is_default" not in payload
        ):
            payload["is_default"] = False
        next_is_default = payload.get("is_default", current.is_default)
        if scope != "private" and next_is_default:
            raise PipelineRejected(422, "只有 private 项目编排可以设为默认")

        switch_default = (
            bool(next_is_default)
            and project_id is not None
            and (payload.get("is_default") is True or project_id != current.project_id)
        )
        comparison_map = current_map
        if switch_default:
            await self._default_clear(transaction, request, project_id)
            if current.project_id == project_id and current.is_default:
                # SQLAlchemy's evaluate synchronization changes the loaded row
                # before the service assigns the explicit default back to it.
                comparison_map = {**current_map, "is_default": False}

        dirty = dirty_pipeline_payload(comparison_map, payload)
        # The old service flushes unconditionally after assigning update fields.
        # Empty arrays preserve that phase without issuing a data UPDATE.
        columns, values = encode_pipeline_columns(dirty)
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_update",
                {"pipeline_id": str(target_id)},
                columns=columns,
                values=values,
            ),
            {"resource_id"},
        )
        if _uuid(facts["resource_id"]) != target_id:
            raise PipelineUnavailable()
        return target_id

    async def _delete_pipeline(
        self, transaction: PipelineTransaction, request: PipelineCommandRequest
    ) -> uuid.UUID:
        target_id = request.target_id
        if target_id is None:
            raise PipelineUnavailable()
        current = await self._get_pipeline(transaction, request, target_id)
        actor = request.admitted_actor

        async def org_reader() -> list[uuid.UUID]:
            return await self._organization_ids(transaction, request, actor.id)

        await assert_pipeline_visible(current, actor, org_reader)
        if not await can_edit_pipeline(current, actor, org_reader):
            raise PipelineRejected(403, "无权删除该编排")
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_delete",
                {"pipeline_id": str(target_id)},
            ),
            {"resource_id"},
        )
        if _uuid(facts["resource_id"]) != target_id:
            raise PipelineUnavailable()
        return target_id

    async def _apply_pipeline(
        self, transaction: PipelineTransaction, request: PipelineCommandRequest
    ) -> uuid.UUID:
        project_id = request.target_id
        if project_id is None:
            raise PipelineUnavailable()
        payload = self._command_payload(
            request, required={"pipeline_id", "set_default"}
        )
        pipeline_id = self._payload_uuid(payload["pipeline_id"])
        if type(payload["set_default"]) is not bool:
            raise PipelineUnavailable()
        source = await self._get_pipeline(transaction, request, pipeline_id)
        await assert_pipeline_visible(
            source,
            request.admitted_actor,
            lambda: self._organization_ids(
                transaction, request, request.admitted_actor.id
            ),
        )

        try:
            canonical_stages = validate_saved_pipeline(source.stages)
        except PipelineValidationError as error:
            raise PipelineRejected(422, error.detail) from error

        missing = await self._unenabled_backend_ids(
            transaction, request, project_id, source.stages
        )
        if missing:
            raise PipelineRejected(
                422,
                {
                    "message": "编排引用的 backend 未在当前项目启用",
                    "unenabled_backends": missing,
                },
            )

        await self._assert_project_manager(transaction, request, project_id)

        await self._step(
            transaction,
            request,
            "source_usage_increment",
            {"source_pipeline_id": str(pipeline_id)},
        )
        if payload["set_default"]:
            await self._default_clear(transaction, request, project_id)

        resource_id = uuid.uuid4()
        insert_payload = {
            "id": resource_id,
            "scope": "private",
            "project_id": project_id,
            "organization_id": None,
            "name": source.name,
            "stages": copy_pipeline_stages(canonical_stages),
            "is_default": payload["set_default"],
            "created_by": request.admitted_actor.id,
        }
        columns, values = encode_pipeline_columns(insert_payload)
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_insert",
                {},
                columns=columns,
                values=values,
            ),
            {"resource_id"},
        )
        if _uuid(facts["resource_id"]) != resource_id:
            raise PipelineUnavailable()
        return resource_id

    async def _list_pipelines(
        self, transaction: PipelineTransaction, request: PipelineQueryRequest
    ) -> list[PipelineRow]:
        filters = request.filters
        if filters is None:
            raise PipelineUnavailable()
        organization_ids = []
        if request.admitted_actor.role != "super_admin":
            organization_ids = await self._organization_ids(
                transaction, request, request.admitted_actor.id
            )
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_list",
                {
                    "scope": filters.scope,
                    "project_id": self._uuid_text(filters.project_id),
                    "organization_id": self._uuid_text(filters.organization_id),
                    "organization_ids": [str(value) for value in organization_ids],
                },
            ),
            {"rows_json"},
        )
        try:
            return decode_pipeline_rows(facts["rows_json"])
        except (ValueError, OverflowError) as error:
            raise self._legacy_materialization_failure(rollback=False) from error

    async def _get_visible_pipeline(
        self, transaction: PipelineTransaction, request: PipelineQueryRequest
    ) -> PipelineRow:
        target_id = request.target_id
        if target_id is None:
            raise PipelineUnavailable()
        row = await self._get_pipeline(transaction, request, target_id)
        await assert_pipeline_visible(
            row,
            request.admitted_actor,
            lambda: self._organization_ids(
                transaction, request, request.admitted_actor.id
            ),
        )
        return row

    async def _get_accepted_projection(
        self, transaction: PipelineTransaction, request: PipelineQueryRequest
    ) -> PipelineRow:
        target_id = request.target_id
        evidence = request.evidence
        if target_id is None or evidence is None:
            raise PipelineUnavailable()
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_get",
                {"pipeline_id": str(target_id)},
            ),
            {"row_json"},
        )
        try:
            row = decode_pipeline_row(facts["row_json"], target_id=target_id)
        except (ValueError, OverflowError):
            raise PipelinePostCommitFailure() from None
        if row is None:
            raise PipelinePostCommitFailure()
        return row

    async def _get_pipeline(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest | PipelineQueryRequest,
        pipeline_id: uuid.UUID,
    ) -> PipelineRow:
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "pipeline_get",
                {"pipeline_id": str(pipeline_id)},
            ),
            {"row_json"},
        )
        try:
            row = decode_pipeline_row(facts["row_json"], target_id=pipeline_id)
        except (ValueError, OverflowError) as error:
            raise self._legacy_materialization_failure(
                rollback=isinstance(request, PipelineCommandRequest)
            ) from error
        if row is None:
            raise PipelineRejected(404, "编排不存在")
        return row

    async def _organization_ids(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest | PipelineQueryRequest,
        actor_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "organization_ids",
                {"actor_id": str(actor_id)},
            ),
            {"organization_ids"},
        )
        values = facts["organization_ids"]
        if type(values) is not list:
            raise PipelineUnavailable()
        return [_uuid(value) for value in values]

    async def _assert_project_visible(
        self,
        transaction: PipelineTransaction,
        command: PipelineCommandRequest,
        project_id: uuid.UUID,
    ) -> None:
        request = project_query_request(
            "require_project_visible",
            command.admitted_actor.id,
            command.admitted_actor.role,
            target_id=project_id,
            request_id=command.operation_id,
        )

        async def step(child_request, _transaction, action):
            facts = _exact_dict(
                await self._step(
                    transaction,
                    command,
                    "project_query",
                    {
                        "request": child_request.model_dump(mode="json", by_alias=True),
                        "action": action,
                    },
                ),
                {"workspace_reply"},
            )
            return decode_project_query_step(
                child_request, action, None, facts["workspace_reply"]
            ).facts

        try:
            await accessible_project_row(request, transaction, step)
        except ProjectQueryRejected as error:
            details = error.error
            raise PipelineRejected(
                details.status_code,
                details.detail,
                headers=details.headers,
            ) from error
        except ProjectQueryUnavailable as error:
            raise PipelineUnavailable() from error
        except (ValueError, OverflowError) as error:
            raise self._legacy_materialization_failure() from error

    async def _assert_project_manager(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest,
        project_id: uuid.UUID,
    ) -> None:
        actor_id = request.admitted_actor.id
        actor: dict[str, Any] | None = None
        project: dict[str, Any] | None = None

        actor_facts = _exact_dict(
            await self._project_access_step(
                transaction, request, project_id, "lock_actor_share"
            ),
            {"actor"},
        )
        raw_actor = actor_facts["actor"]
        if raw_actor is not None:
            actor = _exact_dict(raw_actor, {"actor_id", "platform_role", "is_active"})
            if (
                type(actor["actor_id"]) is not str
                or _uuid(actor["actor_id"]) != actor_id
                or type(actor["platform_role"]) is not str
                or type(actor["is_active"]) is not bool
            ):
                raise PipelineUnavailable()
            actor = dict(actor)
        if actor is None or actor["is_active"] is not True:
            raise PipelineRejected(
                401,
                "账号已停用或不存在",
                headers={"WWW-Authenticate": "Bearer"},
            )

        project_facts = _exact_dict(
            await self._project_access_step(
                transaction, request, project_id, "lock_project_key_share"
            ),
            {"row_json"},
        )
        project = self._decode_locked_project(project_facts["row_json"], project_id)
        manager = self._manager_access(actor, project, project_id)
        if manager is not None:
            return

        member_facts = _exact_dict(
            await self._project_access_step(
                transaction, request, project_id, "lock_member_share"
            ),
            {"rows_json"},
        )
        try:
            members = decode_project_member_rows(
                member_facts["rows_json"],
                target_id=project_id,
                user_id=actor_id,
                max_rows=1,
            )
        except ProjectMemberRowsUnavailable as error:
            raise PipelineUnavailable() from error
        except OverflowError as error:
            raise self._legacy_materialization_failure() from error
        member = members[0] if members else None
        access = self._member_access(actor, project_id, member)
        if not access.is_manager:
            raise PipelineRejected(403, "仅项目负责人或超级管理员可执行")

    async def _project_access_step(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest,
        project_id: uuid.UUID,
        phase: str,
    ) -> dict[str, Any]:
        if project_id != request.target_id or phase not in {
            "lock_actor_share",
            "lock_project_key_share",
            "lock_member_share",
        }:
            raise PipelineUnavailable()
        facts = _exact_dict(
            await self._step(transaction, request, "project_access", {"phase": phase}),
            {"workspace_reply"},
        )
        reply = _json_object(facts["workspace_reply"])
        if set(reply) != {"schema", "actor_id", "project_id", "phase", "facts"}:
            raise PipelineUnavailable()
        if (
            reply["schema"] != "aap-workspace-project-access-step/v1"
            or _uuid(reply["actor_id"]) != request.admitted_actor.id
            or _uuid(reply["project_id"]) != project_id
            or reply["phase"] != phase
            or type(reply["facts"]) is not dict
        ):
            raise PipelineUnavailable()
        return reply["facts"]

    @staticmethod
    def _decode_locked_project(row_json: Any, project_id: uuid.UUID) -> dict[str, Any]:
        if row_json is not None and type(row_json) is not str:
            raise PipelineUnavailable()
        try:
            project = decode_project_row(row_json, target_id=project_id)
        except ProjectQueryUnavailable as error:
            raise PipelineUnavailable() from error
        except (ValueError, OverflowError) as error:
            raise PipelineRejected(
                500, "Internal Server Error", rollback=True
            ) from error
        if project is None:
            raise PipelineRejected(404, "项目不存在")
        return project

    @staticmethod
    def _manager_access(actor: Mapping[str, Any], project, project_id):
        owner_value = project["owner_id"]
        owner_id = _uuid(owner_value) if owner_value is not None else None
        try:
            return project_manager_access(
                actor_id=_uuid(actor["actor_id"]),
                platform_role=actor["platform_role"],
                project_id=project_id,
                owner_id=owner_id,
            )
        except ProjectAccessPolicyFailure as error:
            raise PipelineRejected(error.status_code, error.detail) from error

    @staticmethod
    def _member_access(actor, project_id, member):
        try:
            return project_member_access(
                actor_id=_uuid(actor["actor_id"]),
                platform_role=actor["platform_role"],
                project_id=project_id,
                membership_id=member.id if member is not None else None,
                project_role=member.role if member is not None else None,
                membership_version=member.version if member is not None else None,
            )
        except ProjectAccessPolicyFailure as error:
            raise PipelineRejected(error.status_code, error.detail) from error

    async def _unenabled_backend_ids(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest,
        project_id: uuid.UUID,
        stages: Any,
    ) -> list[str]:
        missing: list[str] = []
        seen: set[str] = set()
        # Preserve the old raw stages iteration. A stored JSON null therefore
        # raises its original TypeError at this phase after stage validation.
        for stage in stages:
            if not isinstance(stage, dict) or not stage.get("ml_backend_id"):
                continue
            backend_id = str(stage["ml_backend_id"])
            if backend_id in seen:
                continue
            seen.add(backend_id)
            try:
                parsed_backend_id = uuid.UUID(backend_id)
            except ValueError:
                missing.append(backend_id)
                continue

            registry_facts = _exact_dict(
                await self._step(
                    transaction,
                    request,
                    "backend_get",
                    {"backend_id": str(parsed_backend_id)},
                ),
                {"row_json"},
            )
            try:
                registry = self._decode_backend_registry(registry_facts["row_json"])
            except (ValueError, OverflowError) as error:
                raise self._legacy_materialization_failure() from error
            if registry is None:
                missing.append(backend_id)
                continue
            if registry["id"] != parsed_backend_id:
                raise PipelineUnavailable()

            pool_facts = _exact_dict(
                await self._step(
                    transaction,
                    request,
                    "backend_pool",
                    {"backend_id": str(parsed_backend_id)},
                ),
                {"rows_json"},
            )
            try:
                pools = self._decode_backend_pools(pool_facts["rows_json"])
            except (ValueError, OverflowError) as error:
                raise self._legacy_materialization_failure() from error
            if not pools:
                missing.append(backend_id)
                continue
            # The legacy query has no ORDER BY and uses .first(); preserve the
            # complete decoded rows and consume their first returned pool.
            pool_id = pools[0]["id"]

            association_facts = _exact_dict(
                await self._step(
                    transaction,
                    request,
                    "backend_enabled",
                    {"project_id": str(project_id), "pool_id": str(pool_id)},
                ),
                {"row_json"},
            )
            try:
                association = self._decode_backend_association(
                    association_facts["row_json"]
                )
            except (ValueError, OverflowError) as error:
                raise self._legacy_materialization_failure() from error
            if association is not None and (
                association["project_id"] != project_id
                or association["pool_id"] != pool_id
            ):
                raise PipelineUnavailable()
            if association is None or not bool(association["enabled"]):
                missing.append(backend_id)
        return missing

    @staticmethod
    def _fact_json(value: Any) -> Any:
        """Decode a stored JSONB text value with the old driver's ValueError phase."""
        if type(value) is str:
            return json.loads(value)
        return value

    @staticmethod
    def _fact_timestamp(value: Any, *, nullable: bool) -> Any:
        from aap_workspace_contract.project_query_contract import (
            ProjectQueryUnavailable,
        )
        from aap_workspace_contract.project_query_values import (
            decode_postgres_timestamp,
        )

        if value is None and nullable:
            return None
        if type(value) is not str:
            raise PipelineUnavailable()
        try:
            return decode_postgres_timestamp(value)
        except ProjectQueryUnavailable as error:
            raise PipelineUnavailable() from error

    @classmethod
    def _decode_backend_registry(cls, row_json: Any) -> dict[str, Any] | None:
        if row_json is None:
            return None
        row = _json_object(row_json)
        row = _exact_dict(
            row,
            {
                "id",
                "last_checked_at",
                "created_at",
                "updated_at",
                "extra_params",
                "health_meta",
            },
        )
        row["id"] = _uuid(row["id"])
        row["last_checked_at"] = cls._fact_timestamp(
            row["last_checked_at"], nullable=True
        )
        row["created_at"] = cls._fact_timestamp(row["created_at"], nullable=False)
        row["updated_at"] = cls._fact_timestamp(row["updated_at"], nullable=False)
        row["extra_params"] = cls._fact_json(row["extra_params"])
        row["health_meta"] = cls._fact_json(row["health_meta"])
        return row

    @classmethod
    def _decode_backend_pools(cls, rows_json: Any) -> list[dict[str, Any]]:
        if type(rows_json) is not str:
            raise PipelineUnavailable()
        # This is Native transport JSON. Stored JSONB subfields are separately
        # decoded below, matching full ORM row materialization.
        values = _transport_json(rows_json)
        if type(values) is not list:
            raise PipelineUnavailable()
        result = []
        expected = {"id", "created_at", "updated_at", "capability_snapshot"}
        for value in values:
            pool = _exact_dict(value, expected)
            pool["id"] = _uuid(pool["id"])
            pool["created_at"] = cls._fact_timestamp(pool["created_at"], nullable=False)
            pool["updated_at"] = cls._fact_timestamp(pool["updated_at"], nullable=False)
            pool["capability_snapshot"] = cls._fact_json(pool["capability_snapshot"])
            result.append(pool)
        return result

    @classmethod
    def _decode_backend_association(cls, row_json: Any) -> dict[str, Any] | None:
        if row_json is None:
            return None
        row = _json_object(row_json)
        row = _exact_dict(
            row,
            {
                "id",
                "project_id",
                "pool_id",
                "enabled",
                "created_at",
                "updated_at",
                "default_variants",
            },
        )
        row["id"] = _uuid(row["id"])
        row["project_id"] = _uuid(row["project_id"])
        row["pool_id"] = _uuid(row["pool_id"])
        if row["enabled"] is not None and type(row["enabled"]) is not bool:
            raise PipelineUnavailable()
        row["created_at"] = cls._fact_timestamp(row["created_at"], nullable=False)
        row["updated_at"] = cls._fact_timestamp(row["updated_at"], nullable=False)
        row["default_variants"] = cls._fact_json(row["default_variants"])
        return row

    async def _default_clear(
        self,
        transaction: PipelineTransaction,
        request: PipelineCommandRequest,
        project_id: uuid.UUID,
    ) -> None:
        facts = _exact_dict(
            await self._step(
                transaction,
                request,
                "default_clear",
                {"project_id": str(project_id)},
            ),
            {"affected_count"},
        )
        if type(facts["affected_count"]) is not int:
            raise PipelineUnavailable()

    @staticmethod
    def _optional_uuid(value: Any) -> uuid.UUID | None:
        return None if value is None else PipelineManager._payload_uuid(value)

    @staticmethod
    def _payload_uuid(value: Any) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if type(value) is not str:
            raise PipelineUnavailable()
        try:
            return uuid.UUID(value)
        except ValueError:
            raise PipelineUnavailable() from None

    @staticmethod
    def _uuid_text(value: uuid.UUID | None) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _legacy_materialization_failure(*, rollback: bool = True) -> PipelineRejected:
        return PipelineRejected(500, "Internal Server Error", rollback=rollback)

    @staticmethod
    def _command_payload(
        request: PipelineCommandRequest,
        *,
        required: set[str] | None = None,
        allowed: set[str] | None = None,
    ) -> dict[str, Any]:
        try:
            value = json.loads(
                request.payload_ascii,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, UnicodeError, TypeError, RecursionError):
            raise PipelineUnavailable() from None
        if type(value) is not dict:
            raise PipelineUnavailable()
        if required is not None and set(value) != required:
            raise PipelineUnavailable()
        if allowed is not None and not set(value).issubset(allowed):
            raise PipelineUnavailable()
        return value

    async def _step(
        self,
        transaction: PipelineTransaction,
        request: Any,
        action: str,
        args: Mapping[str, Any] | None = None,
        *,
        columns: Sequence[str] = (),
        values: Sequence[str | None] = (),
    ) -> dict[str, Any]:
        raw = await transaction.step(
            request,
            action,
            {} if args is None else dict(args),
            list(columns),
            list(values),
        )
        reply = decode_pipeline_step_reply(request, action, raw)
        if isinstance(reply, PipelineStepFailure):
            error = reply.error
            raise PipelineRejected(
                error.status_code,
                error.detail,
                headers=error.headers,
                rollback=error.rollback,
            )
        facts = reply.facts
        if hasattr(facts, "model_dump"):
            facts = facts.model_dump(mode="python", by_alias=True)
        if type(facts) is not dict:
            raise PipelineUnavailable()
        return facts

    @staticmethod
    def _command_success(
        request: PipelineCommandRequest,
        resource_id: uuid.UUID,
        commit_state: str,
    ) -> PipelineSuccess:
        raw = PipelineSuccess(
            schema=PIPELINE_PROTOCOL,
            ok=True,
            operation_id=request.operation_id,
            operation=request.operation,
            actor_id=request.admitted_actor.id,
            target_id=request.target_id,
            result=PipelineResult(resource_id=resource_id),
            commit_state=commit_state,
        )
        return decode_pipeline_reply(
            request, raw, require_committed=commit_state == "committed"
        )

    @staticmethod
    def _command_failure(
        request: PipelineCommandRequest,
        error: PipelineRejected,
        *,
        commit_state: str,
    ) -> PipelineFailure:
        return decode_pipeline_reply(
            request,
            PipelineFailure(
                schema=PIPELINE_PROTOCOL,
                ok=False,
                operation_id=request.operation_id,
                operation=request.operation,
                actor_id=request.admitted_actor.id,
                target_id=request.target_id,
                error=PipelineErrorDetails(
                    status_code=error.status_code,
                    detail=error.detail,
                    headers=error.headers,
                    rollback=error.rollback,
                ),
                commit_state=commit_state,
            ),
        )

    @staticmethod
    def _query_failure(
        request: PipelineQueryRequest, error: PipelineRejected
    ) -> PipelineQueryFailure:
        return decode_pipeline_query_reply(
            request,
            PipelineQueryFailure(
                schema=PIPELINE_PROTOCOL,
                ok=False,
                request_id=request.request_id,
                operation=request.operation,
                actor_id=request.admitted_actor.id,
                target_id=request.target_id,
                error=PipelineErrorDetails(
                    status_code=error.status_code,
                    detail=error.detail,
                    headers=error.headers,
                    rollback=error.rollback,
                ),
            ),
        )

    @staticmethod
    def _status_failure(
        request: PipelineStatusRequest, error: PipelineRejected
    ) -> PipelineStatusFailure:
        return decode_pipeline_status_reply(
            request,
            PipelineStatusFailure(
                schema=PIPELINE_PROTOCOL,
                ok=False,
                request_id=request.request_id,
                operation=request.operation,
                operation_id=request.command.operation_id,
                command_operation=request.command.operation,
                actor_id=request.command.admitted_actor.id,
                target_id=request.command.target_id,
                error=PipelineErrorDetails(
                    status_code=error.status_code,
                    detail=error.detail,
                    headers=error.headers,
                    rollback=error.rollback,
                ),
            ),
        )
