"""Source-level guardrails for Labeling's member-work capabilities.

The owning migration verifier exercises the SQL against PostgreSQL. These
small pure checks pin the exact callable surface and the lock/effect boundaries
without pretending to replace that database qualification.
"""

from __future__ import annotations

import re
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


_SQL = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "sql"
    / "0253_workspace_membership_labeling.sql"
)
_SIGNATURES = {
    "read_membership_work": "uuid, uuid, uuid, uuid",
    "lock_membership_work": "uuid, uuid, text, boolean",
    "handoff_membership_annotation": "uuid, uuid, uuid",
    "handoff_membership_reviewer": "uuid, uuid, uuid",
    "release_membership_task_locks": "uuid, uuid",
}


def _function_body(sql: str, name: str) -> str:
    match = re.search(
        rf"CREATE FUNCTION aap_labeling_v1\.{name}\(.*?AS \$function\$(.*?)\$function\$;",
        sql,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing {name} function body"
    return match.group(1)


def test_membership_labeling_public_capabilities_match_the_frozen_contract() -> None:
    sql = _SQL.read_text()
    signatures = re.findall(
        r"CREATE FUNCTION aap_labeling_v1\.([a-z_]+)\((.*?)\)\s*RETURNS",
        sql,
        flags=re.DOTALL,
    )
    normalized = {
        name: ", ".join(re.findall(r"\bp_[a-z_]+\s+(uuid|text|boolean)\b", arguments))
        for name, arguments in signatures
        if not name.startswith("_")
    }
    assert normalized == _SIGNATURES
    for name, arguments in _SIGNATURES.items():
        assert (
            f"REVOKE ALL ON FUNCTION aap_labeling_v1.{name}({arguments})\n"
            "    FROM PUBLIC;"
        ) in sql

    referenced_tables = set(re.findall(r"\bpublic\.([a-z_]+)\b", sql))
    assert referenced_tables == {"tasks", "task_batches", "task_locks"}
    assert "public.users" not in sql
    assert "public.projects" not in sql
    assert "public.project_members" not in sql


def test_read_facts_keep_exact_project_scope_and_unknown_evidence() -> None:
    sql = _SQL.read_text()
    body = _function_body(sql, "read_membership_work")
    expected_keys = {
        "schema",
        "project_id",
        "user_id",
        "annotation_task_ids",
        "review_task_ids",
        "unassigned_review_task_ids",
        "lock_task_ids",
        "review_claim_task_ids",
        "materialization_annotation_task_ids",
        "materialization_review_task_ids",
        "batch_annotator_ids",
        "batch_reviewer_ids",
        "tasks",
        "batches",
        "annotation_receiver_conflict_task_ids",
        "review_receiver_unknown_task_ids",
        "review_receiver_annotator_task_ids",
        "review_receiver_contributor_task_ids",
    }
    for key in expected_keys:
        assert f"'{key}'" in body
    assert "'aap-labeling-membership-work/v1'" in body
    assert "task.project_id = p_project_id" in body
    assert "COALESCE(task.annotation_contributor_ids, 'null'::jsonb)" in body
    assert "'annotation_contributor_ids', task.annotation_contributor_ids" in body
    assert "'review_contributor_ids', task.review_contributor_ids" in body
    assert "COALESCE(task.assignee_id, batch.annotator_id)" in body
    assert "COALESCE(task.reviewer_id, batch.reviewer_id)" in body


def test_lock_phases_preserve_resource_order_and_error_details() -> None:
    body = _function_body(_SQL.read_text(), "lock_membership_work")
    phases = (
        "PERFORM batch.id",
        "PERFORM task.id",
        "pg_try_advisory_xact_lock",
        "PERFORM task_lock.id",
    )
    positions = [body.index(phase) for phase in phases]
    assert positions == sorted(positions)
    assert body.count("FOR UPDATE NOWAIT") == 2
    assert "FOR UPDATE OF task_lock NOWAIT" in body
    assert "'aap:task-edit-lock:' || v_task_id::text" in body
    assert "批次资源正在变更，请刷新后重试" in body
    assert "任务资源正在变更，请刷新后重试" in body
    assert "任务正在编辑，请刷新后重试" in body
    assert "任务锁正在变更，请刷新后重试" in body
    assert (
        "WHEN lock_not_available OR deadlock_detected OR serialization_failure" in body
    )
    assert "IF NOT p_is_removal THEN" in body


def test_handoffs_keep_terminal_attribution_and_legacy_assignment_values() -> None:
    sql = _SQL.read_text()
    annotation = _function_body(sql, "handoff_membership_annotation")
    reviewer = _function_body(sql, "handoff_membership_reviewer")
    release = _function_body(sql, "release_membership_task_locks")

    assert "task.status <> ALL(v_unfinished_annotation)" in annotation
    assert "task.assignee_id IS NULL" in annotation
    assert "task.status = 'completed'" in reviewer
    assert "task.reviewer_id IS NULL" in reviewer
    assert "reviewer_claimed_at = NULL" in reviewer
    assert "assigned_user_ids = v_updated" in annotation
    assert "assigned_user_ids = v_updated" in reviewer
    assert "'True'" in annotation and "'None'" in annotation
    assert "'True'" in reviewer and "'None'" in reviewer
    assert "assigned_at" not in sql
    assert "updated_at = pg_catalog.now()" in annotation
    assert "updated_at = pg_catalog.now()" in reviewer
    assert "updated_at = pg_catalog.now()" in sql
    assert "task.project_id = p_project_id" in release
    assert "task_lock.user_id = p_user_id" in release
    assert "task_lock.expire_at > pg_catalog.clock_timestamp()" in release


@pytest.mark.asyncio
async def test_read_membership_work_keeps_unknown_distinct_from_empty_evidence(
    db_session,
) -> None:
    from sqlalchemy import text

    from tests.factory import create_project, create_task, create_user

    owner = await create_user(
        db_session,
        "project_admin",
        f"membership-owner-{uuid.uuid4()}@test.local",
        "Owner",
    )
    target = await create_user(
        db_session,
        "employee",
        f"membership-target-{uuid.uuid4()}@test.local",
        "Target",
    )
    receiver = await create_user(
        db_session,
        "employee",
        f"membership-receiver-{uuid.uuid4()}@test.local",
        "Receiver",
    )
    project = await create_project(db_session, owner_id=owner.id)
    unknown = await create_task(db_session, project_id=project.id, status="review")
    unknown.reviewer_id = target.id
    unknown.annotation_contributor_ids = None
    unknown.review_contributor_ids = [str(receiver.id)]

    known_empty = await create_task(db_session, project_id=project.id, status="review")
    known_empty.reviewer_id = target.id
    known_empty.annotation_contributor_ids = []
    known_empty.review_contributor_ids = [str(receiver.id)]

    unassigned = await create_task(db_session, project_id=project.id, status="review")
    await db_session.flush()

    facts = await db_session.scalar(
        text(
            "SELECT aap_labeling_v1.read_membership_work("
            ":project_id, :user_id, NULL, :receiver_id)"
        ),
        {
            "project_id": project.id,
            "user_id": target.id,
            "receiver_id": receiver.id,
        },
    )
    if isinstance(facts, str):
        facts = json.loads(facts)
    assert facts["review_receiver_unknown_task_ids"] == [str(unknown.id)]
    assert facts["review_receiver_contributor_task_ids"] == [str(known_empty.id)]
    assert facts["unassigned_review_task_ids"] == [str(unassigned.id)]

    task_facts = {row["id"]: row for row in facts["tasks"]}
    assert task_facts[str(unknown.id)]["annotation_contributor_ids"] is None
    assert task_facts[str(known_empty.id)]["annotation_contributor_ids"] == []


@pytest.mark.asyncio
async def test_annotation_handoff_matches_frozen_legacy_assigned_id_vectors(
    db_session,
) -> None:
    from sqlalchemy import text

    from tests.factory import create_batch, create_project, create_task, create_user

    owner = await create_user(
        db_session,
        "project_admin",
        f"membership-vector-owner-{uuid.uuid4()}@test.local",
        "Owner",
    )
    source = await create_user(
        db_session,
        "employee",
        f"membership-vector-source-{uuid.uuid4()}@test.local",
        "Source",
    )
    receiver = await create_user(
        db_session,
        "employee",
        f"membership-vector-receiver-{uuid.uuid4()}@test.local",
        "Receiver",
    )
    project = await create_project(db_session, owner_id=owner.id)

    fixture = json.loads(
        (
            Path(__file__).resolve().parent
            / "fixtures/workspace_membership_legacy.json"
        ).read_text()
    )
    vectors = []
    for vector in fixture["assigned_user_id_vectors"]:

        def bind(value):
            if isinstance(value, list):
                return [bind(item) for item in value]
            if isinstance(value, dict):
                return {bind(key): bind(item) for key, item in value.items()}
            if isinstance(value, str):
                return value.replace(vector["from_user_id"], str(source.id)).replace(
                    vector["to_user_id"], str(receiver.id)
                )
            return value

        vectors.append((bind(vector["input"]), bind(vector["output"])))
    original_assigned_at = datetime(2020, 1, 2, tzinfo=timezone.utc)
    original_updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    saved: list[tuple[uuid.UUID, uuid.UUID, list[str]]] = []

    for assigned_user_ids, expected in vectors:
        batch = await create_batch(
            db_session,
            project_id=project.id,
            status="active",
        )
        batch.annotator_id = source.id
        batch.assigned_user_ids = assigned_user_ids
        batch.updated_at = original_updated_at
        task = await create_task(db_session, project_id=project.id, status="pending")
        task.batch_id = batch.id
        task.assigned_at = original_assigned_at
        task.updated_at = original_updated_at
        saved.append((batch.id, task.id, expected))

    await db_session.flush()
    await db_session.execute(
        text(
            "SELECT aap_labeling_v1.handoff_membership_annotation("
            ":project_id, :source_id, :receiver_id)"
        ),
        {
            "project_id": project.id,
            "source_id": source.id,
            "receiver_id": receiver.id,
        },
    )

    for batch_id, task_id, expected in saved:
        raw_assigned_user_ids = await db_session.scalar(
            text(
                "SELECT assigned_user_ids::text FROM public.task_batches "
                "WHERE id = :batch_id"
            ),
            {"batch_id": batch_id},
        )
        assert json.loads(raw_assigned_user_ids) == expected
        task_state = (
            await db_session.execute(
                text(
                    "SELECT assignee_id, assigned_at, updated_at FROM public.tasks "
                    "WHERE id = :task_id"
                ),
                {"task_id": task_id},
            )
        ).one()
        assert task_state.assignee_id == receiver.id
        assert task_state.assigned_at == original_assigned_at
        assert task_state.updated_at != original_updated_at
