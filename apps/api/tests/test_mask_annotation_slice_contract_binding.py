from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.mask_annotation_slices import (
    _AnnotationSliceSqlAdapter,
    _MaskMutationSqlAdapter,
)
from app.services.audit_origin_contract import AuditOriginContext
from aap_workspace_contract.access import ProjectAccess


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["mask", "polygon", "restore"])
@pytest.mark.parametrize("mismatch", ["task", "actor", "access"])
async def test_mutation_binding_mismatch_is_rejected_before_sql(operation, mismatch):
    db = AsyncMock()
    task_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor = SimpleNamespace(id=actor_id)
    bound_access_id = uuid.uuid4() if mismatch == "access" else actor_id
    access = ProjectAccess(
        user_id=bound_access_id,
        project_id=project_id,
        platform_role="super_admin",
        project_role=None,
        membership_id=None,
        membership_version=None,
        access_kind="super_admin",
        capabilities=frozenset(),
        is_super_admin=True,
    )
    requested_task_id = uuid.uuid4() if mismatch == "task" else task_id
    requested_actor_id = uuid.uuid4() if mismatch == "actor" else actor_id
    origin = AuditOriginContext(method="POST", path="/test", ip="127.0.0.1")

    if operation == "mask":
        adapter = _MaskMutationSqlAdapter(
            db, task_id=task_id, actor=actor, access=access
        )
        call = adapter.commit(
            task_id=requested_task_id,
            actor_id=requested_actor_id,
            payload=object(),
            audit_origin=origin,
        )
    else:
        adapter = _AnnotationSliceSqlAdapter(
            db, task_id=task_id, actor=actor, access=access
        )
        if operation == "polygon":
            call = adapter.commit_polygon(
                task_id=requested_task_id,
                actor_id=requested_actor_id,
                payload=object(),
                audit_origin=origin,
            )
        else:
            call = adapter.restore(
                task_id=requested_task_id,
                operation_id=uuid.uuid4(),
                actor_id=requested_actor_id,
                payload=object(),
                audit_origin=origin,
            )

    with pytest.raises(ValueError, match="identity does not match binding"):
        await call

    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()
    db.get.assert_not_awaited()
