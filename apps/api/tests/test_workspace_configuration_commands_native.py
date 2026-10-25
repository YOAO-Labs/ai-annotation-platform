"""Execute real six-operation commands with the function-only credential."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import configuration_request
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from aap_workspace.configuration_requests import ProjectCreate, ProjectTemplateCreate
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_user
from tests.test_workspace_configuration_native import _configuration_dsn

pytestmark = pytest.mark.asyncio


async def test_function_only_six_commands_replay_and_leave_no_drafts(test_engine):
    dsn = _configuration_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"configuration-native-{uuid.uuid4().hex}@example.test",
                "Configuration native owner",
            )
            actor_id = actor.id
        manager = ConfigurationManager(ConfigurationPostgresData(dsn))

        async def execute(operation, target_id=None, payload=None):
            request = configuration_request(
                operation, actor_id, target_id, payload or {}
            )
            reply = await manager.execute(request)
            assert reply.ok, reply.model_dump(mode="json", by_alias=True)
            return request, reply.result

        _, template = await execute(
            "create_template",
            payload=ProjectTemplateCreate(
                name="Native template", type_label="Image", type_key="image-det"
            ).model_dump(exclude_unset=True),
        )
        _, project = await execute(
            "create_project",
            payload=ProjectCreate(
                name="Native project", template_id=template.resource_id
            ).model_dump(exclude_unset=True),
        )
        assert project.template_id == template.resource_id
        first_update, _ = await execute(
            "update_project",
            project.resource_id,
            {
                "name": "First update",
                "box_threshold": 0.35,
            },
        )
        await execute("update_project", project.resource_id, {"name": "Later update"})
        assert (await manager.execute(first_update)).ok
        await execute(
            "update_template", template.resource_id, {"description": "Native updated"}
        )
        _, duplicate = await execute("duplicate_template", template.resource_id)
        assert duplicate.resource_id != template.resource_id
        deletion, _ = await execute("delete_template", template.resource_id)
        assert (await manager.execute(deletion)).ok
        async with maker() as db:
            row = await db.get(Project, project.resource_id)
            assert row.name == "Later update"
            assert row.box_threshold == 0.3499999940395355
            copy = await db.get(ProjectTemplate, duplicate.resource_id)
            assert copy.description == "Native updated"
            assert copy.scope == "private"
            assert await db.get(ProjectTemplate, template.resource_id) is None
            for table in (
                "workspace_configuration_operation_attempts",
                "workspace_configuration_operation_states",
            ):
                assert (
                    await db.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0
                )
    finally:
        if actor_id is not None:
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "DELETE FROM public.workspace_configuration_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor_id},
                )
                await db.execute(
                    delete(ProjectTemplate).where(
                        ProjectTemplate.created_by == actor_id
                    )
                )
                await db.execute(delete(Project).where(Project.owner_id == actor_id))
                await db.execute(delete(User).where(User.id == actor_id))


async def test_stored_configuration_deeper_than_the_small_envelope_limit(test_engine):
    dsn = _configuration_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    config = {}
    node = config
    for _ in range(150):
        node["nested"] = {}
        node = node["nested"]
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"configuration-deep-{uuid.uuid4().hex}@example.test",
                "Configuration nested JSON owner",
            )
            actor_id = actor.id
        payload = ProjectTemplateCreate(
            name="Deep configuration",
            type_label="Image",
            type_key="image-det",
            label_config=config,
        ).model_dump(exclude_unset=True)
        manager = ConfigurationManager(ConfigurationPostgresData(dsn))
        created = await manager.execute(
            configuration_request("create_template", actor_id, None, payload)
        )
        assert created.ok
        duplicated = await manager.execute(
            configuration_request(
                "duplicate_template", actor_id, created.result.resource_id, {}
            )
        )
        assert duplicated.ok
        async with maker() as db:
            row = await db.get(ProjectTemplate, duplicated.result.resource_id)
            assert row.label_config == config
    finally:
        if actor_id is not None:
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "DELETE FROM public.workspace_configuration_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor_id},
                )
                await db.execute(
                    delete(ProjectTemplate).where(
                        ProjectTemplate.created_by == actor_id
                    )
                )
                await db.execute(delete(User).where(User.id == actor_id))
