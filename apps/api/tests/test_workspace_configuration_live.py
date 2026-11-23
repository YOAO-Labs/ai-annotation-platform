"""Public configuration parity and independent owner replay on owned state."""

from __future__ import annotations

import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_workspace.configuration_contract import configuration_request
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_user
from tests.workspace_configuration_helpers import (
    configuration_dsn,
    provider_url,
    public_workspace_client,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable Workspace configuration verifier",
    ),
]

_CONFIGURATION_PATH = "/v1/workspace/configuration/execute"
_OPERATIONS = (
    "create_template",
    "update_template",
    "create_project",
    "update_project",
)


def _auth(actor: User) -> dict[str, str]:
    token = create_access_token(str(actor.id), actor.role)
    return {"Authorization": f"Bearer {token}"}


def _suppress_request_audit(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)


def _fixed_configuration_operation_ids(monkeypatch):
    from app.services import workspace_configuration

    operation_ids = {operation: uuid.uuid4() for operation in _OPERATIONS}
    original = workspace_configuration.configuration_request

    def build(**kwargs):
        operation = kwargs["operation"]
        return original(**kwargs, operation_id=operation_ids[operation])

    monkeypatch.setattr(workspace_configuration, "configuration_request", build)
    return operation_ids


async def _seed_actor(test_engine, label: str):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        actor = await create_user(
            db,
            "project_admin",
            f"workspace-configuration-{label}-{uuid.uuid4().hex}@example.test",
            "Workspace configuration actor",
        )
    return maker, actor


async def _counts_for_operation(db, operation_id: uuid.UUID) -> tuple[int, int, int]:
    params = {"operation_id": str(operation_id)}
    receipt_count = await db.scalar(
        text(
            "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
            "WHERE operation_id=CAST(:operation_id AS uuid)"
        ),
        params,
    )
    state_count = await db.scalar(
        text(
            "SELECT count(*) FROM public.workspace_configuration_operation_states "
            "WHERE operation_id=CAST(:operation_id AS uuid)"
        ),
        params,
    )
    attempt_count = await db.scalar(
        text(
            "SELECT count(*) FROM public.workspace_configuration_operation_attempts "
            "WHERE operation_id=CAST(:operation_id AS uuid)"
        ),
        params,
    )
    return int(receipt_count), int(state_count), int(attempt_count)


