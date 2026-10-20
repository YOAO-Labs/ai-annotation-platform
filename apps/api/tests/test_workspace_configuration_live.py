"""Owned PostgreSQL acceptance for Workspace configuration DSN and HTTP owners."""

from __future__ import annotations

import os
import secrets
import socket
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_user
from tests.workspace_configuration_helpers import (
    api_configuration_binding,
    configuration_dsn,
    provider_url,
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


def _fixed_configuration_requests(monkeypatch):
    from app.services import workspace_configuration

    operation_ids = {operation: uuid.uuid4() for operation in _OPERATIONS}
    requests = {}
    original = workspace_configuration.configuration_request

    def build(**kwargs):
        operation = kwargs["operation"]
        request = original(**kwargs, operation_id=operation_ids[operation])
        requests[operation] = request
        return request

    monkeypatch.setattr(workspace_configuration, "configuration_request", build)
    return operation_ids, requests


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
            "description": "created through the selected owner",
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


@pytest.mark.parametrize("binding", ["dsn", "http"])
async def test_public_configuration_commands_use_selected_dsn_or_http_owner(
    test_engine, httpx_client, monkeypatch, binding
):
    dsn = configuration_dsn(test_engine)
    control = secrets.token_urlsafe(32)
    maker, actor = await _seed_actor(test_engine, binding)
    operation_ids, requests = _fixed_configuration_requests(monkeypatch)
    _suppress_request_audit(monkeypatch)
    template_id = project_id = None
    label = f"{binding}-{uuid.uuid4().hex[:8]}"
    try:
        if binding == "dsn":
            async with api_configuration_binding(dsn=dsn, token=control):
                (
                    template_id,
                    project_id,
                    template_name,
                    project_name,
                ) = await _public_configuration_commands(httpx_client, actor, label)
        else:
            drop_id = operation_ids["create_template"]
            async with provider_url(
                dsn, control, drop_operation_id=str(drop_id)
            ) as origin:
                async with api_configuration_binding(url=origin, token=control):
                    (
                        template_id,
                        project_id,
                        template_name,
                        project_name,
                    ) = await _public_configuration_commands(httpx_client, actor, label)

            assert requests["create_template"].operation_id == drop_id
            async with provider_url(dsn, control) as origin:
                async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
                    replay = await client.post(
                        origin + _CONFIGURATION_PATH,
                        headers={"Authorization": f"Bearer {control}"},
                        json=requests["create_template"].model_dump(
                            mode="json", by_alias=True
                        ),
                    )
            assert replay.status_code == 200, replay.text
            replay_reply = replay.json()
            assert replay_reply["ok"] is True
            assert replay_reply["operation_id"] == str(drop_id)
            assert set(replay_reply["result"]) == {"resource_id", "template_id"}
            assert replay_reply["result"]["resource_id"] == str(template_id)
            assert replay_reply["result"]["template_id"] is None

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


async def test_unavailable_selected_configuration_owner_does_not_fall_back(
    test_engine, httpx_client, monkeypatch
):
    maker, actor = await _seed_actor(test_engine, "unavailable")
    operation_ids, _requests = _fixed_configuration_requests(monkeypatch)
    _suppress_request_audit(monkeypatch)
    project_name = f"Unavailable configuration {uuid.uuid4().hex}"
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            unused_port = reservation.getsockname()[1]
            async with api_configuration_binding(
                url=f"http://127.0.0.1:{unused_port}",
                token=secrets.token_urlsafe(32),
            ):
                response = await httpx_client.post(
                    "/api/v1/projects",
                    json={"name": project_name, "type_label": "Image"},
                    headers=_auth(actor),
                )
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "项目配置服务暂不可用"
        async with maker() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.projects WHERE name=:name"),
                    {"name": project_name},
                )
                == 0
            )
            assert await _counts_for_operation(db, operation_ids["create_project"]) == (
                0,
                0,
                0,
            )
    finally:
        await _cleanup_world(maker, actor.id, operation_ids)
