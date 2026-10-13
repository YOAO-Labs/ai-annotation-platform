"""Project-admin read visibility vs manage scope for user management endpoints.

Issue #115: 项目管理员在成员分配中可见未分配标注员，但在用户与权限列表中不可见。
Read scope (list / stats / export via ``build_user_query``) includes enabled
annotators/reviewers (incl. unassigned ones, matching the member-assignment
candidate picker) and enabled super admins (read-only lookup).

Manage scope (maintainer revision, 2026-09-16): project admins manage every
*enabled* annotator/reviewer account (unassigned or in other projects'
memberships) for account-level writes — role switch, password reset, group
assignment.  Lifecycle writes that hand over work (deactivate / delete /
offboarding) additionally require the target's projects to be owned by the
actor, so unassigned accounts pass while straddling/foreign members stay
gated on a superior.  Super admins, other project admins and viewers are
never manageable by a project admin; deactivated accounts are out of scope.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project_member import ProjectMember
from tests.factory import create_membership, create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(principal: tuple) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def _seed_scope_world(db: AsyncSession, project_admin, super_admin):
    """Seed one project admin's surroundings.

    - own project with an active annotator member, an inactive reviewer member
      and a viewer member
    - unassigned active annotator / reviewer / viewer, another project admin,
      an inactive annotator and an inactive super admin (all outside the PA's
      projects)
    - a foreign project owned by the super admin with one annotator member
    """

    manager, _ = project_admin
    admin, _ = super_admin

    own_project = await create_project(db, owner_id=manager.id, name="PA Own")
    member = await create_user(db, "employee", "own-member@e.test", "Own Member")
    inactive_member = await create_user(
        db, "employee", "own-inactive@e.test", "Own Inactive"
    )
    inactive_member.is_active = False
    viewer_member = await create_user(db, "viewer", "own-viewer@e.test", "Own Viewer")
    db.add_all(
        [
            ProjectMember(
                project_id=own_project.id,
                user_id=member.id,
                role="annotator",
                assigned_by=manager.id,
            ),
            ProjectMember(
                project_id=own_project.id,
                user_id=inactive_member.id,
                role="reviewer",
                assigned_by=manager.id,
            ),
            ProjectMember(
                project_id=own_project.id,
                user_id=viewer_member.id,
                role="viewer",
                assigned_by=manager.id,
            ),
        ]
    )

    unassigned_annotator = await create_user(
        db, "employee", "free-annotator@e.test", "Free Annotator"
    )
    unassigned_reviewer = await create_user(
        db, "employee", "free-reviewer@e.test", "Free Reviewer"
    )
    unassigned_viewer = await create_user(
        db, "viewer", "free-viewer@e.test", "Free Viewer"
    )
    other_pa = await create_user(db, "project_admin", "other-pa@e.test", "Other PA")
    inactive_annotator = await create_user(
        db, "employee", "inactive-annotator@e.test", "Inactive Annotator"
    )
    inactive_annotator.is_active = False
    inactive_super_admin = await create_user(
        db, "super_admin", "inactive-sa@e.test", "Inactive SA"
    )
    inactive_super_admin.is_active = False

    foreign_project = await create_project(db, owner_id=admin.id, name="Foreign")
    foreign_member = await create_user(
        db, "employee", "foreign-member@e.test", "Foreign Member"
    )
    await create_membership(
        db,
        project_id=foreign_project.id,
        user_id=foreign_member.id,
        role="annotator",
        assigned_by=admin.id,
    )
    await db.flush()

    return SimpleNamespace(
        manager=manager,
        admin=admin,
        own_project=own_project,
        member=member,
        inactive_member=inactive_member,
        viewer_member=viewer_member,
        unassigned_annotator=unassigned_annotator,
        unassigned_reviewer=unassigned_reviewer,
        unassigned_viewer=unassigned_viewer,
        other_pa=other_pa,
        inactive_annotator=inactive_annotator,
        inactive_super_admin=inactive_super_admin,
        foreign_project=foreign_project,
        foreign_member=foreign_member,
    )


async def _query_ids(client: httpx.AsyncClient, principal: tuple, **params) -> set[str]:
    response = await client.get(
        "/api/v1/users/query",
        params={"page": 1, "page_size": 200, **params},
        headers=_headers(principal),
    )
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["items"]}


async def test_pa_default_active_visibility(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)
    ids = await _query_ids(httpx_client, project_admin, status="active")

    assert str(world.manager.id) in ids  # self
    assert str(world.member.id) in ids  # own project member
    # The fix (issue #115): enabled unassigned workers are visible.
    assert str(world.unassigned_annotator.id) in ids
    assert str(world.unassigned_reviewer.id) in ids
    # Enabled super admin is visible read-only (maintainer addition).
    assert str(world.admin.id) in ids
    # Foreign-project enabled annotator shares the same widened population.
    assert str(world.foreign_member.id) in ids
    # No expansion: own inactive member, unassigned viewer, other PA, disabled.
    assert str(world.inactive_member.id) not in ids
    assert str(world.viewer_member.id) in ids  # own member, any role
    assert str(world.unassigned_viewer.id) not in ids
    assert str(world.other_pa.id) not in ids
    assert str(world.inactive_annotator.id) not in ids
    assert str(world.inactive_super_admin.id) not in ids


async def test_pa_inactive_visibility_keeps_disabled_accounts_hidden(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)
    ids = await _query_ids(httpx_client, project_admin, status="inactive")

    assert str(world.inactive_member.id) in ids  # own project member
    assert str(world.inactive_annotator.id) not in ids  # unassigned disabled
    assert str(world.inactive_super_admin.id) not in ids  # disabled super admin
    assert str(world.unassigned_annotator.id) not in ids  # active account


async def test_pa_role_filters(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)

    super_admins = await _query_ids(
        httpx_client, project_admin, status="active", role="super_admin"
    )
    assert str(world.admin.id) in super_admins  # read-only lookup
    assert str(world.inactive_super_admin.id) not in super_admins

    viewers = await _query_ids(httpx_client, project_admin, status="all", role="viewer")
    assert str(world.viewer_member.id) in viewers  # own member only
    assert str(world.unassigned_viewer.id) not in viewers

    project_admins = await _query_ids(
        httpx_client, project_admin, status="active", role="project_admin"
    )
    assert str(world.manager.id) in project_admins  # self
    assert str(world.other_pa.id) not in project_admins


async def test_pa_project_filter_stays_project_bounded(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)

    own = await _query_ids(
        httpx_client,
        project_admin,
        status="all",
        project_id=str(world.own_project.id),
    )
    assert own == {
        str(world.member.id),
        str(world.inactive_member.id),
        str(world.viewer_member.id),
    }

    foreign = await _query_ids(
        httpx_client,
        project_admin,
        status="all",
        project_id=str(world.foreign_project.id),
    )
    assert foreign == set()  # no cross-project membership disclosure


async def test_pa_search_finds_unassigned_annotator(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """The exact issue #115 repro: search by the unassigned annotator's name."""

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    response = await httpx_client.get(
        "/api/v1/users/query",
        params={
            "page": 1,
            "page_size": 50,
            "status": "active",
            "search": "Free Annotator",
        },
        headers=_headers(project_admin),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(world.unassigned_annotator.id)


async def test_pa_query_stats_export_share_widened_scope(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    await _seed_scope_world(db_session, project_admin, super_admin)
    params = {"status": "active", "search": "Free"}

    query = await httpx_client.get(
        "/api/v1/users/query",
        params={**params, "page": 1, "page_size": 50},
        headers=_headers(project_admin),
    )
    stats = await httpx_client.get(
        "/api/v1/users/stats", params=params, headers=_headers(project_admin)
    )
    exported = await httpx_client.get(
        "/api/v1/users/export",
        params={**params, "format": "json"},
        headers=_headers(project_admin),
    )
    assert query.status_code == stats.status_code == exported.status_code == 200
    query_body = query.json()
    export_body = json.loads(exported.text)
    assert (
        query_body["total"]
        == stats.json()["total"]
        == export_body["_export_meta"]["count"]
        == 2
    )
    export_emails = {row["email"] for row in export_body["users"]}
    assert export_emails == {"free-annotator@e.test", "free-reviewer@e.test"}


async def test_pa_query_flags_is_managed(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)
    response = await httpx_client.get(
        "/api/v1/users/query",
        params={"page": 1, "page_size": 200, "status": "active"},
        headers=_headers(project_admin),
    )
    assert response.status_code == 200, response.text
    flags = {row["id"]: row["is_managed"] for row in response.json()["items"]}

    assert flags[str(world.manager.id)] is True  # self
    assert flags[str(world.member.id)] is True  # own project member
    # Enabled annotator/reviewer accounts are operable, incl. unassigned and
    # foreign-project members (maintainer revision).
    assert flags[str(world.unassigned_annotator.id)] is True
    assert flags[str(world.unassigned_reviewer.id)] is True
    assert flags[str(world.foreign_member.id)] is True
    assert flags[str(world.admin.id)] is False  # super admin: visible, read-only

    # Super-admin actor: everything is manageable.
    sa_response = await httpx_client.get(
        "/api/v1/users/query",
        params={"page": 1, "page_size": 200, "status": "all"},
        headers=_headers(super_admin),
    )
    assert sa_response.status_code == 200, sa_response.text
    assert all(row["is_managed"] for row in sa_response.json()["items"])


async def test_pa_account_writes_but_platform_role_change_is_super_admin_only(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Account-level writes succeed on unassigned enabled workers.

    Platform-role preview/change is super-administrator only (plan AUTH-04);
    the remaining account writes (password reset, group preview, lifecycle
    deactivate/delete) stay available to the project administrator.
    """

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    target = world.unassigned_annotator
    headers = _headers(project_admin)

    preview = await httpx_client.get(
        f"/api/v1/users/{target.id}/role/preview?role=employee", headers=headers
    )
    assert preview.status_code == 403, preview.text

    role = await httpx_client.patch(
        f"/api/v1/users/{target.id}/role",
        json={"role": "employee"},
        headers=headers,
    )
    assert role.status_code == 403, role.text

    reset = await httpx_client.post(
        f"/api/v1/users/{target.id}/admin-reset-password", headers=headers
    )
    assert reset.status_code == 200, reset.text

    group = await httpx_client.post(
        "/api/v1/users/groups/bulk/preview",
        json={"user_ids": [str(target.id)], "group_id": None},
        headers=headers,
    )
    assert group.status_code == 200, group.text
    item = group.json()["items"][0]
    assert item["ok"] is True, item
    assert item["email"] == target.email

    deactivate = await httpx_client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert deactivate.status_code == 200, deactivate.text
    assert deactivate.json()["is_active"] is False

    delete = await httpx_client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert delete.status_code == 200, delete.text


async def test_pa_offboarding_allowed_on_unassigned_worker(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Offboarding an unassigned worker has no handover map and succeeds."""

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    target = world.unassigned_reviewer
    headers = _headers(project_admin)

    preview = await httpx_client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["can_commit"] is True, body
    assert body["blockers"] == []

    commit = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        json={"preview_version": body["preview_version"], "reason": "验收离职"},
        headers=headers,
    )
    assert commit.status_code == 200, commit.text


async def test_pa_account_writes_on_foreign_member_but_lifecycle_gated(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Foreign-project annotator: account writes open, lifecycle writes gated."""

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    target = world.foreign_member
    headers = _headers(project_admin)

    role = await httpx_client.patch(
        f"/api/v1/users/{target.id}/role",
        json={"role": "employee"},
        headers=headers,
    )
    assert role.status_code == 403, role.text

    reset = await httpx_client.post(
        f"/api/v1/users/{target.id}/admin-reset-password", headers=headers
    )
    assert reset.status_code == 200, reset.text

    offboard = await httpx_client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
    )
    assert offboard.status_code == 403, offboard.text

    deactivate = await httpx_client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert deactivate.status_code == 403, deactivate.text

    delete = await httpx_client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert delete.status_code == 403, delete.text


async def test_pa_query_flags_is_lifecycle_managed(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Lifecycle capability is narrower than account manageability.

    Codex review (PR #119): a single ``is_managed`` flag made the users page
    render offboarding/delete buttons for cross-project rows whose lifecycle
    writes the API rejects with 403. ``is_lifecycle_managed`` stays true only
    for rows whose derived projects all sit inside the actor's own projects.
    """

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    response = await httpx_client.get(
        "/api/v1/users/query",
        params={"page": 1, "page_size": 200, "status": "active"},
        headers=_headers(project_admin),
    )
    assert response.status_code == 200, response.text
    flags = {
        row["id"]: (row["is_managed"], row["is_lifecycle_managed"])
        for row in response.json()["items"]
    }

    assert flags[str(world.manager.id)] == (True, True)  # self
    assert flags[str(world.member.id)] == (True, True)  # own project member
    # Unassigned enabled workers: no derived projects, lifecycle allowed.
    assert flags[str(world.unassigned_annotator.id)] == (True, True)
    # Foreign-project member: manageable, but lifecycle gated.
    assert flags[str(world.foreign_member.id)] == (True, False)
    # Super admin stays read-only on both axes.
    assert flags[str(world.admin.id)] == (False, False)

    # Super-admin actor: everything stays fully manageable.
    sa_response = await httpx_client.get(
        "/api/v1/users/query",
        params={"page": 1, "page_size": 200, "status": "all"},
        headers=_headers(super_admin),
    )
    assert sa_response.status_code == 200, sa_response.text
    assert all(row["is_lifecycle_managed"] for row in sa_response.json()["items"])


async def test_pa_lifecycle_blocked_for_task_straddling_annotator(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Membership-free annotator holding a foreign task is lifecycle-gated.

    Codex review (PR #119): ``remove_member`` does not clear task/batch
    assignments, so a membership-only guard let project admins deactivate or
    delete workers still holding work in other admins' projects.
    """

    from app.db.models.task import Task

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    straddler = await create_user(
        db_session, "employee", "straddler@e.test", "Task Straddler"
    )
    db_session.add(
        Task(
            project_id=world.foreign_project.id,
            display_id=f"T-{straddler.id.hex[:8]}",
            file_name="straddle.jpg",
            file_path="straddle.jpg",
            status="in_progress",
            assignee_id=straddler.id,
        )
    )
    await db_session.flush()

    headers = _headers(project_admin)

    # Account-level writes stay open on the enabled annotator.
    reset = await httpx_client.post(
        f"/api/v1/users/{straddler.id}/admin-reset-password", headers=headers
    )
    assert reset.status_code == 200, reset.text

    # Lifecycle writes must stay gated even without any ProjectMember row.
    deactivate = await httpx_client.post(
        f"/api/v1/users/{straddler.id}/deactivate", headers=headers
    )
    assert deactivate.status_code == 403, deactivate.text

    delete = await httpx_client.delete(f"/api/v1/users/{straddler.id}", headers=headers)
    assert delete.status_code == 403, delete.text


async def test_pa_delete_transfer_receiver_must_cover_task_projects(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Delete-transfer receivers stay project-bounded.

    Codex review (PR #119): the platform-wide manage arm accepted any enabled
    annotator as ``transfer_to_user_id``, so pending tasks could be reassigned
    to someone with no membership in the affected project.
    """

    from app.db.models.task import Task

    world = await _seed_scope_world(db_session, project_admin, super_admin)
    headers = _headers(project_admin)

    target = await create_user(
        db_session, "employee", "leaving-member@e.test", "Leaving Member"
    )
    receiver = await create_user(
        db_session, "employee", "covering-member@e.test", "Covering Member"
    )
    outsider = await create_user(
        db_session, "employee", "outside-receiver@e.test", "Outside Receiver"
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=world.own_project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=world.manager.id,
            ),
            ProjectMember(
                project_id=world.own_project.id,
                user_id=receiver.id,
                role="annotator",
                assigned_by=world.manager.id,
            ),
            Task(
                project_id=world.own_project.id,
                display_id=f"T-{target.id.hex[:8]}",
                file_name="handover.jpg",
                file_path="handover.jpg",
                status="in_progress",
                assignee_id=target.id,
            ),
        ]
    )
    await db_session.flush()

    # The enabled outsider would pass the account-level manage arm but has no
    # membership in the project holding the pending task.
    blocked = await httpx_client.request(
        "DELETE",
        f"/api/v1/users/{target.id}",
        json={"transfer_to_user_id": str(outsider.id)},
        headers=headers,
    )
    assert blocked.status_code == 403, blocked.text

    # A member of the affected project is a valid receiver.
    ok = await httpx_client.request(
        "DELETE",
        f"/api/v1/users/{target.id}",
        json={"transfer_to_user_id": str(receiver.id)},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text


async def test_pa_offboarding_blocked_on_disabled_unassigned_account(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    """Offboarding keeps its empty-project exception limited to active targets.

    Codex review (PR #119): preview/commit accept suspended accounts, so a
    project admin who knew a suspended unassigned account's id could offboard
    it although disabled accounts stay outside the widened manage scope.
    """

    suspended = await create_user(
        db_session, "annotator", "suspended-free@e.test", "Suspended Free"
    )
    suspended.is_active = False
    suspended.disabled_kind = "suspended"
    await db_session.flush()

    headers = _headers(project_admin)
    preview = await httpx_client.get(
        f"/api/v1/users/{suspended.id}/offboarding-preview", headers=headers
    )
    assert preview.status_code == 403, preview.text

    commit = await httpx_client.post(
        f"/api/v1/users/{suspended.id}/offboarding",
        json={"preview_version": "0", "reason": "越权尝试"},
        headers=headers,
    )
    assert commit.status_code == 403, commit.text

    # Super admins keep full offboarding access to the same account.
    sa_preview = await httpx_client.get(
        f"/api/v1/users/{suspended.id}/offboarding-preview",
        headers=_headers(super_admin),
    )
    assert sa_preview.status_code == 200, sa_preview.text


async def test_pa_writes_blocked_on_super_admin(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)
    target = world.admin
    headers = _headers(project_admin)

    role = await httpx_client.patch(
        f"/api/v1/users/{target.id}/role",
        json={"role": "employee"},
        headers=headers,
    )
    assert role.status_code == 403, role.text

    reset = await httpx_client.post(
        f"/api/v1/users/{target.id}/admin-reset-password", headers=headers
    )
    assert reset.status_code == 403, reset.text

    deactivate = await httpx_client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert deactivate.status_code == 403, deactivate.text

    delete = await httpx_client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert delete.status_code == 403, delete.text

    # Platform-role preview is super-admin only, so an out-of-scope project
    # administrator is denied before any user detail is disclosed.
    preview = await httpx_client.get(
        f"/api/v1/users/{target.id}/role/preview?role=employee", headers=headers
    )
    assert preview.status_code == 403, preview.text
    assert target.email not in preview.text and target.name not in preview.text


async def test_legacy_role_filters_are_rejected(
    httpx_client: httpx.AsyncClient, super_admin
):
    """role=annotator/reviewer must be a validation error, not an empty list."""

    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    for url in ("/api/v1/users", "/api/v1/users/query", "/api/v1/users/stats"):
        response = await httpx_client.get(
            url, params={"role": "reviewer"}, headers=headers
        )
        assert response.status_code == 400, (url, response.text)
        assert "非法平台角色" in response.text


async def test_super_admin_visibility_unchanged(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)
    ids = await _query_ids(httpx_client, super_admin, status="all")

    for user in (
        world.manager,
        world.member,
        world.unassigned_annotator,
        world.unassigned_viewer,
        world.other_pa,
        world.inactive_annotator,
        world.inactive_super_admin,
        world.foreign_member,
    ):
        assert str(user.id) in ids


async def test_legacy_picker_semantics_unchanged(
    httpx_client: httpx.AsyncClient, project_admin, super_admin, db_session
):
    world = await _seed_scope_world(db_session, project_admin, super_admin)

    # role=employee&status=active keeps the wide candidate list (assign modal).
    candidates = await httpx_client.get(
        "/api/v1/users",
        params={"role": "employee", "status": "active"},
        headers=_headers(project_admin),
    )
    assert candidates.status_code == 200, candidates.text
    candidate_ids = {row["id"] for row in candidates.json()}
    assert str(world.unassigned_annotator.id) in candidate_ids
    assert str(world.foreign_member.id) in candidate_ids

    # Without a role the picker stays strict (transfer receivers, pickers).
    strict = await httpx_client.get(
        "/api/v1/users", params={"status": "active"}, headers=_headers(project_admin)
    )
    assert strict.status_code == 200, strict.text
    strict_ids = {row["id"] for row in strict.json()}
    assert str(world.manager.id) in strict_ids
    assert str(world.member.id) in strict_ids
    assert str(world.unassigned_annotator.id) not in strict_ids
    assert str(world.admin.id) not in strict_ids
