"""Atomic failure and commit-guard checks for the configuration owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_columns import encode_config_columns
from aap_workspace.configuration_contract import (
    ConfigurationStepSuccess,
    ConfigurationUnavailable,
    configuration_request,
    decode_configuration_step_reply,
)
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.workspace_configuration_helpers import configuration_dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace configuration verifier",
    ),
]


def _request(operation, actor_id, target_id, payload, operation_id):
    return configuration_request(
        operation=operation,
        actor_id=actor_id,
        target_id=target_id,
        payload=payload,
        operation_id=operation_id,
    )


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        state = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if state:
            return state
        error = getattr(error, "orig", None) or error.__cause__
    return None


async def _counts(db, operation_id: uuid.UUID) -> tuple[int, int, int]:
    params = {"operation_id": str(operation_id)}
    counts = []
    for table in (
        "workspace_configuration_operation_receipts",
        "workspace_configuration_operation_states",
        "workspace_configuration_operation_attempts",
    ):
        counts.append(
            int(
                await db.scalar(
                    text(
                        f"SELECT count(*) FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
            )
        )
    return tuple(counts)


async def _cleanup(test_engine, actor_id: uuid.UUID, operation_ids) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        for operation_id in operation_ids:
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
                "workspace_configuration_operation_receipts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(Project).where(Project.owner_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def test_selected_dsn_native_text_and_json_failures_roll_back_atomically(
    test_engine,
):
    dsn = configuration_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = project_id = template_id = None
    operation_ids = {
        "null_project": uuid.uuid4(),
        "nul_jsonb": uuid.uuid4(),
        "nul_text": uuid.uuid4(),
    }
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"workspace-config-failure-{uuid.uuid4().hex}@example.test",
                "Workspace configuration failure actor",
            )
            project = await create_project(
                db,
                owner_id=actor.id,
                name="Workspace configuration failure source",
            )
            template = ProjectTemplate(
                id=uuid.uuid4(),
                display_id="PT-" + uuid.uuid4().hex[:8],
                name="Workspace configuration failure template",
                type_label="Image",
                type_key="image-det",
                created_by=actor.id,
            )
            db.add(template)
            await db.flush()
            actor_id, project_id, template_id = actor.id, project.id, template.id

        async with maker() as observer:
            project_before = await observer.get(Project, project_id)
            template_before = await observer.get(ProjectTemplate, template_id)
            project_snapshot = (
                project_before.box_threshold,
                project_before.rendering_config,
                project_before.updated_at,
            )
            template_snapshot = (template_before.name, template_before.updated_at)

        manager = ConfigurationManager(ConfigurationPostgresData(dsn))
        cases = (
            (
                "update_project",
                project_id,
                {"box_threshold": None},
                "23502",
                operation_ids["null_project"],
            ),
            (
                "create_template",
                None,
                {
                    "name": f"Rejected JSONB template {uuid.uuid4().hex}",
                    "type_label": "Image",
                    "type_key": "image-det",
                    "label_config": {"marker": "\x00"},
                },
                "22P05",
                operation_ids["nul_jsonb"],
            ),
            (
                "create_template",
                None,
                {
                    "name": f"Rejected\x00template-{uuid.uuid4().hex}",
                    "type_label": "Image",
                    "type_key": "image-det",
                    "scope": "private",
                },
                "22021",
                operation_ids["nul_text"],
            ),
        )
        rejected_template_name = cases[1][2]["name"]
        for operation, target_id, payload, expected_state, operation_id in cases:
            request = _request(
                operation,
                actor_id,
                target_id,
                payload,
                operation_id,
            )
            with pytest.raises(asyncpg.PostgresError) as captured:
                await manager.execute(request)
            assert _sqlstate(captured.value) == expected_state

        async with maker() as observer:
            project_after = await observer.get(Project, project_id)
            template_after = await observer.get(ProjectTemplate, template_id)
            assert (
                project_after.box_threshold,
                project_after.rendering_config,
                project_after.updated_at,
            ) == project_snapshot
            assert (template_after.name, template_after.updated_at) == template_snapshot
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.project_templates WHERE name=:name"
                    ),
                    {"name": rejected_template_name},
                )
                == 0
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.project_templates "
                        "WHERE created_by=CAST(:actor_id AS uuid)"
                    ),
                    {"actor_id": str(actor_id)},
                )
                == 1
            )
            for operation_id in operation_ids.values():
                assert await _counts(observer, operation_id) == (0, 0, 0)
    finally:
        if actor_id is not None:
            await _cleanup(test_engine, actor_id, operation_ids.values())


class _WrongFinishTransaction:
    def __init__(self, transaction):
        self.transaction = transaction

    @property
    def autoflush(self):
        return self.transaction.autoflush

    async def before_project_insert(self):
        await self.transaction.before_project_insert()

    async def step(self, request_json, action, args, columns, values):
        reply = await self.transaction.step(request_json, action, args, columns, values)
        if action != "finish":
            return reply
        value = json.loads(reply) if isinstance(reply, (str, bytes)) else dict(reply)
        value["facts"]["resource_id"] = str(uuid.uuid4())
        return json.dumps(value, ensure_ascii=True)


class _WrongFinishData:
    def __init__(self, data):
        self.data = data

    @asynccontextmanager
    async def transaction(self, *, write):
        async with self.data.transaction(write=write) as transaction:
            yield _WrongFinishTransaction(transaction)


async def test_mismatched_native_finish_reply_rolls_back_project_and_receipt(
    test_engine,
):
    dsn = configuration_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    operation_id = uuid.uuid4()
    project_name = f"Wrong finish reply {uuid.uuid4().hex}"
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"workspace-config-finish-{uuid.uuid4().hex}@example.test",
                "Workspace configuration finish actor",
            )
            actor_id = actor.id

        request = _request(
            "create_project",
            actor_id,
            None,
            {
                "name": project_name,
                "type_label": "Image",
                "type_key": "image-det",
            },
            operation_id,
        )
        manager = ConfigurationManager(_WrongFinishData(ConfigurationPostgresData(dsn)))
        with pytest.raises(ConfigurationUnavailable):
            await manager.execute(request)

        async with maker() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM public.projects WHERE name=:name"),
                    {"name": project_name},
                )
                == 0
            )
            assert await _counts(observer, operation_id) == (0, 0, 0)
    finally:
        if actor_id is not None:
            await _cleanup(test_engine, actor_id, [operation_id])


async def test_native_create_project_without_finish_cannot_commit(test_engine):
    dsn = configuration_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    operation_id = uuid.uuid4()
    project_name = f"Unfinished native create {uuid.uuid4().hex}"
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"workspace-config-unfinished-{uuid.uuid4().hex}@example.test",
                "Workspace configuration unfinished actor",
            )
            actor_id = actor.id

        payload = {
            "name": project_name,
            "type_label": "Image",
            "type_key": "image-det",
        }
        request = _request("create_project", actor_id, None, payload, operation_id)
        request_json = request.model_dump(mode="json", by_alias=True)
        columns, values = encode_config_columns("project", payload)
        data = ConfigurationPostgresData(dsn)
        try:
            async with data.transaction(write=True) as transaction:
                for action, args, names, content in (
                    ("begin", {}, [], []),
                    ("authority", {"autoflush": True}, [], []),
                    ("allocate", {}, [], []),
                    ("insert", {}, columns, values),
                ):
                    raw = await transaction.step(
                        request_json, action, args, names, content
                    )
                    reply = decode_configuration_step_reply(request, action, raw)
                    assert isinstance(reply, ConfigurationStepSuccess), reply
        except asyncpg.PostgresError as error:
            assert _sqlstate(error) == "23514"
        else:
            raise AssertionError("The deferred unfinished-command guard allowed commit")

        async with maker() as observer:
            assert (
                await observer.scalar(
                    text("SELECT count(*) FROM public.projects WHERE name=:name"),
                    {"name": project_name},
                )
                == 0
            )
            assert await _counts(observer, operation_id) == (0, 0, 0)
    finally:
        if actor_id is not None:
            await _cleanup(test_engine, actor_id, [operation_id])
