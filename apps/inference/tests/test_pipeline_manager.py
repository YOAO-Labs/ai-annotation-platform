from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid

import pytest

from aap_inference.pipeline_contract import (
    PIPELINE_STEP_PROTOCOL,
    PipelineRejected,
    PipelineStepSuccess,
    PipelineUnavailable,
    pipeline_command_request,
    pipeline_query_request,
    pipeline_status_request,
)
from aap_inference.pipeline_manager import PipelineManager


class _Transaction:
    def __init__(self, *, owns_commit: bool, step_handler=None):
        self.autoflush = True
        self.owns_commit = owns_commit
        self.events = []
        self.step_handler = step_handler

    async def step(self, request, action, args=None, columns=(), values=()):
        event = (action, args or {}, list(columns), list(values))
        self.events.append(event)
        if self.step_handler is not None:
            facts = self.step_handler(request, action, args or {})
        elif action == "begin":
            facts = {"replay": None}
        elif action == "status":
            facts = {"state": "unknown", "resource_id": None}
        else:
            raise AssertionError(f"Unexpected pipeline action: {action}")
        return PipelineStepSuccess(
            schema=PIPELINE_STEP_PROTOCOL,
            ok=True,
            request_id=(
                request.request_id
                if hasattr(request, "request_id")
                else request.operation_id
            ),
            operation=request.operation,
            action=action,
            actor_id=(
                request.command.admitted_actor.id
                if hasattr(request, "command")
                else request.admitted_actor.id
            ),
            target_id=(
                request.command.target_id
                if hasattr(request, "command")
                else request.target_id
            ),
            facts=facts,
        )


class _Data:
    def __init__(self, *, owns_commit=True, step_handler=None):
        self.transaction_value = _Transaction(
            owns_commit=owns_commit, step_handler=step_handler
        )
        self.readonly_values = []
        self.committed = False

    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        self.readonly_values.append(readonly)
        try:
            yield self.transaction_value
        except BaseException:
            raise
        else:
            if self.transaction_value.owns_commit and not readonly:
                self.committed = True


class _ExitErrorData(_Data):
    def __init__(self, *, exit_error, step_handler):
        super().__init__(owns_commit=True, step_handler=step_handler)
        self.exit_error = exit_error

    @asynccontextmanager
    async def transaction(self, *, readonly=False):
        self.readonly_values.append(readonly)
        try:
            yield self.transaction_value
        except BaseException:
            raise
        else:
            if not readonly:
                raise self.exit_error


def _command(*, operation, actor_id, actor_role, target_id=None, payload):
    return pipeline_command_request(
        operation,
        actor_id=actor_id,
        actor_role=actor_role,
        target_id=target_id,
        payload=payload,
        operation_id=uuid.uuid4(),
    )


_DEFAULT_STAGES = object()


def _pipeline_row_json(
    *,
    pipeline_id,
    actor_id,
    scope="public",
    project_id=None,
    organization_id=None,
    name="Existing",
    stages=_DEFAULT_STAGES,
    is_default=False,
):
    return json.dumps(
        {
            "id": str(pipeline_id),
            "scope": scope,
            "project_id": str(project_id) if project_id is not None else None,
            "organization_id": (
                str(organization_id) if organization_id is not None else None
            ),
            "name": name,
            "stages": [] if stages is _DEFAULT_STAGES else stages,
            "is_default": is_default,
            "created_by": str(actor_id),
            "usage_count": 0,
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }
    )


class _UpdateManager(PipelineManager):
    async def _assert_project_visible(self, _transaction, _command, _project_id):
        return None


def _update_data(row_json, pipeline_id):
    def handle(_request, action, _args):
        if action == "begin":
            return {"replay": None}
        if action == "pipeline_get":
            return {"row_json": row_json}
        if action == "default_clear":
            return {"affected_count": 1}
        if action in {"pipeline_update", "finish"}:
            return {"resource_id": str(pipeline_id)}
        raise AssertionError(f"Unexpected pipeline action: {action}")

    return _Data(owns_commit=True, step_handler=handle)


