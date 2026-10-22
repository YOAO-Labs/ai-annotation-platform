"""The API borrows the Workspace delete capability in its request transaction."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace.project_deletion_contract import (
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
)
from app.db.models.project import Project
from app.services.project_deletion_data import ProjectDeletionData


def _success(
    *, operation_id: UUID, actor_id: UUID, project_id: UUID
) -> dict[str, object]:
    return {
        "schema": "aap-project-delete/v1",
        "ok": True,
        "data": {
            "operation_id": str(operation_id),
            "actor_id": str(actor_id),
            "project_id": str(project_id),
            "deleted_tasks": 2,
            "deleted_annotations": 3,
            "audit_id": 11,
        },
    }


class _BorrowedSession:
    def __init__(self, payload: object):
        self.payload = payload
        self.events: list[tuple[object, ...]] = []

    async def flush(self) -> None:
        self.events.append(("flush",))

    async def scalar(self, statement, parameters) -> object:
        self.events.append(("scalar", str(statement), parameters))
        return self.payload

    async def commit(self) -> None:
        self.events.append(("commit",))

    async def rollback(self) -> None:
        self.events.append(("rollback",))

    async def begin(self) -> None:
        self.events.append(("begin",))

    async def begin_nested(self) -> None:
        self.events.append(("begin_nested",))


def _identifiers() -> tuple[UUID, UUID, UUID]:
    return uuid4(), uuid4(), uuid4()


async def test_adapter_flushes_then_calls_owner_with_sql_null_and_borrows_transaction():
    operation_id, actor_id, project_id = _identifiers()
    db = _BorrowedSession(
        _success(
            operation_id=operation_id,
            actor_id=actor_id,
            project_id=project_id,
        )
    )

    receipt = await ProjectDeletionData(db).delete(
        operation_id=operation_id,
        actor_id=actor_id,
        project_id=project_id,
    )

    assert isinstance(receipt, ProjectDeletionReceipt)
    assert (receipt.operation_id, receipt.actor_id, receipt.project_id) == (
        operation_id,
        actor_id,
        project_id,
    )
    assert [event[0] for event in db.events] == ["flush", "scalar"]
    _, statement, parameters = db.events[1]
    assert "aap_workspace_v1.delete_project(" in statement
    assert parameters == {
        "operation_id": operation_id,
        "actor_id": actor_id,
        "project_id": project_id,
        "audit_context": None,
    }


async def test_adapter_serializes_only_the_supplied_audit_context():
    operation_id, actor_id, project_id = _identifiers()
    context = ProjectDeletionAuditContext(
        method="DELETE",
        path=f"/api/v1/projects/{project_id}",
        ip="192.0.2.14",
        request_id=str(uuid4()),
    )
    db = _BorrowedSession(
        _success(
            operation_id=operation_id,
            actor_id=actor_id,
            project_id=project_id,
        )
    )

    await ProjectDeletionData(db).delete(
        operation_id=operation_id,
        actor_id=actor_id,
        project_id=project_id,
        audit_context=context,
    )

    parameters = db.events[1][2]
    assert json.loads(parameters["audit_context"]) == context.model_dump(mode="json")
    assert set(parameters) == {
        "operation_id",
        "actor_id",
        "project_id",
        "audit_context",
    }
    assert [event[0] for event in db.events] == ["flush", "scalar"]


async def test_adapter_preserves_owner_failures_without_finishing_the_transaction():
    operation_id, actor_id, project_id = _identifiers()
    db = _BorrowedSession(
        {
            "schema": "aap-project-delete/v1",
            "ok": False,
            "error": {"code": "project_busy"},
        }
    )

    with pytest.raises(ProjectDeletionFailure) as raised:
        await ProjectDeletionData(db).delete(
            operation_id=operation_id,
            actor_id=actor_id,
            project_id=project_id,
        )

    assert raised.value.code == "project_busy"
    assert [event[0] for event in db.events] == ["flush", "scalar"]


async def test_application_keeps_its_adapter_call_and_cached_project_expunge(
    monkeypatch,
):
    from app.application import project_deletion as application

    operation_id, actor_id, project_id = _identifiers()
    expected = ProjectDeletionReceipt(
        operation_id=operation_id,
        actor_id=actor_id,
        project_id=project_id,
        deleted_tasks=2,
        deleted_annotations=3,
        audit_id=11,
    )
    context = ProjectDeletionAuditContext(method="DELETE", path="/projects/test")
    cached_project = object()
    identity_key = (Project, (project_id,), None)
    calls: list[dict[str, object]] = []
    expunged: list[object] = []

    class _ApplicationSession:
        def __init__(self):
            self.sync_session = SimpleNamespace(
                identity_map={identity_key: cached_project}
            )

        def expunge(self, value: object) -> None:
            expunged.append(value)

    class _Adapter:
        def __init__(self, db):
            assert db is session

        async def delete(self, **kwargs):
            calls.append(kwargs)
            return expected

    session = _ApplicationSession()
    monkeypatch.setattr(application, "ProjectDeletionData", _Adapter)

    result = await application.delete_project(
        session,
        project_id,
        actor_id=actor_id,
        audit_context=context,
    )

    assert result is expected
    assert len(calls) == 1
    assert isinstance(calls[0]["operation_id"], UUID)
    assert calls[0]["actor_id"] == actor_id
    assert calls[0]["project_id"] == project_id
    assert calls[0]["audit_context"] is context
    assert expunged == [cached_project]
