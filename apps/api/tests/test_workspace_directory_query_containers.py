"""Directory image qualification with real public reads and bounded concurrency."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import httpx
import pytest
from sqlalchemy import delete
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
from tests.workspace_directory_query_helpers import api_directory_query_binding

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_DIRECTORY_QUERY_CONTAINER_LIVE") != "1",
        reason="requires an explicitly qualified owned Directory image",
    ),
]


async def test_directory_image_public_reads_restart_and_bounded_concurrency(
    test_engine, app_module, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    users, project_id = [], None
    token = secrets.token_urlsafe(32)
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"directory-image-{uuid.uuid4().hex}@example.test",
                "Directory owner",
            )
            member = await create_user(
                db,
                "employee",
                f"directory-image-{uuid.uuid4().hex}@example.test",
                "目录成员ß",
            )
            member.avatar_ref = "preset:blue"
            users = [actor.id, member.id]
            project = await create_project(db, owner_id=actor.id)
            project_id = project.id
            await create_membership(
                db, project_id=project_id, user_id=member.id, role="annotator"
            )
        headers = {
            "Authorization": "Bearer " + create_access_token(str(actor.id), actor.role)
        }
        async with (
            _query_container(
                test_engine, tmp_path / "directory", token, query_kind="directory"
            ) as owned,
            _committed_api_client(app_module, maker) as client,
        ):
            origin, container, image_id = owned
            async with api_directory_query_binding(url=origin, token=token):
                paths = [
                    f"/api/v1/projects/{project_id}/{view}"
                    for view in ("members", "mention-candidates")
                ]
                expected = []
                for path in paths:
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200, response.text
                    expected.append(response.json())
                assert expected[0][0]["user_name"] == "目录成员ß"
                assert expected[0][0]["avatar_ref"] == "preset:blue"
                assert [row["kind"] for row in expected[1]] == ["owner", "member"]
                semaphore = asyncio.Semaphore(4)

                async def read(index):
                    async with semaphore:
                        started = time.monotonic()
                        response = await client.get(paths[index % 2], headers=headers)
                        assert response.status_code == 200, response.text
                        assert response.json() == expected[index % 2]
                        return (time.monotonic() - started) * 1000

                durations = await asyncio.gather(*(read(index) for index in range(16)))
                details = _inspect_docker(container)
                assert not details["State"]["OOMKilled"]
                assert (
                    details["Config"]["Labels"]["org.aap.acceptance"]
                    == "module-data-boundary"
                )
                _docker("restart", container, timeout=60)
                restarted = _inspect_docker(container)
                published = restarted["NetworkSettings"]["Ports"]["8177/tcp"]
                assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
                restarted_origin = f"http://127.0.0.1:{published[0]['HostPort']}"
                # Docker may change an ephemeral host port after restart.
                settings.workspace_directory_query_url = restarted_origin
                deadline = time.monotonic() + 40
                async with httpx.AsyncClient(trust_env=False, timeout=2) as readiness:
                    while True:
                        try:
                            if (
                                await readiness.get(restarted_origin + "/ready")
                            ).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        assert time.monotonic() < deadline
                        await asyncio.sleep(0.1)
                assert (await client.get(paths[0], headers=headers)).json() == expected[
                    0
                ]
                assert (await client.get(paths[1], headers=headers)).json() == expected[
                    1
                ]
                report_path = Path(
                    os.environ["AAP_WORKSPACE_DIRECTORY_QUERY_OBSERVATION"]
                )
                assert (
                    report_path.resolve().parent
                    == Path(__file__).resolve().parents[3] / ".worktree"
                )
                with report_path.open("x") as stream:
                    json.dump(
                        {
                            "status": "passed",
                            "image_id": image_id,
                            "container_id": container,
                            "requests": 16,
                            "concurrency": 4,
                            "milliseconds": durations,
                            "container_restarted": True,
                            "initial_origin": origin,
                            "restarted_origin": restarted_origin,
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