@pytest.mark.asyncio
async def test_create_role_rejection_precedes_identity_or_project_reads():
    actor_id = uuid.uuid4()
    request = _command(
        operation="create_pipeline",
        actor_id=actor_id,
        actor_role="employee",
        payload={
            "name": "Pipeline",
            "scope": "organization",
            "project_id": None,
            "organization_id": str(uuid.uuid4()),
            "stages": [],
            "is_default": False,
        },
    )
    data = _Data(owns_commit=True)

    reply = await PipelineManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 403
    assert reply.commit_state == "rolled_back"
    assert [event[0] for event in data.transaction_value.events] == ["begin"]
    assert data.committed is False


@pytest.mark.asyncio
async def test_borrowed_unknown_status_preserves_original_command_binding():
    command = _command(
        operation="delete_pipeline",
        actor_id=uuid.uuid4(),
        actor_role="project_admin",
        target_id=uuid.uuid4(),
        payload={},
    )
    status = pipeline_status_request(command, request_id=uuid.uuid4())
    data = _Data(owns_commit=False)

    reply = await PipelineManager(data).status(status)

    assert reply.ok is True
    assert reply.operation_id == command.operation_id
    assert reply.actor_id == command.admitted_actor.id
    assert reply.target_id == command.target_id
    assert reply.state == "unknown"
    assert reply.resource_id is None
    assert data.readonly_values == [True]


@pytest.mark.asyncio
async def test_noop_update_keeps_explicit_flush_phase_with_empty_columns():
    actor_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    project_id = uuid.uuid4()
    request = _command(
        operation="update_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=pipeline_id,
        payload={"name": "Existing"},
    )
    data = _update_data(
        _pipeline_row_json(
            pipeline_id=pipeline_id,
            actor_id=actor_id,
            scope="private",
            project_id=project_id,
        ),
        pipeline_id,
    )

    reply = await _UpdateManager(data).execute(request)

    assert reply.ok is True
    assert reply.commit_state == "committed"
    assert [event[0] for event in data.transaction_value.events] == [
        "begin",
        "pipeline_get",
        "pipeline_update",
        "finish",
    ]
    update_event = data.transaction_value.events[2]
    assert update_event[2:] == ([], [])
    assert data.committed is True


@pytest.mark.asyncio
async def test_default_clear_restores_same_project_default_as_dirty_update():
    actor_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    project_id = uuid.uuid4()
    request = _command(
        operation="update_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=pipeline_id,
        payload={"is_default": True},
    )
    data = _update_data(
        _pipeline_row_json(
            pipeline_id=pipeline_id,
            actor_id=actor_id,
            scope="private",
            project_id=project_id,
            is_default=True,
        ),
        pipeline_id,
    )

    reply = await _UpdateManager(data).execute(request)

    assert reply.ok is True
    assert [event[0] for event in data.transaction_value.events] == [
        "begin",
        "pipeline_get",
        "default_clear",
        "pipeline_update",
        "finish",
    ]
    clear_event = data.transaction_value.events[2]
    assert clear_event[1] == {"project_id": str(project_id)}
    update_event = data.transaction_value.events[3]
    assert update_event[2:] == (["is_default"], ["true"])


@pytest.mark.asyncio
async def test_move_default_does_not_dirty_unchanged_default_column():
    actor_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    old_project_id = uuid.uuid4()
    new_project_id = uuid.uuid4()
    request = _command(
        operation="update_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=pipeline_id,
        payload={"project_id": str(new_project_id), "is_default": True},
    )
    data = _update_data(
        _pipeline_row_json(
            pipeline_id=pipeline_id,
            actor_id=actor_id,
            scope="private",
            project_id=old_project_id,
            is_default=True,
        ),
        pipeline_id,
    )

    reply = await _UpdateManager(data).execute(request)

    assert reply.ok is True
    clear_event = data.transaction_value.events[2]
    assert clear_event[0] == "default_clear"
    assert clear_event[1] == {"project_id": str(new_project_id)}
    update_event = data.transaction_value.events[3]
    assert update_event[2:] == (["project_id"], [str(new_project_id)])


