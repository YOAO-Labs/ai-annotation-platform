"""Actual transfers retain precommit decoding and postcommit current-row reads."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace_contract.project_query_contract import ProjectQueryProjectionMissing
from app.application.project_workspace import transfer_project_owner
from app.config import settings
from app.db.models.project import Project
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services.workspace_project_query import execute_project_query
from tests.factory import create_project, create_user
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@asynccontextmanager
async def _world(test_engine, monkeypatch):
    for field in (
        "workspace_owner_transfer_url",
        "workspace_owner_transfer_database_url",
        "workspace_owner_transfer_database_url_file",
    ):
        monkeypatch.setattr(settings, field, "")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    ids = []
    project_id = None
    try:
        async with maker.begin() as db:
            for role in ("super_admin", "project_admin", "project_admin"):
                user = await create_user(
                    db, role, f"transfer-query-{uuid.uuid4().hex}@example.test", role
                )
                ids.append(user.id)
            project = await create_project(db, owner_id=ids[1], name="Before commit")
            project_id = project.id
        actor = IdentityActorFacts(
            id=ids[0],
            name="Accepted actor",
            email="accepted@example.test",
            role="super_admin",
            is_active=True,
        )
        yield maker, actor, project_id, ids[1], ids[2]
    finally:
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(
                    text(
                        "DELETE FROM public.workspace_owner_transfer_operation_receipts "
                        "WHERE project_id=:id"
                    ),
                    {"id": project_id},
                )
                await db.execute(delete(Project).where(Project.id == project_id))
            if ids:
                await db.execute(delete(User).where(User.id.in_(ids)))


async def test_precommit_decode_failure_keeps_caller_transfer_uncommitted(
    test_engine, monkeypatch
):
    async with _world(test_engine, monkeypatch) as (maker, actor, target, old, new):
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.projects SET due_date=DATE '10000-01-01' WHERE id=:id"
                ),
                {"id": target},
            )
        async with maker() as caller:
            with pytest.raises(ValueError, match="year 10000"):
                await transfer_project_owner(
                    caller, actor=actor, project_id=target, new_owner_id=new
                )
            assert caller.in_transaction()
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT owner_id FROM public.projects WHERE id=:id"),
                        {"id": target},
                    )
                    == old
                )
            await caller.rollback()
        async with maker() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts "
                        "WHERE project_id=:id"
                    ),
                    {"id": target},
                )
                == 0
            )


@pytest.mark.parametrize("mutation", ["rename", "delete", "temporal_extreme"])
async def test_accepted_transfer_projection_rereads_after_commit_without_reauthorization(
    test_engine, monkeypatch, mutation
):
    async with _world(test_engine, monkeypatch) as (maker, actor, target, _old, new):
        async with maker() as caller:
            evidence = await transfer_project_owner(
                caller, actor=actor, project_id=target, new_owner_id=new
            )
            await caller.commit()
        async with maker.begin() as observer:
            # The accepted transfer remains projectable after the actor loses
            # the original role, while row contents still come from this phase.
            await observer.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": actor.id},
            )
            if mutation == "delete":
                await observer.execute(delete(Project).where(Project.id == target))
            elif mutation == "temporal_extreme":
                await observer.execute(
                    text(
                        "UPDATE public.projects SET due_date=DATE '10000-01-01' WHERE id=:id"
                    ),
                    {"id": target},
                )
            else:
                await observer.execute(
                    text("UPDATE public.projects SET name='After commit' WHERE id=:id"),
                    {"id": target},
                )
        async with api_project_query_binding(
            dsn=project_query_dsn(test_engine), token="test-query"
        ):

            async def projection():
                async with maker() as caller:
                    return await execute_project_query(
                        caller,
                        operation="get_project_projection",
                        actor=actor,
                        target_id=target,
                        projection_evidence=evidence,
                    )

            if mutation == "delete":
                with pytest.raises(ProjectQueryProjectionMissing):
                    await projection()
            elif mutation == "temporal_extreme":
                with pytest.raises(ValueError, match="year 10000"):
                    await projection()
            else:
                result = await projection()
                assert result["name"] == "After commit" and result["owner_id"] == new
        async with maker() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": evidence.operation_id},
                )
                == 1
            )
