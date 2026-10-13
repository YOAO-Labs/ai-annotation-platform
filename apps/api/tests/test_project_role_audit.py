"""Behavioral audit tests for Increment A project-role preparation.

These tests run against a real disposable PostgreSQL schema.  Seeded rows are
committed so the audit can run in the PostgreSQL-enforced ``REPEATABLE READ,
READ ONLY`` transaction used by the CLI; every test cleans up in ``finally``.
Assertions are ID-scoped so pre-existing committed rows do not matter.

Alembic migration steps are synchronous (``asyncio.run`` per isolated step),
matching ``test_migration_0173_project_role_preparation.py``; calling Alembic
from inside a running event loop would nest ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.project_member import ProjectMember
from app.db.models.task_batch import TaskBatch
from app.db.models.user_invitation import UserInvitation
from scripts.audit_project_roles import (
    AUDIT_REPORT_VERSION,
    _json_default,
    active_manager_predicate,
    collect_report,
    read_only_audit_session,
)
from tests.factory import create_membership, create_project, create_task, create_user

_API_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_0173_PATH = (
    _API_ROOT / "alembic" / "versions" / "0173_project_role_preparation.py"
)

_SENSITIVE_KEYS = {"password_hash", "token", "email", "invite_url", "signed_url"}
# Unique text embedded in seeded emails/tokens/names: none of it may appear in
# the serialized report, unlike the report's own run id.
_EMAIL_SENTINEL = "email-sentinel-probe"
_TOKEN_SENTINEL = "token-sentinel-probe"
_NAME_SENTINEL = "NameSentinelProbe"


def _load_migration_0173():
    spec = importlib.util.spec_from_file_location(
        "migration_0173", _MIGRATION_0173_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _replay_migration(sync_conn, migration, direction: str) -> None:
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, direction)()


def _set_alembic_version(sync_conn, version: str) -> None:
    sync_conn.execute(
        text("UPDATE alembic_version SET version_num = :v"), {"v": version}
    )


def _has_preparation_column(sync_conn) -> bool:
    return bool(
        sync_conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'project_members' AND column_name = 'version'"
            )
        ).scalar()
    )


def _item_ids(section: dict) -> set[str]:
    return {
        str(
            item.get("id")
            or item.get("task_id")
            or item.get("project_id")
            or item.get("user_id")
        )
        for item in section["items"]
    }


def _item_user_ids(section: dict) -> set[str]:
    return {str(item["user_id"]) for item in section["items"]}


async def _readonly_report(engine, **kwargs) -> dict:
    async with read_only_audit_session(engine) as db:
        report = await collect_report(db, **kwargs)
        # Prove the transaction is genuinely read-only: a write must fail.
        raised = None
        try:
            await db.execute(
                text(
                    "INSERT INTO project_members "
                    "(id, project_id, user_id, role, assigned_by) "
                    "VALUES (:id, :project_id, :user_id, 'viewer', :assigned_by)"
                ),
                {
                    "id": uuid.uuid4(),
                    "project_id": uuid.uuid4(),
                    "user_id": uuid.uuid4(),
                    "assigned_by": uuid.uuid4(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - prove READ ONLY enforcement
            raised = exc
        assert raised is not None, "write unexpectedly succeeded in read-only audit"
        assert "read-only" in str(raised).lower(), f"unexpected write error: {raised}"
        return report


async def _seed_full(engine) -> dict:
    users: list[uuid.UUID] = []
    projects: list[uuid.UUID] = []
    ids: dict = {}

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:

        async def make_user(role: str, tag: str):
            user = await create_user(
                db,
                role,
                f"{_EMAIL_SENTINEL}-{tag}-{uuid.uuid4()}@test.local",
                f"{_NAME_SENTINEL}-{tag}",
            )
            users.append(user.id)
            return user

        owner = await make_user("project_admin", "owner")
        project = await create_project(db, owner_id=owner.id)
        other_project = await create_project(db, owner_id=owner.id)
        projects.extend([project.id, other_project.id])

        unknown_user = await make_user("mystery_role", "unknown")
        unknown_member = ProjectMember(
            project_id=project.id,
            user_id=unknown_user.id,
            role="mystery_member",
            assigned_by=owner.id,
        )

        # Legacy mismatch: global annotator with a reviewer membership.
        legacy_user = await make_user("annotator", "legacy")
        legacy_member = ProjectMember(
            project_id=project.id,
            user_id=legacy_user.id,
            role="reviewer",
            assigned_by=owner.id,
        )

        # Post-conversion diversity: employee annotating in A and reviewing in B.
        employee_user = await make_user("employee", "employee")
        employee_member_a = ProjectMember(
            project_id=project.id,
            user_id=employee_user.id,
            role="annotator",
            assigned_by=owner.id,
        )
        employee_member_b = ProjectMember(
            project_id=other_project.id,
            user_id=employee_user.id,
            role="reviewer",
            assigned_by=owner.id,
        )

        viewer = await make_user("viewer", "viewer")
        viewer_member = ProjectMember(
            project_id=project.id,
            user_id=viewer.id,
            role="annotator",
            assigned_by=owner.id,
        )

        admin_member = ProjectMember(
            project_id=project.id,
            user_id=owner.id,
            role="viewer",
            assigned_by=owner.id,
        )

        bad_owner = await make_user("annotator", "badowner")
        bad_project = await create_project(db, owner_id=bad_owner.id)
        projects.append(bad_project.id)

        inactive_owner = await make_user("project_admin", "inactiveowner")
        inactive_owner.is_active = False
        inactive_project = await create_project(db, owner_id=inactive_owner.id)
        projects.append(inactive_project.id)

        # Assignment gaps: explicit, inherited batch and overridden batch.
        assignee = await make_user("annotator", "assignee")
        task_missing = await create_task(db, project_id=project.id)
        task_missing.assignee_id = assignee.id

        reviewer = await make_user("reviewer", "reviewer")
        task_missing_reviewer = await create_task(db, project_id=project.id)
        task_missing_reviewer.reviewer_id = reviewer.id

        # Inherited: no task assignee, the batch default is a non-member.
        batch_annotator = await make_user("annotator", "batchnonmember")
        batch_inherited = TaskBatch(
            project_id=project.id,
            display_id=f"B-AUDIT-{uuid.uuid4().hex[:8]}",
            name="Inherited",
            annotator_id=batch_annotator.id,
        )
        db.add(batch_inherited)
        await db.flush()
        task_inherited = await create_task(db, project_id=project.id)
        task_inherited.batch_id = batch_inherited.id

        # Override: the batch default is a valid annotator member, but the task
        # explicitly points at a non-member.  Effective assignment must be the
        # task value, so this is flagged.
        batch_override = TaskBatch(
            project_id=project.id,
            display_id=f"B-AUDIT-{uuid.uuid4().hex[:8]}",
            name="Override",
            annotator_id=employee_user.id,
        )
        db.add(batch_override)
        await db.flush()
        task_override = await create_task(db, project_id=project.id)
        task_override.batch_id = batch_override.id
        task_override.assignee_id = assignee.id

        task_wrong_role = await create_task(db, project_id=project.id)
        task_wrong_role.assignee_id = legacy_user.id

        inactive_assignee = await make_user("annotator", "inactive")
        inactive_assignee.is_active = False
        inactive_assignee.disabled_kind = "suspended"
        task_inactive = await create_task(
            db, project_id=project.id, status="in_progress"
        )
        task_inactive.assignee_id = inactive_assignee.id

        db.add_all(
            [
                unknown_member,
                legacy_member,
                employee_member_a,
                employee_member_b,
                viewer_member,
                admin_member,
                batch_inherited,
                batch_override,
                task_missing,
                task_missing_reviewer,
                task_inherited,
                task_override,
                task_wrong_role,
                task_inactive,
            ]
        )

        future = datetime.now(timezone.utc) + timedelta(days=3)
        deleted_invitation = UserInvitation(
            email=f"{_EMAIL_SENTINEL}-deleted-{uuid.uuid4()}@test.local",
            role="reviewer",
            project_id=uuid.uuid4(),
            project_role="reviewer",
            token=f"{_TOKEN_SENTINEL}-{uuid.uuid4().hex}",
            expires_at=future,
            invited_by=owner.id,
        )
        legacy_project_invitation = UserInvitation(
            email=f"{_EMAIL_SENTINEL}-legacyinv-{uuid.uuid4()}@test.local",
            role="annotator",
            project_id=project.id,
            project_role=None,
            token=f"{_TOKEN_SENTINEL}-{uuid.uuid4().hex}",
            expires_at=future,
            invited_by=owner.id,
        )
        account_invitation = UserInvitation(
            email=f"{_EMAIL_SENTINEL}-account-{uuid.uuid4()}@test.local",
            role="viewer",
            project_id=None,
            project_role="annotator",
            token=f"{_TOKEN_SENTINEL}-{uuid.uuid4().hex}",
            expires_at=future,
            invited_by=owner.id,
        )
        db.add_all([deleted_invitation, legacy_project_invitation, account_invitation])

        # Review evidence cases.
        task_complete = await create_task(db, project_id=project.id, status="completed")
        task_complete.review_round_id = uuid.uuid4()
        task_complete.review_submitter_id = assignee.id
        task_complete.review_contributor_ids = [str(assignee.id)]
        task_complete.annotation_contributor_ids = [str(assignee.id)]

        task_missing_submitter = await create_task(
            db, project_id=project.id, status="review"
        )
        task_missing_submitter.review_round_id = uuid.uuid4()
        task_missing_submitter.review_submitter_id = None
        task_missing_submitter.review_contributor_ids = [str(assignee.id)]
        task_missing_submitter.annotation_contributor_ids = [str(assignee.id)]

        task_malformed_object = await create_task(
            db, project_id=project.id, status="review"
        )
        task_malformed_object.review_round_id = uuid.uuid4()
        task_malformed_object.review_submitter_id = assignee.id
        task_malformed_object.review_contributor_ids = {"not": "an-array"}
        task_malformed_object.annotation_contributor_ids = [str(assignee.id)]

        # Numeric and non-UUID string elements are invalid, not complete.
        task_invalid_number = await create_task(
            db, project_id=project.id, status="review"
        )
        task_invalid_number.review_round_id = uuid.uuid4()
        task_invalid_number.review_submitter_id = assignee.id
        task_invalid_number.review_contributor_ids = [123]
        task_invalid_number.annotation_contributor_ids = [str(assignee.id)]

        task_invalid_string = await create_task(
            db, project_id=project.id, status="review"
        )
        task_invalid_string.review_round_id = uuid.uuid4()
        task_invalid_string.review_submitter_id = assignee.id
        task_invalid_string.review_contributor_ids = ["not-a-uuid"]
        task_invalid_string.annotation_contributor_ids = [str(assignee.id)]

        task_unknown_accumulator = await create_task(
            db, project_id=project.id, status="review"
        )
        task_unknown_accumulator.review_round_id = uuid.uuid4()
        task_unknown_accumulator.review_submitter_id = assignee.id
        task_unknown_accumulator.review_contributor_ids = [str(assignee.id)]
        task_unknown_accumulator.annotation_contributor_ids = None

        # Valid arrays, but the frozen set does not contain the submitter.
        task_missing_contributors = await create_task(
            db, project_id=project.id, status="review"
        )
        task_missing_contributors.review_round_id = uuid.uuid4()
        task_missing_contributors.review_submitter_id = assignee.id
        task_missing_contributors.review_contributor_ids = [str(reviewer.id)]
        task_missing_contributors.annotation_contributor_ids = [str(assignee.id)]

        task_missing_round = await create_task(
            db, project_id=project.id, status="review"
        )
        task_missing_round.review_round_id = None

        db.add_all(
            [
                task_complete,
                task_missing_submitter,
                task_malformed_object,
                task_invalid_number,
                task_invalid_string,
                task_unknown_accumulator,
                task_missing_contributors,
                task_missing_round,
            ]
        )
        await db.flush()
        ids = {
            "project": project.id,
            "other_project": other_project.id,
            "unknown_user": unknown_user.id,
            "unknown_member": unknown_member.id,
            "legacy_user": legacy_user.id,
            "legacy_member": legacy_member.id,
            "employee_user": employee_user.id,
            "viewer_member": viewer_member.id,
            "admin_member": admin_member.id,
            "bad_project": bad_project.id,
            "inactive_project": inactive_project.id,
            "task_missing": task_missing.id,
            "task_missing_reviewer": task_missing_reviewer.id,
            "task_inherited": task_inherited.id,
            "task_override": task_override.id,
            "task_wrong_role": task_wrong_role.id,
            "task_inactive": task_inactive.id,
            "inactive_assignee": inactive_assignee.id,
            "deleted_invitation": deleted_invitation.id,
            "legacy_project_invitation": legacy_project_invitation.id,
            "account_invitation": account_invitation.id,
            "task_complete": task_complete.id,
            "task_missing_submitter": task_missing_submitter.id,
            "task_malformed_object": task_malformed_object.id,
            "task_invalid_number": task_invalid_number.id,
            "task_invalid_string": task_invalid_string.id,
            "task_unknown_accumulator": task_unknown_accumulator.id,
            "task_missing_contributors": task_missing_contributors.id,
            "task_missing_round": task_missing_round.id,
        }
        await db.commit()

    return {"ids": ids, "users": users, "projects": projects}


async def _seed_manager_cases(engine) -> dict:
    """Seed active-manager and adversarial effective assignments.

    Mirrors ``project_access.is_privileged_for_project``: an active super
    administrator manages every project and an active ``project_admin`` owns
    exactly the project it owns.  Each manager task has no membership row, so
    the pre-fix audit reports it; the adversarial rows prove the exemption is
    not broader than the live authorization rule.
    """
    users: list[uuid.UUID] = []
    projects: list[uuid.UUID] = []
    ids: dict = {}

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:

        async def make_user(role: str, tag: str):
            user = await create_user(
                db,
                role,
                f"{_EMAIL_SENTINEL}-{tag}-{uuid.uuid4()}@test.local",
                f"{_NAME_SENTINEL}-{tag}",
            )
            users.append(user.id)
            return user

        super_admin = await make_user("super_admin", "mgr-super")
        super_admin_member = await make_user("super_admin", "mgr-super-member")
        admin_owner = await make_user("project_admin", "mgr-admin-owner")
        foreign_admin = await make_user("project_admin", "mgr-foreign-admin")
        inactive_admin = await make_user("project_admin", "mgr-inactive-admin")
        inactive_admin.is_active = False
        inactive_super = await make_user("super_admin", "mgr-inactive-super")
        inactive_super.is_active = False
        employee = await make_user("employee", "mgr-employee")
        employee_owner = await make_user("employee", "mgr-employee-owner")
        wrong_employee = await make_user("employee", "mgr-wrong-employee")

        work_project = await create_project(db, owner_id=admin_owner.id)
        foreign_project = await create_project(db, owner_id=foreign_admin.id)
        employee_project = await create_project(db, owner_id=employee_owner.id)
        inactive_admin_project = await create_project(db, owner_id=inactive_admin.id)
        projects.extend(
            [
                work_project.id,
                foreign_project.id,
                employee_project.id,
                inactive_admin_project.id,
            ]
        )

        # A manager may still carry a member row: the assignment must not be
        # re-reported as a wrong-role gap.  An ordinary employee with a
        # mismatched membership stays a finding.
        super_admin_membership = ProjectMember(
            project_id=work_project.id,
            user_id=super_admin_member.id,
            role="viewer",
            assigned_by=admin_owner.id,
        )
        wrong_employee_membership = ProjectMember(
            project_id=work_project.id,
            user_id=wrong_employee.id,
            role="reviewer",
            assigned_by=admin_owner.id,
        )
        db.add_all([super_admin_membership, wrong_employee_membership])
        await db.flush()

        # Positive: explicit and inherited manager assignments resolve without a
        # membership, including a manager whose member row has another role.
        task_super_reviewer = await create_task(db, project_id=work_project.id)
        task_super_reviewer.reviewer_id = super_admin.id

        task_admin_owner_assignee = await create_task(db, project_id=work_project.id)
        task_admin_owner_assignee.assignee_id = admin_owner.id

        task_manager_wrong_role = await create_task(db, project_id=work_project.id)
        task_manager_wrong_role.reviewer_id = super_admin_member.id

        batch_super_reviewer = TaskBatch(
            project_id=work_project.id,
            display_id=f"B-AUDIT-{uuid.uuid4().hex[:8]}",
            name="Inherited super reviewer",
            reviewer_id=super_admin.id,
        )
        db.add(batch_super_reviewer)
        await db.flush()
        task_inherited_super_reviewer = await create_task(
            db, project_id=work_project.id
        )
        task_inherited_super_reviewer.batch_id = batch_super_reviewer.id

        batch_admin_assignee = TaskBatch(
            project_id=work_project.id,
            display_id=f"B-AUDIT-{uuid.uuid4().hex[:8]}",
            name="Inherited admin owner",
            annotator_id=admin_owner.id,
        )
        db.add(batch_admin_assignee)
        await db.flush()
        task_inherited_admin_assignee = await create_task(
            db, project_id=work_project.id
        )
        task_inherited_admin_assignee.batch_id = batch_admin_assignee.id

        # The explicit task value wins: a non-member batch default must not mask
        # the manager override.
        batch_employee_default = TaskBatch(
            project_id=work_project.id,
            display_id=f"B-AUDIT-{uuid.uuid4().hex[:8]}",
            name="Non-member default",
            annotator_id=employee.id,
        )
        db.add(batch_employee_default)
        await db.flush()
        task_override_manager = await create_task(db, project_id=work_project.id)
        task_override_manager.batch_id = batch_employee_default.id
        task_override_manager.assignee_id = super_admin.id

        # Adversarial non-managers: a foreign-project admin, an ordinary
        # employee (explicit and inherited) and a non-administrative owner all
        # keep their findings.  The inactive administrator owns the project it
        # is assigned in and the inactive super administrator would manage any
        # project, so those two only stay findings when the active-account
        # check is present.
        task_foreign_admin = await create_task(db, project_id=work_project.id)
        task_foreign_admin.assignee_id = foreign_admin.id

        task_employee = await create_task(db, project_id=work_project.id)
        task_employee.reviewer_id = employee.id

        task_inherited_employee = await create_task(db, project_id=work_project.id)
        task_inherited_employee.batch_id = batch_employee_default.id

        task_employee_owner = await create_task(db, project_id=employee_project.id)
        task_employee_owner.assignee_id = employee_owner.id

        task_inactive_admin = await create_task(
            db, project_id=inactive_admin_project.id
        )
        task_inactive_admin.assignee_id = inactive_admin.id

        task_inactive_super = await create_task(db, project_id=work_project.id)
        task_inactive_super.reviewer_id = inactive_super.id

        task_wrong_employee = await create_task(db, project_id=work_project.id)
        task_wrong_employee.assignee_id = wrong_employee.id

        db.add_all(
            [
                task_super_reviewer,
                task_admin_owner_assignee,
                task_manager_wrong_role,
                task_inherited_super_reviewer,
                task_inherited_admin_assignee,
                task_override_manager,
                task_foreign_admin,
                task_employee,
                task_inherited_employee,
                task_employee_owner,
                task_inactive_admin,
                task_inactive_super,
                task_wrong_employee,
            ]
        )
        await db.flush()
        ids = {
            "work_project": work_project.id,
            "super_admin": super_admin.id,
            "super_admin_member": super_admin_member.id,
            "admin_owner": admin_owner.id,
            "foreign_admin": foreign_admin.id,
            "inactive_admin": inactive_admin.id,
            "inactive_super": inactive_super.id,
            "employee": employee.id,
            "employee_owner": employee_owner.id,
            "wrong_employee": wrong_employee.id,
            "super_admin_membership": super_admin_membership.id,
            "wrong_employee_membership": wrong_employee_membership.id,
            "task_super_reviewer": task_super_reviewer.id,
            "task_admin_owner_assignee": task_admin_owner_assignee.id,
            "task_manager_wrong_role": task_manager_wrong_role.id,
            "task_inherited_super_reviewer": task_inherited_super_reviewer.id,
            "task_inherited_admin_assignee": task_inherited_admin_assignee.id,
            "task_override_manager": task_override_manager.id,
            "task_foreign_admin": task_foreign_admin.id,
            "task_employee": task_employee.id,
            "task_inherited_employee": task_inherited_employee.id,
            "task_employee_owner": task_employee_owner.id,
            "task_inactive_admin": task_inactive_admin.id,
            "task_inactive_super": task_inactive_super.id,
            "task_wrong_employee": task_wrong_employee.id,
        }
        await db.commit()

    return {"ids": ids, "users": users, "projects": projects}


async def _cleanup(engine, ctx: dict) -> None:
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        projects = ctx.get("projects") or []
        users = ctx.get("users") or []
        if projects:
            await db.execute(
                text("DELETE FROM tasks WHERE project_id = ANY(:p)"),
                {"p": projects},
            )
            await db.execute(
                text("DELETE FROM task_batches WHERE project_id = ANY(:p)"),
                {"p": projects},
            )
            await db.execute(
                text("DELETE FROM project_members WHERE project_id = ANY(:p)"),
                {"p": projects},
            )
            await db.execute(
                text("DELETE FROM projects WHERE id = ANY(:p)"), {"p": projects}
            )
        if users:
            await db.execute(
                text("DELETE FROM user_invitations WHERE invited_by = ANY(:u)"),
                {"u": users},
            )
            await db.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": users})
        await db.commit()


async def test_audit_flags_expected_discrepancies(
    test_engine, apply_migrations
) -> None:
    ctx = await _seed_full(test_engine)
    ids = ctx["ids"]
    try:
        report = await _readonly_report(
            test_engine, run_id="audit-test", generated_at=datetime.now(timezone.utc)
        )
    finally:
        await _cleanup(test_engine, ctx)

    assert report["report_version"] == AUDIT_REPORT_VERSION
    assert report["read_only"] is True
    assert report["baseline"]["schema_in_sync"] is True
    assert all(report["baseline"]["schema_capabilities"].values())

    roles = report["findings"]["role_discrepancies"]
    assert str(ids["unknown_user"]) in _item_ids(roles["unknown_platform_roles"])
    assert str(ids["unknown_member"]) in _item_ids(roles["unknown_member_roles"])
    assert str(ids["legacy_member"]) in _item_ids(
        roles["legacy_global_member_mismatches"]
    )
    # The employee with annotator/reviewer memberships is diversity, not a
    # legacy global/member mismatch.
    assert str(ids["employee_user"]) in _item_user_ids(
        roles["cross_project_role_diversity"]
    )
    assert str(ids["employee_user"]) not in _item_user_ids(
        roles["legacy_global_member_mismatches"]
    )
    assert str(ids["viewer_member"]) in _item_ids(roles["viewer_membership_conflicts"])
    assert str(ids["admin_member"]) in _item_ids(roles["administrator_memberships"])
    assert str(ids["bad_project"]) in _item_ids(roles["invalid_administrative_owners"])
    assert str(ids["inactive_project"]) in _item_ids(
        roles["inactive_administrative_owners"]
    )

    gaps = report["findings"]["assignment_membership_gaps"]
    assert str(ids["task_missing"]) in _item_ids(gaps["annotator_missing_membership"])
    # Inherited batch default and explicit task override both resolve through
    # the effective-assignment CTE.
    assert str(ids["task_inherited"]) in _item_ids(gaps["annotator_missing_membership"])
    assert str(ids["task_override"]) in _item_ids(gaps["annotator_missing_membership"])
    assert str(ids["task_missing_reviewer"]) in _item_ids(
        gaps["reviewer_missing_membership"]
    )
    assert str(ids["task_wrong_role"]) in _item_ids(
        gaps["annotator_wrong_membership_role"]
    )
    assert str(ids["task_inactive"]) in _item_ids(
        gaps["assignment_to_inactive_account"]
    )
    assert str(ids["inactive_assignee"]) in _item_ids(
        gaps["inactive_accounts_with_unfinished_work"]
    )

    invitations = report["findings"]["invitations"]
    assert str(ids["deleted_invitation"]) in _item_ids(
        invitations["pending_deleted_project_target"]
    )
    assert str(ids["legacy_project_invitation"]) in {
        str(item["id"])
        for item in invitations["pending"]["items"]
        if item["project_role"] is None
    }
    assert invitations["pending_project_role_to_backfill"]["total"] >= 1
    assert str(ids["account_invitation"]) in _item_ids(
        invitations["account_invitation_with_project_role"]
    )

    evidence = report["findings"]["review_evidence"]
    assert str(ids["task_complete"]) in _item_ids(evidence["complete"])
    for key in (
        "task_missing_submitter",
        "task_malformed_object",
        "task_invalid_number",
        "task_invalid_string",
        "task_unknown_accumulator",
        "task_missing_contributors",
        "task_missing_round",
    ):
        assert str(ids[key]) in _item_ids(evidence["incomplete"]), key
    reasons = evidence["incomplete_reasons"]
    assert reasons["missing_submitter"]["total"] >= 1
    assert reasons["malformed_review_array"]["total"] >= 3
    assert reasons["unknown_accumulator"]["total"] >= 1
    assert reasons["missing_contributors"]["total"] >= 1
    assert reasons["missing_round"]["total"] >= 1

    reconciliation = report["reconciliation"]
    assert reconciliation["projects"]["total"] >= 2
    assert reconciliation["memberships"]["total"] >= 4
    assert reconciliation["tasks"]["total"] >= 6

    # Privacy: seeded email/token/name sentinels and the DSN must not appear,
    # and the malformed payloads must not be echoed.
    rendered = json.dumps(report, default=_json_default)
    assert _EMAIL_SENTINEL not in rendered
    assert _TOKEN_SENTINEL not in rendered
    assert _NAME_SENTINEL not in rendered
    assert "not-a-uuid" not in rendered
    assert "an-array" not in rendered
    _assert_no_sensitive_keys(report)


async def test_audit_recognizes_active_managers_without_membership(
    test_engine, apply_migrations
) -> None:
    """Active managers never need a membership; everyone else still does."""
    ctx = await _seed_manager_cases(test_engine)
    ids = ctx["ids"]
    try:
        report = await _readonly_report(
            test_engine,
            run_id="audit-manager-test",
            generated_at=datetime.now(timezone.utc),
        )
    finally:
        await _cleanup(test_engine, ctx)

    gaps = report["findings"]["assignment_membership_gaps"]

    # Active super administrators and administrative owners are valid effective
    # reviewers/annotators without a membership row, whether the assignment is
    # explicit or inherited from the batch default.
    for key in (
        "task_super_reviewer",
        "task_manager_wrong_role",
        "task_inherited_super_reviewer",
    ):
        assert str(ids[key]) not in _item_ids(gaps["reviewer_missing_membership"]), key
        assert str(ids[key]) not in _item_ids(gaps["reviewer_wrong_membership_role"]), (
            key
        )
    for key in (
        "task_admin_owner_assignee",
        "task_inherited_admin_assignee",
        "task_override_manager",
    ):
        assert str(ids[key]) not in _item_ids(gaps["annotator_missing_membership"]), key
        assert str(ids[key]) not in _item_ids(
            gaps["annotator_wrong_membership_role"]
        ), key

    # Adversarial non-managers keep their findings: a foreign-project
    # administrator, ordinary employees (explicit and inherited), a
    # non-administrative owner and an ordinary wrong-role member.
    assert str(ids["task_foreign_admin"]) in _item_ids(
        gaps["annotator_missing_membership"]
    )
    assert str(ids["task_employee"]) in _item_ids(gaps["reviewer_missing_membership"])
    assert str(ids["task_inherited_employee"]) in _item_ids(
        gaps["annotator_missing_membership"]
    )
    assert str(ids["task_employee_owner"]) in _item_ids(
        gaps["annotator_missing_membership"]
    )
    assert str(ids["task_wrong_employee"]) in _item_ids(
        gaps["annotator_wrong_membership_role"]
    )

    # Inactive managers are never privileged: the missing-membership finding
    # and the inactive-account check both stay.  The inactive administrator owns
    # the project it is assigned in and the inactive super administrator would
    # manage any project, so these rows only survive when the active-account
    # check is present.
    assert str(ids["task_inactive_admin"]) in _item_ids(
        gaps["annotator_missing_membership"]
    )
    assert str(ids["task_inactive_admin"]) in _item_ids(
        gaps["assignment_to_inactive_account"]
    )
    assert str(ids["inactive_admin"]) in _item_ids(
        gaps["inactive_accounts_with_unfinished_work"]
    )
    assert str(ids["task_inactive_super"]) in _item_ids(
        gaps["reviewer_missing_membership"]
    )
    assert str(ids["task_inactive_super"]) in _item_ids(
        gaps["assignment_to_inactive_account"]
    )
    assert str(ids["inactive_super"]) in _item_ids(
        gaps["inactive_accounts_with_unfinished_work"]
    )

    # Suppressing the assignment gap does not hide the administrative
    # membership from the role reconciliation.
    roles = report["findings"]["role_discrepancies"]
    assert str(ids["super_admin_member"]) in _item_user_ids(
        roles["administrator_memberships"]
    )


async def test_active_manager_predicate_fails_closed(test_engine) -> None:
    """Exercise the pure-SQL manager rule on synthetic relation rows.

    No tables are read: ``ua`` and ``p`` are built from VALUES rows so the
    missing-project, missing-user, NULL-role and inactive-account branches are
    asserted directly against a real PostgreSQL connection.
    """
    predicate = active_manager_predicate("ua")
    sql = (
        "SELECT c.name, c.expected, NOT (" + predicate + ") AS is_gap "
        "FROM (VALUES "
        "(1, 'active super_admin', false, "
        "'00000000-0000-0000-0000-000000000001'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(2, 'active admin owner', false, "
        "'00000000-0000-0000-0000-000000000002'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(3, 'active admin non-owner', true, "
        "'00000000-0000-0000-0000-000000000003'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(4, 'active employee', true, "
        "'00000000-0000-0000-0000-000000000004'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(5, 'inactive super_admin', true, "
        "'00000000-0000-0000-0000-000000000005'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(6, 'NULL role', true, "
        "'00000000-0000-0000-0000-000000000006'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(7, 'missing user', true, "
        "'00000000-0000-0000-0000-0000000000ff'::uuid, "
        "'00000000-0000-0000-0000-0000000000a1'::uuid), "
        "(8, 'missing project super_admin', true, "
        "'00000000-0000-0000-0000-000000000001'::uuid, "
        "'00000000-0000-0000-0000-0000000000ff'::uuid), "
        "(9, 'missing project admin owner', true, "
        "'00000000-0000-0000-0000-000000000002'::uuid, "
        "'00000000-0000-0000-0000-0000000000ff'::uuid)"
        ") AS c(sort, name, expected, active_id, project_id) "
        "LEFT JOIN (VALUES "
        "('00000000-0000-0000-0000-000000000001'::uuid, true, 'super_admin'), "
        "('00000000-0000-0000-0000-000000000002'::uuid, true, 'project_admin'), "
        "('00000000-0000-0000-0000-000000000003'::uuid, true, 'project_admin'), "
        "('00000000-0000-0000-0000-000000000004'::uuid, true, 'employee'), "
        "('00000000-0000-0000-0000-000000000005'::uuid, false, 'super_admin'), "
        "('00000000-0000-0000-0000-000000000006'::uuid, true, NULL)"
        ") AS ua(id, is_active, role) ON ua.id = c.active_id "
        "LEFT JOIN (VALUES "
        "('00000000-0000-0000-0000-0000000000a1'::uuid, "
        "'00000000-0000-0000-0000-000000000002'::uuid)"
        ") AS p(id, owner_id) ON p.id = c.project_id "
        "ORDER BY c.sort"
    )
    async with test_engine.connect() as conn:
        rows = (await conn.execute(text(sql))).mappings().all()

    assert len(rows) == 9
    for row in rows:
        assert bool(row["is_gap"]) is bool(row["expected"]), row["name"]


def test_audit_runs_before_0173_migration(test_db_url, apply_migrations) -> None:
    """Synchronous migration test: Alembic must run outside the event loop."""
    migration = _load_migration_0173()
    state: dict = {"users": []}

    async def step(action: str) -> None:
        engine = create_async_engine(test_db_url)
        try:
            if action == "seed":
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    owner = await create_user(
                        db,
                        "project_admin",
                        f"{_EMAIL_SENTINEL}-pre-{uuid.uuid4()}@test.local",
                        f"{_NAME_SENTINEL}-pre",
                    )
                    state["users"].append(owner.id)
                    project = await create_project(db, owner_id=owner.id)
                    state["project"] = project.id
                    await create_membership(
                        db,
                        project_id=project.id,
                        user_id=owner.id,
                        role="viewer",
                        assigned_by=owner.id,
                    )
                    task = await create_task(db, project_id=project.id, status="review")
                    task.review_round_id = uuid.uuid4()
                    # An active administrative owner manages the project without
                    # a membership row on every schema revision.
                    task.assignee_id = owner.id
                    state["manager_task"] = task.id
                    future = datetime.now(timezone.utc) + timedelta(days=3)
                    legacy_invitation = UserInvitation(
                        email=f"{_EMAIL_SENTINEL}-pre-inv-{uuid.uuid4()}@test.local",
                        role="annotator",
                        project_id=project.id,
                        project_role=None,
                        token=f"{_TOKEN_SENTINEL}-{uuid.uuid4().hex}",
                        expires_at=future,
                        invited_by=owner.id,
                    )
                    account_invitation = UserInvitation(
                        email=f"{_EMAIL_SENTINEL}-pre-account-{uuid.uuid4()}@test.local",
                        role="viewer",
                        project_id=None,
                        project_role="annotator",
                        token=f"{_TOKEN_SENTINEL}-{uuid.uuid4().hex}",
                        expires_at=future,
                        invited_by=owner.id,
                    )
                    db.add_all([legacy_invitation, account_invitation])
                    await db.flush()
                    state["legacy_project_invitation"] = legacy_invitation.id
                    await db.commit()
            elif action == "simulate_pre_0173":
                # Replay only the 0173 schema Operations and align the version
                # pointer; never invoke the production 0174 downgrade.
                async with engine.begin() as conn:
                    if await conn.run_sync(_has_preparation_column):
                        await conn.run_sync(_replay_migration, migration, "downgrade")
                    await conn.run_sync(_set_alembic_version, "0172")
            elif action == "restore_head":
                async with engine.begin() as conn:
                    if not await conn.run_sync(_has_preparation_column):
                        await conn.run_sync(_replay_migration, migration, "upgrade")
                    await conn.run_sync(_set_alembic_version, "0174")
            elif action == "audit":
                async with read_only_audit_session(engine) as db:
                    state["report"] = await collect_report(db, run_id="pre-0173")
            else:  # cleanup exact seeded records
                async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                    if "project" in state:
                        await db.execute(
                            text("DELETE FROM tasks WHERE project_id = :p"),
                            {"p": state["project"]},
                        )
                        await db.execute(
                            text("DELETE FROM project_members WHERE project_id = :p"),
                            {"p": state["project"]},
                        )
                        await db.execute(
                            text("DELETE FROM projects WHERE id = :p"),
                            {"p": state["project"]},
                        )
                    if state["users"]:
                        await db.execute(
                            text(
                                "DELETE FROM user_invitations "
                                "WHERE invited_by = ANY(:u)"
                            ),
                            {"u": state["users"]},
                        )
                        await db.execute(
                            text("DELETE FROM users WHERE id = ANY(:u)"),
                            {"u": state["users"]},
                        )
                    await db.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(step("seed"))
        asyncio.run(step("simulate_pre_0173"))
        asyncio.run(step("audit"))
    finally:
        asyncio.run(step("restore_head"))
        asyncio.run(step("cleanup"))

    report = state["report"]
    assert report["baseline"]["schema_in_sync"] is False
    assert not any(report["baseline"]["schema_capabilities"].values())
    assert report["findings"]["review_evidence"]["complete"]["total"] == 0
    assert report["findings"]["review_evidence"]["incomplete"]["total"] >= 1
    assert report["summary"]["review_evidence_incomplete"] >= 1
    assert (
        report["findings"]["invitations"]["pending_project_role_to_backfill"]["total"]
        >= 1
    )
    assert (
        report["findings"]["invitations"]["account_invitation_with_project_role"][
            "total"
        ]
        == 0
    )
    # Manager authority does not depend on the 0173 preparation columns: the
    # active administrative owner stays a valid assignee pre-migration even
    # though its only member row is a non-annotator role.
    gaps = report["findings"]["assignment_membership_gaps"]
    assert str(state["manager_task"]) not in _item_ids(
        gaps["annotator_missing_membership"]
    )
    assert str(state["manager_task"]) not in _item_ids(
        gaps["annotator_wrong_membership_role"]
    )
    pending = {
        str(item["id"]): item
        for item in report["findings"]["invitations"]["pending"]["items"]
    }
    assert pending[str(state["legacy_project_invitation"])]["project_role"] is None
    rendered = json.dumps(report, default=_json_default)
    assert _EMAIL_SENTINEL not in rendered
    assert _TOKEN_SENTINEL not in rendered
    _assert_no_sensitive_keys(report)


def test_cli_smoke_against_isolated_database(test_db_url, apply_migrations) -> None:
    """Run the documented CLI against the isolated DB without a DSN argument."""
    env = os.environ.copy()
    # The CLI reads settings.database_url; provide the isolated test URL through
    # the environment, never on the command line or in output.
    env["DATABASE_URL"] = test_db_url
    result = subprocess.run(
        [
            sys.executable,
            str(_API_ROOT / "scripts" / "audit_project_roles.py"),
            "--max-rows",
            "1",
        ],
        cwd=_API_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["report_version"] == AUDIT_REPORT_VERSION
    assert report["read_only"] is True
    assert test_db_url not in result.stdout
    assert test_db_url not in result.stderr


def _assert_no_sensitive_keys(value, path: str = "") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in _SENSITIVE_KEYS, f"sensitive key {key} at {path}"
            _assert_no_sensitive_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_sensitive_keys(nested, f"{path}[{index}]")
