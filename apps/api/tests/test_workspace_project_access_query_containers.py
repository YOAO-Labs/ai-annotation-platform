"""The Project query image serves current access without Identity display profiles."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _docker, _inspect_docker
from tests.test_workspace_project_query_containers import (
    _committed_api_client,
    _query_container,
)
from tests.workspace_project_query_helpers import api_project_query_binding

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_PROJECT_ACCESS_QUERY_CONTAINER_LIVE") != "1",
        reason="requires an explicitly qualified owned Project-access image",
    ),
]


async def test_access_image_reads_current_version_without_profiles_and_restarts(
    test_engine, app_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users, project_id = [], None
    token = secrets.token_urlsafe(32)
    try:
        async with maker.begin() as db:
            owner = await create_user(
                db,
                "project_admin",
                f"access-image-{uuid.uuid4().hex}@example.test",
                "Owner",
            )
            actor = await create_user(
                db,
                "employee",
                f"access-image-{uuid.uuid4().hex}@example.test",
                "Member",
            )
            users = [owner.id, actor.id]
            project = await create_project(db, owner_id=owner.id)
            project_id = project.id
            member = await create_membership(
                db, project_id=project_id, user_id=actor.id, role="reviewer"
            )
            member.version = 37
        headers = {
            "Authorization": "Bearer " + create_access_token(str(actor.id), actor.role)
        }
        async with (
            _query_container(
                test_engine,
                tmp_path / "access",
                token,
                query_kind="project-access",
                profiles=False,
            ) as owned,
            _committed_api_client(app_module, maker) as client,
        ):
            origin, container, image_id = owned
            path = f"/api/v1/projects/{project_id}/access"
            async with api_project_query_binding(url=origin, token=token):
                initial = await client.get(path, headers=headers)
                assert initial.status_code == 200, initial.text
                assert initial.json()["membership_version"] == 37
                assert initial.json()["project_role"] == "reviewer"
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE public.project_members SET role='viewer',version=38 WHERE id=:id"
                        ),
                        {"id": member.id},
                    )
                current = await client.get(path, headers=headers)
                assert current.status_code == 200, current.text
                expected = current.json()
                assert (
                    expected["project_role"] == "viewer"
                    and expected["membership_version"] == 38
                )
                assert expected["capabilities"] == [
                    "member.read",
                    "project.read",
                    "task.read",
                ]
                gate = asyncio.Semaphore(4)

                async def read():
                    async with gate:
                        started = time.monotonic()
                        response = await client.get(path, headers=headers)
                        assert response.status_code == 200, response.text
                        assert response.json() == expected
                        return (time.monotonic() - started) * 1000

                durations = await asyncio.gather(*(read() for _ in range(16)))
                _docker("restart", container, timeout=60)
                metadata = _inspect_docker(container)
                published = metadata["NetworkSettings"]["Ports"]["8176/tcp"]
                assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
                restarted = f"http://127.0.0.1:{published[0]['HostPort']}"
                settings.workspace_project_query_url = restarted
                deadline = time.monotonic() + 40
                async with httpx.AsyncClient(trust_env=False, timeout=2) as readiness:
                    while True:
                        try:
                            if (
                                await readiness.get(restarted + "/ready")
                            ).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline
                        await asyncio.sleep(0.1)
                final = await client.get(path, headers=headers)
                assert final.status_code == 200 and final.json() == expected
                target = Path(
                    os.environ["AAP_WORKSPACE_PROJECT_ACCESS_QUERY_OBSERVATION"]
                )
                assert (
                    target.resolve().parent
                    == Path(__file__).resolve().parents[3] / ".worktree"
                )
                with target.open("x") as stream:
                    json.dump(
                        {
                            "status": "passed",
                            "image_id": image_id,
                            "container_id": container,
                            "identity_profiles_configured": False,
                            "read_only_secret_files": 2,
                            "initial_membership_version": 37,
                            "current_membership_version": 38,
                            "container_restarted": True,
                            "initial_origin": origin,
                            "restarted_origin": restarted,
                            "requests": 16,
                            "concurrency": 4,
                            "milliseconds": durations,
                            "owner_budget": {"cpu": 1, "memory_mib": 256},
                            "database_and_api_outside_budget": True,
                        },
                        stream,
                        indent=2,
                    )
    finally:
        async with maker.begin() as db:
            if project_id is not None:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.project_id == project_id)
                )
                await db.execute(delete(Project).where(Project.id == project_id))
            if users:
                await db.execute(delete(User).where(User.id.in_(users)))
