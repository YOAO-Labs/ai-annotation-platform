from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aap_audit.read_relation_postgres import audit_events
from app.api.v1.audit_logs import _build_base_query, _managed_audit_scope
from app.db.enums import UserRole
from app.db.models.audit_log import AuditLog
from app.schemas.audit import AuditLogOut
from app.services.annotation_evidence import review_round_contributor_snapshot
from app.services.identity_contracts import IdentityActorFacts
from app.services.audit_read_relations import audit_event, execute_audit_relation_read


def test_audit_orm_alias_selects_only_the_public_relation():
    compiled = str(select(audit_event).compile(dialect=postgresql.dialect()))
    assert "FROM aap_audit_v1.audit_events AS audit_events" in compiled
    assert "public.audit_logs" not in compiled


def test_project_admin_filters_still_compose_workspace_and_identity_public_facts():
    actor = IdentityActorFacts(
        id=uuid.uuid4(),
        name="Project manager",
        email="manager@example.test",
        role=UserRole.PROJECT_ADMIN,
        is_active=True,
    )
    base, count = _build_base_query(
        "project.update",
        "project",
        "project-1",
        None,
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 31, tzinfo=timezone.utc),
        business_only=True,
        detail_key="result",
        detail_value="updated",
    )
    scope = _managed_audit_scope(actor, project_id=uuid.uuid4())
    list_statement = base.where(scope).compile(dialect=postgresql.dialect())
    list_sql = str(list_statement)
    count_sql = str(count.where(scope).compile(dialect=postgresql.dialect()))

    for sql in (list_sql, count_sql):
        assert "aap_audit_v1.audit_events" in sql
        assert "aap_workspace_v1.project_facts" in sql
        assert "aap_identity_v1.invitation_audit_targets" in sql
        assert "public.audit_logs" not in sql
    assert any(value == "http.%" for value in list_statement.params.values())
    assert " @> " in list_sql


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned PostgreSQL database and 0320 relation",
)
async def test_review_snapshot_reads_latest_successful_matching_round(db_session):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        review_round_id=uuid.uuid4(),
        first_review_contributor_ids=["fallback"],
        first_reviewed_at=None,
    )
    now = datetime.now(timezone.utc)
    cases = [
        ("task.submit", 200, {}, ["earlier"]),
        ("task.skip", 200, {}, ["b", "a", "b", None]),
        ("task.submit", 200, {"project_id": str(uuid.uuid4())}, ["other-project"]),
        ("task.submit", 200, {"review_round_id": str(uuid.uuid4())}, ["other-round"]),
        ("task.submit", 409, {}, ["failed"]),
        ("task.approve", 200, {}, ["other-action"]),
    ]
    for offset, (action, status, extra, contributors) in enumerate(cases):
        db_session.add(
            AuditLog(
                action=action,
                target_type="task",
                target_id=str(task.id),
                status_code=status,
                detail_json={
                    "project_id": str(task.project_id),
                    "review_round_id": str(task.review_round_id),
                    "contributor_ids": contributors,
                    **extra,
                },
                created_at=now + timedelta(microseconds=offset),
            )
        )

    assert await review_round_contributor_snapshot(db_session, task) == ["a", "b"]
    task.review_round_id = uuid.uuid4()
    assert await review_round_contributor_snapshot(db_session, task) == ["fallback"]
    task.first_reviewed_at = now
    assert await review_round_contributor_snapshot(db_session, task) == []


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned PostgreSQL database and 0320 relation",
)
@pytest.mark.parametrize(
    "ip_value",
    [
        "127.0.0.1",
        "2001:db8::1",
        "192.0.2.13/24",
        "2001:db8::2/64",
        "::ffff:192.0.2.1",
        "::ffff:192.0.2.1/120",
    ],
)
async def test_relation_reuses_orm_identity_and_preserves_native_inet_values(
    test_engine, ip_value
):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    expected_action = f"audit.read.relation.inet.{ip_value}"
    try:
        async with session_factory() as db:
            event = AuditLog(
                action=expected_action[:64],
                actor_email="stored@example.test",
                actor_role="project_admin",
                target_type="audit_read_probe",
                target_id="inet-parity",
                ip=ip_value,
                detail_json={"sample": ip_value},
                created_at=datetime.now(timezone.utc),
            )
            db.add(event)
            await db.flush()

            legacy = await db.scalar(select(AuditLog).where(AuditLog.id == event.id))
            assert legacy is event
            db.sync_session.expire(legacy, ["ip"])
            legacy = await db.scalar(select(AuditLog).where(AuditLog.id == event.id))
            old_raw_ip = legacy.ip
            old_output_ip = AuditLogOut.model_validate(legacy).ip

            db.sync_session.expire(legacy, ["ip"])
            from_relation = await db.scalar(
                select(audit_event).where(audit_event.id == event.id)
            )
            assert from_relation is legacy
            assert type(from_relation.ip) is type(old_raw_ip)
            assert from_relation.ip == old_raw_ip
            assert AuditLogOut.model_validate(from_relation).ip == old_output_ip
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned PostgreSQL database and 0320 relation",
)
async def test_relation_alias_preserves_dirty_expired_autoflush_and_same_uow(
    test_engine,
):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            row = AuditLog(
                action="audit.read.relation.identity",
                actor_email="before@example.test",
                target_type="audit_read_probe",
                target_id="identity",
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            await db.flush()

            cached = await db.scalar(
                select(audit_event).where(audit_event.id == row.id)
            )
            assert cached is row

            for statement in (
                select(AuditLog).where(AuditLog.id == row.id),
                select(audit_event).where(audit_event.id == row.id),
            ):
                with pytest.raises(DBAPIError, match="audit_logs rows are immutable"):
                    async with db.begin_nested():
                        row.actor_email = "dirty@example.test"
                        with db.no_autoflush:
                            same = await db.scalar(statement)
                        assert same is row
                        assert same.actor_email == "dirty@example.test"
                        assert db.is_modified(row)
                        await db.scalar(statement)

                assert db.in_transaction()
                db.sync_session.expire(row, ["actor_email"])
                same = await db.scalar(statement)
                assert same is row
                assert same.actor_email == "before@example.test"
                assert not db.is_modified(row)

            duplicate_at = datetime.now(timezone.utc)
            duplicate = AuditLog(
                action="audit.read.relation.composite-key",
                target_type="audit_read_probe",
                target_id="composite-key",
                created_at=duplicate_at,
            )
            db.add(duplicate)
            await db.flush()
            second_at = duplicate_at + timedelta(microseconds=1)
            await db.execute(
                text(
                    "INSERT INTO public.audit_logs "
                    "(id, action, target_type, target_id, created_at) "
                    "VALUES (:id, :action, :target_type, :target_id, :created_at)"
                ),
                {
                    "id": duplicate.id,
                    "action": "audit.read.relation.composite-key.second",
                    "target_type": "audit_read_probe",
                    "target_id": "composite-key",
                    "created_at": second_at,
                },
            )
            composite_rows = (
                await db.execute(
                    select(
                        audit_events.c.id,
                        audit_events.c.created_at,
                        audit_events.c.action,
                    )
                    .where(audit_events.c.id == duplicate.id)
                    .order_by(audit_events.c.created_at)
                )
            ).all()
            assert len(composite_rows) == 2
            assert [item.id for item in composite_rows] == [duplicate.id] * 2
            assert [item.created_at for item in composite_rows] == [
                duplicate_at,
                second_at,
            ]
            assert [item.action for item in composite_rows] == [
                "audit.read.relation.composite-key",
                "audit.read.relation.composite-key.second",
            ]

            pending = AuditLog(
                action="audit.read.relation.core.flush",
                target_type="audit_read_probe",
                target_id="core-flush",
                created_at=datetime.now(timezone.utc),
            )
            db.add(pending)
            result = await execute_audit_relation_read(
                db,
                select(audit_events.c.id).where(
                    audit_events.c.action == "audit.read.relation.core.flush"
                ),
            )
            assert result.scalar_one() == pending.id

            not_flushed = AuditLog(
                action="audit.read.relation.core.no-autoflush",
                target_type="audit_read_probe",
                target_id="core-no-autoflush",
                created_at=datetime.now(timezone.utc),
            )
            db.add(not_flushed)
            with db.no_autoflush:
                result = await execute_audit_relation_read(
                    db,
                    select(audit_events.c.id).where(
                        audit_events.c.action == "audit.read.relation.core.no-autoflush"
                    ),
                )
            assert result.scalar_one_or_none() is None
            assert not_flushed.id is None
            assert db.in_transaction()
            await db.rollback()
    finally:
        await engine.dispose()