@pytest.mark.asyncio
async def test_stored_null_apply_stages_return_known_generic_500():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    def handle(_request, action, _args):
        if action == "begin":
            return {"replay": None}
        if action == "pipeline_get":
            return {
                "row_json": _pipeline_row_json(
                    pipeline_id=pipeline_id,
                    actor_id=actor_id,
                    scope="public",
                    stages=None,
                )
            }
        raise AssertionError(f"Unexpected pipeline action after null stages: {action}")

    request = _command(
        operation="apply_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=project_id,
        payload={"pipeline_id": str(pipeline_id), "set_default": False},
    )
    data = _Data(owns_commit=True, step_handler=handle)

    reply = await PipelineManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 500
    assert reply.error.detail == "Internal Server Error"
    assert reply.error.rollback is True
    assert reply.commit_state == "rolled_back"
    assert [event[0] for event in data.transaction_value.events] == [
        "begin",
        "pipeline_get",
    ]
    assert data.committed is False


@pytest.mark.parametrize(
    ("exit_error", "expected_status", "expected_state", "expected_rollback"),
    [
        (
            PipelineRejected(500, "Internal Server Error", rollback=True),
            500,
            "rolled_back",
            True,
        ),
        (PipelineUnavailable(), 503, "unknown", False),
    ],
)
@pytest.mark.asyncio
async def test_owner_transaction_exit_preserves_known_failure_vs_unknown(
    exit_error, expected_status, expected_state, expected_rollback
):
    actor_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    def handle(_request, action, _args):
        if action == "begin":
            return {"replay": None}
        if action == "pipeline_get":
            return {
                "row_json": _pipeline_row_json(
                    pipeline_id=pipeline_id,
                    actor_id=actor_id,
                    scope="public",
                )
            }
        if action in {"pipeline_delete", "finish"}:
            return {"resource_id": str(pipeline_id)}
        raise AssertionError(f"Unexpected pipeline action: {action}")

    request = _command(
        operation="delete_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        target_id=pipeline_id,
        payload={},
    )
    data = _ExitErrorData(exit_error=exit_error, step_handler=handle)

    reply = await PipelineManager(data).execute(request)

    assert reply.ok is False
    assert reply.error.status_code == expected_status
    assert reply.error.rollback is expected_rollback
    assert reply.commit_state == expected_state
    assert [event[0] for event in data.transaction_value.events] == [
        "begin",
        "pipeline_get",
        "pipeline_delete",
        "finish",
    ]


@pytest.mark.asyncio
async def test_query_materialization_typeerror_is_generic_500_but_unavailable_stays_unavailable():
    actor_id = uuid.uuid4()
    query = pipeline_query_request(
        "list_pipelines",
        actor_id=actor_id,
        actor_role="super_admin",
        filters={"scope": None, "project_id": None, "organization_id": None},
    )

    def type_error(_request, action, _args):
        if action == "pipeline_list":
            raise TypeError("legacy stored stages value")
        raise AssertionError(f"Unexpected pipeline action: {action}")

    reply = await PipelineManager(
        _Data(owns_commit=True, step_handler=type_error)
    ).query(query)

    assert reply.ok is False
    assert reply.error.status_code == 500
    assert reply.error.detail == "Internal Server Error"
    assert reply.error.rollback is False

    def unavailable(_request, action, _args):
        if action == "pipeline_list":
            raise PipelineUnavailable()
        raise AssertionError(f"Unexpected pipeline action: {action}")

    with pytest.raises(PipelineUnavailable):
        await PipelineManager(_Data(owns_commit=True, step_handler=unavailable)).query(
            query
        )


def _backend_registry_facts(backend_id):
    return {
        "row_json": json.dumps(
            {
                "id": str(backend_id),
                "last_checked_at": None,
                "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z",
                "extra_params": "{}",
                "health_meta": "{}",
            }
        )
    }


