"""Verify final Project authority locks through every Inference owner binding."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.pipeline_contract import PipelineFailure, pipeline_command_request
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.services.inference_pipelines import _invoke, _select_owner
from tests.factory import create_project, create_user
from tests.inference_pipeline_owner_helpers import (
    _BINDINGS,
    api_pipeline_owner_binding,
)
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_BUSY_PHASES = (
    ("lock_actor_share", "account_busy"),
    ("lock_project_key_share", "project_busy"),
    ("lock_member_share", "membership_busy"),
)


def _owner_dsn(test_engine) -> str:
    url = test_engine.url.set(drivername="postgresql")
    assert url.database and url.database.endswith(("_test", "_e2e"))
    return url.render_as_string(hide_password=False)


async def _seed(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"pipeline-authority-{uuid.uuid4().hex}@example.test",
            "Pipeline authority actor",
        )
        project = await create_project(
            db, owner_id=actor.id, name="Pipeline authority target"
        )
        source = ProjectPipeline(
            id=uuid.uuid4(),
            scope="public",
            name="Authority source",
            stages=[],
            is_default=False,
            created_by=actor.id,
        )
        prior_default = ProjectPipeline(
            id=uuid.uuid4(),
            scope="private",
            project_id=project.id,
            name="Authority prior default",
            stages=[],
            is_default=True,
            created_by=actor.id,
        )
        db.add_all([source, prior_default])
    return maker, actor.id, project.id, source.id, prior_default.id


async def _withdraw_owner_and_add_annotator(maker, actor_id, project_id):
    async with maker.begin() as db:
        replacement_owner = await create_user(
            db,
            "project_admin",
            f"pipeline-authority-owner-{uuid.uuid4().hex}@example.test",
            "Replacement project owner",
        )
        await db.execute(
            update(User).where(User.id == actor_id).values(role="employee")
        )
        await db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(owner_id=replacement_owner.id)
        )
        db.add(
            ProjectMember(
                project_id=project_id,
                user_id=actor_id,
                role="annotator",
                assigned_by=replacement_owner.id,
            )
        )
    return replacement_owner.id


async def _cleanup(maker, actor_id, project_id, source_id, extra_user_ids=()):
    async with maker.begin() as db:
        await db.execute(
            delete(ProjectMember).where(ProjectMember.project_id == project_id)
        )
        await db.execute(
            delete(ProjectPipeline).where(ProjectPipeline.project_id == project_id)
        )
        await db.execute(delete(ProjectPipeline).where(ProjectPipeline.id == source_id))
        await db.execute(delete(Project).where(Project.id == project_id))
        await db.execute(delete(User).where(User.id.in_((actor_id, *extra_user_ids))))


async def _snapshot(maker, project_id, source_id):
    async with maker() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id::text AS id, name, is_default, usage_count "
                        "FROM public.project_pipelines "
                        "WHERE project_id=:project_id OR id=:source_id "
                        "ORDER BY id"
                    ),
                    {"project_id": project_id, "source_id": source_id},
                )
            )
            .mappings()
            .all()
        )
        total_tasks = await db.scalar(
            select(Project.total_tasks).where(Project.id == project_id)
        )
    return {"pipelines": [dict(row) for row in rows], "total_tasks": total_tasks}


async def _operation_receipt_count(maker, operation_id):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM aap_inference_pipelines_v1.operation_receipts "
                "WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        )


@asynccontextmanager
async def _hold_authority_row(test_engine, phase, actor_id, project_id):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    transaction = connection.transaction()
    started = False
    try:
        await transaction.start()
        started = True
        if phase == "lock_actor_share":
            query = "SELECT id FROM public.users WHERE id=$1::uuid FOR UPDATE"
            parameters = (actor_id,)
        elif phase == "lock_project_key_share":
            query = "SELECT id FROM public.projects WHERE id=$1::uuid FOR UPDATE"
            parameters = (project_id,)
        else:
            query = (
                "SELECT id FROM public.project_members "
                "WHERE project_id=$1::uuid AND user_id=$2::uuid FOR UPDATE"
            )
            parameters = (project_id, actor_id)
        row = await connection.fetchrow(query, *parameters)
        assert row is not None
        yield connection
    finally:
        try:
            if started:
                await transaction.rollback()
        finally:
            await connection.close()


async def _execute_private_command(mode, maker, request):
    owner = _select_owner()
    if mode == "borrowed":
        async with maker() as db:
            reply = await _invoke(owner, db, "execute", request)
            if isinstance(reply, PipelineFailure) and reply.error.rollback:
                # Match the borrowed API boundary, which owns this rollback.
                await db.rollback()
            return reply
    return await _invoke(owner, None, "execute", request)


@pytest.mark.parametrize("mode", _BINDINGS)
@pytest.mark.parametrize(("phase", "reason"), _BUSY_PHASES)
async def test_inference_pipeline_final_authority_busy_is_mapped_without_writes(
    test_engine, monkeypatch, tmp_path, mode, phase, reason
):
    await assert_owned_test_database(test_engine)
    maker, actor_id, project_id, source_id, _prior_default_id = await _seed(test_engine)
    replacement_owner_id = None
    extra_users = ()
    try:
        operation_id = uuid.uuid4()
        if phase == "lock_member_share":
            replacement_owner_id = await _withdraw_owner_and_add_annotator(
                maker, actor_id, project_id
            )
            extra_users = (replacement_owner_id,)
        before = await _snapshot(maker, project_id, source_id)
        command = pipeline_command_request(
            "apply_pipeline",
            actor_id=actor_id,
            actor_role="project_admin",
            target_id=project_id,
            payload={"pipeline_id": str(source_id), "set_default": True},
            operation_id=operation_id,
        )
        async with api_pipeline_owner_binding(test_engine, monkeypatch, mode, tmp_path):
            async with _hold_authority_row(test_engine, phase, actor_id, project_id):
                reply = await _execute_private_command(mode, maker, command)

        assert isinstance(reply, PipelineFailure)
        assert reply.error.status_code == 409
        assert reply.error.detail == {"reason": reason}
        assert reply.error.rollback is True
        assert reply.commit_state == (
            "caller_pending" if mode == "borrowed" else "rolled_back"
        )
        assert await _snapshot(maker, project_id, source_id) == before
        assert await _operation_receipt_count(maker, operation_id) == 0
    finally:
        await _cleanup(maker, actor_id, project_id, source_id, extra_users)