async def _cleanup_world(
    maker, actor_id: uuid.UUID, operation_ids: dict[str, uuid.UUID]
) -> None:
    async with maker.begin() as db:
        for operation_id in operation_ids.values():
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
        await db.execute(
            text(
                "DELETE FROM public.workspace_configuration_operation_receipts "
                "WHERE actor_id=CAST(:actor_id AS uuid)"
            ),
            {"actor_id": str(actor_id)},
        )
        await db.execute(
            delete(ProjectTemplate).where(ProjectTemplate.created_by == actor_id)
        )
        await db.execute(delete(Project).where(Project.owner_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def _public_configuration_commands(client, actor, label: str):
    headers = _auth(actor)
    template_name = f"Configuration {label} template"
    project_name = f"Configuration {label} project"
    created_template = await client.post(
        "/api/v1/project-templates",
        json={
            "name": template_name,
            "description": "created through the public API",
            "type_label": "Image",
            "type_key": "image-det",
            "box_threshold": 0.41,
            "scope": "private",
        },
        headers=headers,
    )
    assert created_template.status_code == 201, created_template.text
    template = created_template.json()
    template_id = uuid.UUID(template["id"])
    assert template["name"] == template_name and template["scope"] == "private"

    updated_template_name = f"{template_name} updated"
    updated_template = await client.patch(
        f"/api/v1/project-templates/{template_id}",
        json={"name": updated_template_name, "box_threshold": 0.61},
        headers=headers,
    )
    assert updated_template.status_code == 200, updated_template.text
    assert updated_template.json()["name"] == updated_template_name

    created_project = await client.post(
        "/api/v1/projects",
        json={"name": project_name, "template_id": str(template_id)},
        headers=headers,
    )
    assert created_project.status_code == 200, created_project.text
    project = created_project.json()
    project_id = uuid.UUID(project["id"])
    assert project["name"] == project_name
    assert project["box_threshold"] == pytest.approx(0.61)

    updated_project_name = f"{project_name} updated"
    updated_project = await client.patch(
        f"/api/v1/projects/{project_id}",
        json={"name": updated_project_name, "box_threshold": 0.72},
        headers=headers,
    )
    assert updated_project.status_code == 200, updated_project.text
    assert updated_project.json()["name"] == updated_project_name
    assert updated_project.json()["box_threshold"] == pytest.approx(0.72)
    return template_id, project_id, updated_template_name, updated_project_name


async def test_public_configuration_commands_use_caller_session(
    test_engine, app_module, monkeypatch
):
    maker, actor = await _seed_actor(test_engine, "caller")
    operation_ids = _fixed_configuration_operation_ids(monkeypatch)
    label = uuid.uuid4().hex[:8]
    try:
        async with public_workspace_client(
            test_engine, app_module, monkeypatch
        ) as client:
            (
                template_id,
                project_id,
                template_name,
                project_name,
            ) = await _public_configuration_commands(client, actor, label)

        async with maker() as db:
            template = await db.get(ProjectTemplate, template_id)
            project = await db.get(Project, project_id)
            assert template is not None and project is not None
            assert template.name == template_name
            assert template.box_threshold == pytest.approx(0.61)
            assert template.scope == "private" and template.created_by == actor.id
            assert project.name == project_name
            assert project.box_threshold == pytest.approx(0.72)
            assert await db.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
                    "WHERE actor_id=CAST(:actor_id AS uuid)"
                ),
                {"actor_id": str(actor.id)},
            ) == len(_OPERATIONS)
            for operation_id in operation_ids.values():
                assert await _counts_for_operation(db, operation_id) == (1, 0, 0)
    finally:
        await _cleanup_world(maker, actor.id, operation_ids)


async def test_standalone_configuration_receipt_survives_lost_reply_and_restart(
    test_engine,
):
    dsn = configuration_dsn(test_engine)
    control = secrets.token_urlsafe(32)
    maker, actor = await _seed_actor(test_engine, "standalone-replay")
    request = configuration_request(
        operation="create_template",
        actor_id=actor.id,
        target_id=None,
        payload={
            "name": f"Standalone template {uuid.uuid4().hex}",
            "type_label": "Image",
            "type_key": "image-det",
            "scope": "private",
        },
    )
    command = request.model_dump(mode="json", by_alias=True)
    headers = {"Authorization": f"Bearer {control}"}
    operation_id = request.operation_id
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
            async with provider_url(
                dsn, control, drop_operation_id=operation_id
            ) as origin:
                with pytest.raises(httpx.RequestError):
                    await client.post(
                        origin + _CONFIGURATION_PATH, headers=headers, json=command
                    )
            async with maker() as observer:
                assert await _counts_for_operation(observer, operation_id) == (1, 0, 0)
                template_id = (
                    await observer.scalars(
                        select(ProjectTemplate.id).where(
                            ProjectTemplate.created_by == actor.id
                        )
                    )
                ).one()
            async with provider_url(dsn, control) as origin:
                replay = await client.post(
                    origin + _CONFIGURATION_PATH, headers=headers, json=command
                )
        assert replay.status_code == 200, replay.text
        replay_reply = replay.json()
        assert replay_reply["ok"] is True
        assert replay_reply["operation_id"] == str(operation_id)
        assert replay_reply["result"] == {
            "resource_id": str(template_id),
            "template_id": None,
        }
    finally:
        await _cleanup_world(maker, actor.id, {"create_template": operation_id})