@pytest.mark.asyncio
async def test_backend_probe_keeps_registry_pool_and_association_reads_separate():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    association_id = uuid.uuid4()

    def handle(_request, action, args):
        if action == "backend_get":
            return _backend_registry_facts(uuid.UUID(args["backend_id"]))
        if action == "backend_pool":
            return {
                "rows_json": json.dumps(
                    [
                        {
                            "id": str(pool_id),
                            "created_at": "2025-01-01T00:00:00Z",
                            "updated_at": "2025-01-01T00:00:00Z",
                            "capability_snapshot": "null",
                        }
                    ]
                )
            }
        if action == "backend_enabled":
            return {
                "row_json": json.dumps(
                    {
                        "id": str(association_id),
                        "project_id": str(project_id),
                        "pool_id": str(pool_id),
                        "enabled": False,
                        "created_at": "2025-01-01T00:00:00Z",
                        "updated_at": "2025-01-01T00:00:00Z",
                        "default_variants": "null",
                    }
                )
            }
        raise AssertionError(f"Unexpected backend action {action}")

    request = _command(
        operation="apply_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=project_id,
        payload={"pipeline_id": str(uuid.uuid4()), "set_default": False},
    )
    transaction = _Transaction(owns_commit=False, step_handler=handle)
    missing = await PipelineManager(_Data(owns_commit=False))._unenabled_backend_ids(
        transaction,
        request,
        project_id,
        [
            {"ml_backend_id": str(backend_id)},
            {"ml_backend_id": str(backend_id)},
            {"ml_backend_id": "not-a-uuid"},
        ],
    )

    assert missing == [str(backend_id), "not-a-uuid"]
    assert [event[0] for event in transaction.events] == [
        "backend_get",
        "backend_pool",
        "backend_enabled",
    ]


@pytest.mark.asyncio
async def test_backend_probe_rejects_registry_for_another_requested_id():
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    unrelated_backend_id = uuid.uuid4()

    def handle(_request, action, _args):
        if action == "backend_get":
            return _backend_registry_facts(unrelated_backend_id)
        raise AssertionError(
            f"Unexpected backend action after mismatched registry: {action}"
        )

    request = _command(
        operation="apply_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=project_id,
        payload={"pipeline_id": str(uuid.uuid4()), "set_default": False},
    )
    transaction = _Transaction(owns_commit=False, step_handler=handle)

    with pytest.raises(PipelineUnavailable):
        await PipelineManager(_Data(owns_commit=False))._unenabled_backend_ids(
            transaction,
            request,
            project_id,
            [{"ml_backend_id": str(backend_id)}],
        )

    assert [event[0] for event in transaction.events] == ["backend_get"]


@pytest.mark.parametrize("mismatched_field", ["project_id", "pool_id"])
@pytest.mark.asyncio
async def test_backend_probe_rejects_misbound_project_association(mismatched_field):
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    unrelated_id = uuid.uuid4()
    association_id = uuid.uuid4()
    association_project_id = project_id
    association_pool_id = pool_id
    if mismatched_field == "project_id":
        association_project_id = unrelated_id
    else:
        association_pool_id = unrelated_id

    def handle(_request, action, _args):
        if action == "backend_get":
            return _backend_registry_facts(backend_id)
        if action == "backend_pool":
            return {
                "rows_json": json.dumps(
                    [
                        {
                            "id": str(pool_id),
                            "created_at": "2025-01-01T00:00:00Z",
                            "updated_at": "2025-01-01T00:00:00Z",
                            "capability_snapshot": "null",
                        }
                    ]
                )
            }
        if action == "backend_enabled":
            return {
                "row_json": json.dumps(
                    {
                        "id": str(association_id),
                        "project_id": str(association_project_id),
                        "pool_id": str(association_pool_id),
                        "enabled": True,
                        "created_at": "2025-01-01T00:00:00Z",
                        "updated_at": "2025-01-01T00:00:00Z",
                        "default_variants": "null",
                    }
                )
            }
        raise AssertionError(f"Unexpected backend action: {action}")

    request = _command(
        operation="apply_pipeline",
        actor_id=actor_id,
        actor_role="project_admin",
        target_id=project_id,
        payload={"pipeline_id": str(uuid.uuid4()), "set_default": False},
    )
    transaction = _Transaction(owns_commit=False, step_handler=handle)

    with pytest.raises(PipelineUnavailable):
        await PipelineManager(_Data(owns_commit=False))._unenabled_backend_ids(
            transaction,
            request,
            project_id,
            [{"ml_backend_id": str(backend_id)}],
        )

    assert [event[0] for event in transaction.events] == [
        "backend_get",
        "backend_pool",
        "backend_enabled",
    ]
