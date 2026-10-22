"""The local write-policy port preserves transaction authority and HTTP facts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application import legacy_file_upload
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services import predictions_import, project_access, project_write_guard
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
)
from tests.factory import create_membership, create_project, create_user


def _access(*, is_manager: bool = False) -> ProjectAccess:
    return ProjectAccess(
        user_id=uuid4(),
        project_id=uuid4(),
        platform_role="employee",
        project_role="reviewer",
        membership_id=uuid4(),
        membership_version=1,
        access_kind="member",
        capabilities=frozenset(),
        is_manager=is_manager,
    )


class _Resolver:
    def __init__(self, *, access: ProjectAccess | None = None, failure=None):
        self.access = access
        self.failure = failure
        self.request = None

    async def resolve(self, request: ProjectWritePolicyRequest) -> ProjectAccess:
        self.request = request
        if self.failure is not None:
            raise self.failure
        assert self.access is not None
        return self.access


async def _invoke_consumer(monkeypatch, consumer, resolver, *, manager_denial=None):
    monkeypatch.setattr(
        project_write_guard,
        "bind_project_write_policy_resolver",
        lambda _db: resolver,
    )
    monkeypatch.setattr(
        project_access, "bind_project_write_policy_resolver", lambda _db: resolver
    )
    monkeypatch.setattr(
        legacy_file_upload,
        "bind_project_write_policy_resolver",
        lambda _db: resolver,
    )
    if consumer == "manager":
        if manager_denial is None:
            await project_write_guard.lock_project_manager(object(), uuid4(), uuid4())
        else:
            await project_write_guard.lock_project_manager(
                object(), uuid4(), uuid4(), denial_detail=manager_denial
            )
    elif consumer == "prediction_import":
        await predictions_import._hold_import_authority(
            object(), actor_id=uuid4(), project_id=uuid4()
        )
    else:
        workflow = object.__new__(legacy_file_upload.LegacyFileUploadWorkflow)
        workflow.db = object()
        await workflow._manager_access(uuid4(), uuid4(), lock=True)


@pytest.mark.asyncio
async def test_policy_port_holds_authority_and_refreshes_cached_membership(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await create_user(
            setup, "project_admin", f"policy-owner-{uuid4()}@test.local", "Owner"
        )
        member = await create_user(
            setup, "employee", f"policy-member-{uuid4()}@test.local", "Member"
        )
        project = await create_project(setup, owner_id=owner.id)
        membership = await create_membership(
            setup, project_id=project.id, user_id=member.id, role="annotator"
        )
        user_ids, actor_id, project_id, membership_id = (
            [owner.id, member.id],
            member.id,
            project.id,
            membership.id,
        )
        await setup.commit()
    try:
        async with maker() as writer:
            cached_member = await writer.get(ProjectMember, membership_id)
            resolver = project_access.bind_project_write_policy_resolver(writer)
            request = ProjectWritePolicyRequest(actor_id, project_id)
            access = await resolver.resolve(request)
            assert (access.user_id, access.project_id, access.membership_id) == (
                actor_id,
                project_id,
                membership_id,
            )
            assert access.has("annotation.write")
            for model, identity in (
                (User, actor_id),
                (Project, project_id),
                (ProjectMember, membership_id),
            ):
                async with maker() as revoker:
                    with pytest.raises(DBAPIError) as busy:
                        await revoker.execute(
                            select(model.id)
                            .where(model.id == identity)
                            .with_for_update(nowait=True)
                        )
                    assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
                    await revoker.rollback()
            await writer.commit()
            async with maker() as revoker:
                await revoker.execute(
                    update(ProjectMember)
                    .where(ProjectMember.id == membership_id)
                    .values(role="viewer", version=2)
                )
                await revoker.commit()
            assert cached_member.role == "annotator"
            refreshed = await resolver.resolve(request)
            assert (refreshed.project_role, refreshed.membership_version) == (
                "viewer",
                2,
            )
            assert not refreshed.has("annotation.write")
            assert access.project_role == "annotator"
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail", "headers"),
    [
        (401, "账号已停用或不存在", {"WWW-Authenticate": "Bearer"}),
        (403, "项目职责无效，访问被拒绝", None),
        (404, "项目不存在", None),
        (409, {"reason": "project_busy"}, None),
    ],
)
async def test_sql_resolver_projects_expected_http_failures_to_pure_facts(
    monkeypatch, status_code, detail, headers
):
    source = HTTPException(status_code=status_code, detail=detail, headers=headers)

    async def lock_rows(*_args, **_kwargs):
        raise source

    monkeypatch.setattr(project_access, "lock_current_project_access", lock_rows)
    resolver = project_access.SqlProjectWritePolicyResolver(object())

    with pytest.raises(ProjectWritePolicyFailure) as raised:
        await resolver.resolve(ProjectWritePolicyRequest(uuid4(), uuid4()))

    assert raised.value.status_code == source.status_code
    assert raised.value.detail == source.detail
    assert raised.value.headers == source.headers
    assert raised.value.__cause__ is source


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["manager", "prediction_import", "legacy_upload"])
@pytest.mark.parametrize(
    ("status_code", "detail", "headers"),
    [
        (401, "账号已停用或不存在", {"WWW-Authenticate": "Bearer"}),
        (403, "项目职责无效，访问被拒绝", None),
        (404, "项目不存在", None),
        (409, {"reason": "membership_busy"}, None),
    ],
)
async def test_access_only_consumers_restore_http_failures_at_their_boundary(
    monkeypatch, consumer, status_code, detail, headers
):
    failure = ProjectWritePolicyFailure(status_code, detail, headers)
    with pytest.raises(HTTPException) as raised:
        await _invoke_consumer(monkeypatch, consumer, _Resolver(failure=failure))
    assert raised.value.status_code == status_code
    assert raised.value.detail == detail
    assert raised.value.headers == headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("consumer", "expected_detail"),
    [
        ("manager", "custom manager denial"),
        ("prediction_import", "项目管理权限已变化"),
        ("legacy_upload", "仅项目负责人或超级管理员可执行"),
    ],
)
async def test_manager_denial_details_remain_at_the_consuming_boundary(
    monkeypatch, consumer, expected_detail
):
    with pytest.raises(HTTPException) as raised:
        await _invoke_consumer(
            monkeypatch,
            consumer,
            _Resolver(access=_access()),
            manager_denial=expected_detail if consumer == "manager" else None,
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == expected_detail
