"""Focused tests for the demo seed's explicit project-duty reconciliation.

``scripts.seed._ensure_demo_memberships`` is the seam that turns the seeded
personas into reachable accounts: platform identity alone never authorizes
project access, so every fixture project must receive the documented member
rows.  These tests exercise every seeded display id without any media fixture
and use unique persona emails because ``users.email`` is globally unique.
"""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from scripts.seed import (
    DEMO_PROJECT_DISPLAY_IDS,
    DEMO_PROJECT_MEMBERS,
    _ensure_demo_memberships,
)
from tests.factory import build_tool_bindings

pytestmark = pytest.mark.asyncio

TEST_MEMBERS = (
    ("seed-anno", "annotator"),
    ("seed-anno2", "annotator"),
    ("seed-anno3", "annotator"),
    ("seed-qa", "reviewer"),
    ("seed-viewer", "viewer"),
)
PLATFORM_ROLES = {
    "seed-admin": "super_admin",
    "seed-pm": "project_admin",
    "seed-anno": "employee",
    "seed-anno2": "employee",
    "seed-anno3": "employee",
    "seed-qa": "employee",
    "seed-viewer": "viewer",
}


async def _seed_personas(db):
    users = {}
    for email, role in PLATFORM_ROLES.items():
        user = User(
            id=uuid.uuid4(),
            email=email,
            name=email,
            password_hash="test",
            role=role,
            is_active=True,
        )
        db.add(user)
        users[email] = user
    await db.flush()
    return users


async def _seed_demo_projects(db, owner_id):
    """Create one project per seeded display id plus an unrelated decoy."""

    display_ids = tuple(f"P-SEED-{i}" for i in range(len(DEMO_PROJECT_DISPLAY_IDS)))
    projects = {}
    for display_id in display_ids:
        project = Project(
            id=uuid.uuid4(),
            display_id=display_id,
            name=f"demo {display_id}",
            type_label="图像目标检测",
            type_key="image-det",
            owner_id=owner_id,
            tool_bindings=build_tool_bindings(["car"]),
            ai_enabled=False,
        )
        db.add(project)
        projects[display_id] = project
    decoy = Project(
        id=uuid.uuid4(),
        display_id="P-NOT-A-DEMO-FIXTURE",
        name="unrelated project",
        type_label="图像目标检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
        ai_enabled=False,
    )
    db.add(decoy)
    await db.flush()
    return projects, decoy


async def _members_in(db, projects):
    rows = list((await db.execute(select(ProjectMember))).scalars())
    wanted = {str(p.id) for p in projects}
    return [row for row in rows if str(row.project_id) in wanted]


async def test_demo_membership_contract_covers_every_seeded_project():
    """Shape test for the production constants the demo seed ships with."""

    assert set(DEMO_PROJECT_DISPLAY_IDS) == {
        "P-COCO8",
        "P-VIDEO-DEV",
        "P-PC-DEV",
        "P-PC-MULTI",
        "P-OCR",
    }
    assert dict(DEMO_PROJECT_MEMBERS) == {
        "anno": "annotator",
        "anno2": "annotator",
        "anno3": "annotator",
        "qa": "reviewer",
        "viewer": "viewer",
    }
    # Managers never appear as members: they act through ownership / super admin.
    assert not {"pm", "admin"} & {email for email, _ in DEMO_PROJECT_MEMBERS}


async def test_every_seeded_project_gets_its_documented_duties(db_session):
    users = await _seed_personas(db_session)
    projects, decoy = await _seed_demo_projects(db_session, users["seed-pm"].id)

    changed = await _ensure_demo_memberships(
        db_session,
        members=TEST_MEMBERS,
        display_ids=tuple(projects),
        assigner_emails=("seed-pm", "seed-admin"),
    )
    await db_session.flush()

    assert changed == len(projects) * len(TEST_MEMBERS)
    members = await _members_in(db_session, [*projects.values(), decoy])
    by_project: dict[str, list[ProjectMember]] = {}
    for member in members:
        by_project.setdefault(str(member.project_id), []).append(member)

    # Every demo fixture project is reachable exactly as documented, and the
    # unrelated project gets nothing.
    assert set(by_project) == {str(p.id) for p in projects.values()}
    expected = {users[email].id: duty for email, duty in TEST_MEMBERS}
    for display_id, project in projects.items():
        assert {m.user_id: m.role for m in by_project[str(project.id)]} == expected, (
            display_id
        )
    assert not by_project.get(str(decoy.id))

    # Managers act through ownership / super admin, never through a member row.
    manager_ids = {users["seed-pm"].id, users["seed-admin"].id}
    assert all(m.user_id not in manager_ids for m in members)


async def test_reconciliation_is_idempotent_and_repairs_role_drift(db_session):
    users = await _seed_personas(db_session)
    projects, _decoy = await _seed_demo_projects(db_session, users["seed-pm"].id)
    target = next(iter(projects.values()))

    await _ensure_demo_memberships(
        db_session,
        members=TEST_MEMBERS,
        display_ids=tuple(projects),
        assigner_emails=("seed-pm", "seed-admin"),
    )
    await db_session.flush()
    assert (
        await _ensure_demo_memberships(
            db_session,
            members=TEST_MEMBERS,
            display_ids=tuple(projects),
            assigner_emails=("seed-pm", "seed-admin"),
        )
        == 0
    )

    # A drifted duty is repaired instead of duplicated.
    drifted = next(
        member
        for member in await _members_in(db_session, [target])
        if member.user_id == users["seed-anno"].id
    )
    drifted.role = "viewer"
    await db_session.flush()

    assert (
        await _ensure_demo_memberships(
            db_session,
            members=TEST_MEMBERS,
            display_ids=tuple(projects),
            assigner_emails=("seed-pm", "seed-admin"),
        )
        == 1
    )
    repaired = [
        member
        for member in await _members_in(db_session, [target])
        if member.user_id == users["seed-anno"].id
    ]
    assert len(repaired) == 1
    assert repaired[0].role == "annotator"


async def test_missing_persona_user_is_skipped(db_session):
    users = await _seed_personas(db_session)
    projects, _decoy = await _seed_demo_projects(db_session, users["seed-pm"].id)
    await db_session.delete(users["seed-anno3"])
    await db_session.flush()

    changed = await _ensure_demo_memberships(
        db_session,
        members=TEST_MEMBERS,
        display_ids=tuple(projects),
        assigner_emails=("seed-pm", "seed-admin"),
    )
    await db_session.flush()

    assert changed == len(projects) * (len(TEST_MEMBERS) - 1)
    members = await _members_in(db_session, projects.values())
    assert all(member.user_id != users["seed-anno3"].id for member in members)
    assert len(members) == len(projects) * (len(TEST_MEMBERS) - 1)
