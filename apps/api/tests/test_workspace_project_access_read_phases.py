"""Compare real legacy/new reads when committed state changes between phases."""

from contextlib import asynccontextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_workspace.project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import project_query_request
from aap_workspace_contract.project_query_values import materialize_project_query_data
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.schemas.project import ProjectAccessOut
from app.services.workspace_project_query_data import WorkspaceProjectQueryData
from tests.factory import create_membership, create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _legacy_reader(tmp_path):
    root = Path(__file__).resolve().parents[3]
    reference = json.loads(
        (
            Path(__file__).parent
            / "fixtures/workspace_access_reads_legacy/samples.json"
        ).read_text()
    )
    relative = "apps/api/app/services/project_access.py"
    raw = subprocess.check_output(
        ["git", "show", reference["source"]["pre_extraction_base"] + ":" + relative],
        cwd=root,
    )
    assert (
        hashlib.sha256(raw).hexdigest()
        == reference["source"]["runtime_source_sha256"][relative]
    )
    path = tmp_path / "frozen_legacy_project_access.py"
    # The frozen implementation keeps its original hash; only the import of
    # the byte-identical relocated policy changes for this executable probe.
    assert raw.count(b"from aap_workspace.access_contract import") == 1
    path.write_bytes(
        raw.replace(
            b"from aap_workspace.access_contract import",
            b"from aap_workspace_contract.access import",
        )
    )
    spec = importlib.util.spec_from_file_location("frozen_legacy_project_access", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public_access(value):
    return ProjectAccessOut(
        project_id=value.project_id,
        user_id=value.user_id,
        platform_role=value.platform_role,
        project_role=value.project_role,
        membership_id=value.membership_id,
        membership_version=value.membership_version,
        access_kind=value.access_kind,
        is_manager=value.is_manager,
        capabilities=sorted(value.capabilities),
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "change", ["role_version", "inactive_and_missing", "late_temporal"]
)
async def test_final_access_uses_second_authority_read_without_extra_initial_row(
    test_engine, tmp_path, change
):
    legacy = _legacy_reader(tmp_path)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    results = []
    for implementation in ("legacy", "owner"):
        users, project_id = [], None
        actions = []
        try:
            async with maker.begin() as db:
                owner = await create_user(
                    db,
                    "project_admin",
                    f"access-phase-{uuid.uuid4().hex}@example.test",
                    "Owner",
                )
                actor = await create_user(
                    db,
                    "employee",
                    f"access-phase-{uuid.uuid4().hex}@example.test",
                    "Member",
                )
                users = [owner.id, actor.id]
                project = await create_project(db, owner_id=owner.id)
                project_id = project.id
                member = await create_membership(
                    db, project_id=project_id, user_id=actor.id, role="annotator"
                )
                member_id = member.id

            async def mutate():
                async with maker.begin() as db:
                    if change == "role_version":
                        await db.execute(
                            text(
                                "UPDATE public.project_members SET role='reviewer',version=2 WHERE id=:id"
                            ),
                            {"id": member_id},
                        )
                    elif change == "inactive_and_missing":
                        await db.execute(
                            text(
                                "UPDATE public.users SET is_active=false WHERE id=:id"
                            ),
                            {"id": actor.id},
                        )
                        await db.execute(
                            delete(Project).where(Project.id == project_id)
                        )
                    else:
                        await db.execute(
                            text(
                                "UPDATE public.project_members SET updated_at=TIMESTAMPTZ '10000-01-01 00:00:00+00' WHERE id=:id"
                            ),
                            {"id": member_id},
                        )

            async with maker() as db:
                try:
                    if implementation == "legacy":
                        initial, _ = await legacy.resolve_project_access_by_id(
                            db, actor_id=actor.id, project_id=project_id
                        )
                        await mutate()
                        result = _public_access(
                            await legacy.resolve_project_access(
                                db, actor_id=actor.id, project=initial
                            )
                        )
                    else:
                        binding = WorkspaceProjectQueryData(db)

                        class ChangedBetweenPhases:
                            @asynccontextmanager
                            async def transaction(self):
                                changed = False
                                async with binding.transaction() as transaction:

                                    class Reads:
                                        async def step(
                                            self,
                                            request,
                                            action,
                                            parameters=None,
                                            **kwargs,
                                        ):
                                            nonlocal changed
                                            actions.append(action)
                                            facts = await transaction.step(
                                                request,
                                                action,
                                                parameters=parameters,
                                                **kwargs,
                                            )
                                            if (
                                                action == "access_member_rows"
                                                and not changed
                                            ):
                                                changed = True
                                                await mutate()
                                            return facts

                                    yield Reads()

                        reply = await ProjectQueryManager(
                            ChangedBetweenPhases(), None
                        ).execute(
                            project_query_request(
                                "get_project_access",
                                actor.id,
                                actor.role,
                                target_id=project_id,
                            )
                        )
                        if not reply.ok:
                            raise HTTPException(
                                status_code=reply.error.status_code,
                                detail=reply.error.detail,
                                headers=reply.error.headers,
                            )
                        result = ProjectAccessOut(
                            **materialize_project_query_data(reply)
                        ).model_dump(mode="json")
                    aliases = {
                        str(actor.id): "$actor",
                        str(project_id): "$project",
                        str(member_id): "$member",
                    }
                    result = {
                        key: aliases.get(value, value)
                        if isinstance(value, str)
                        else value
                        for key, value in result.items()
                    }
                    results.append({"status": 200, "body": result})
                except HTTPException as error:
                    results.append(
                        {
                            "status": error.status_code,
                            "detail": error.detail,
                            "headers": error.headers,
                        }
                    )
                except (DBAPIError, ValueError, OverflowError):
                    results.append({"status": 500})
                finally:
                    await db.rollback()

            if implementation == "owner":
                prefix = [
                    "get_row",
                    "actor_status",
                    "get_row",
                    "access_member_rows",
                    "actor_status",
                ]
                expected = (
                    prefix
                    if change == "inactive_and_missing"
                    else prefix + ["get_row", "access_member_rows"]
                )
                assert actions == expected
        finally:
            async with maker.begin() as db:
                if project_id is not None:
                    await db.execute(
                        delete(ProjectMember).where(
                            ProjectMember.project_id == project_id
                        )
                    )
                    await db.execute(delete(Project).where(Project.id == project_id))
                if users:
                    await db.execute(delete(User).where(User.id.in_(users)))
    assert results[0] == results[1]
    if change == "role_version":
        assert results[1]["status"] == 200
        assert results[1]["body"]["project_role"] == "reviewer"
        assert results[1]["body"]["membership_version"] == 2
    elif change == "inactive_and_missing":
        assert results[1] == {
            "status": 401,
            "detail": "账号已停用或不存在",
            "headers": {"WWW-Authenticate": "Bearer"},
        }
    else:
        assert results[1]["status"] == 500
