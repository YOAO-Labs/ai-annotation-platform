from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aap_audit.append_contract import AppendEvent, AppendRequest
from aap_audit.postgres import AuditAppendPostgresData
from aap_audit.query_contract import (
    AuditQueryAfter,
    AuditQueryFilters,
    AuditQueryRequest,
    decode_query_reply,
    validate_query_reply,
)
from aap_audit.query_postgres import AuditQueryPostgresData
from app.api.v1.audit_logs import _legacy_query_datetime, _query_filters
from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from app.schemas.audit import AuditLogOut
from app.services.audit import AuditService
from app.services.audit_origin_contract import AuditOriginContext

_BASELINE_ACTOR_ID = UUID("00000000-0000-4000-8000-000000000315")


async def test_owner_append_ignores_caller_temporary_type_shadowing(test_engine):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("CREATE TEMP TABLE audit_shadow_probe (id int)")
            )
            await connection.execute(text("CREATE TYPE pg_temp.jsonb AS (value text)"))
            reply = await AuditAppendPostgresData(connection).append(
                AppendRequest.create(
                    [
                        AppendEvent(
                            **{
                                name: "audit.namespace_probe"
                                if name == "action"
                                else None
                                for name in AppendEvent.model_fields
                            }
                        )
                    ]
                )
            )
            assert reply.ok is True
            assert len(reply.data.events) == 1
            await connection.rollback()
    finally:
        await engine.dispose()


async def test_owner_query_accepts_distinct_rows_with_the_same_id(db_session):
    event_id = -(uuid4().int % (1 << 62)) - 1
    action = f"audit.composite.{uuid4().hex}"
    await db_session.execute(
        text(
            "INSERT INTO public.audit_logs (id, action, created_at) VALUES "
            "(:id, :action, CURRENT_TIMESTAMP), "
            "(:id, :action, CURRENT_TIMESTAMP + INTERVAL '1 microsecond')"
        ),
        {"id": event_id, "action": action},
    )
    request = AuditQueryRequest.list_page(
        AuditQueryFilters(
            action=action,
            target_type=None,
            target_id=None,
            actor_id=None,
            from_=None,
            to=None,
            business_only=False,
            detail_key=None,
            detail_value=None,
        ),
        page=1,
        page_size=2,
    )
    reply = await AuditQueryPostgresData(db_session).query(request)
    assert reply.data.total == "2"
    assert [event.id for event in reply.data.events] == [str(event_id), str(event_id)]
    assert len({event.created_at for event in reply.data.events}) == 2


async def test_owner_query_ignores_caller_temporary_type_shadowing(test_engine):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    filters = AuditQueryFilters(
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=None,
        to=None,
        business_only=False,
        detail_key=None,
        detail_value=None,
    )
    request = AuditQueryRequest.list_page(filters, page=1, page_size=1)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("CREATE TYPE pg_temp.jsonb AS (value text)"))
            raw_reply = await connection.scalar(
                text(
                    "SELECT aap_audit_v1.query_events("
                    "CAST(:request AS pg_catalog.jsonb))"
                ),
                {"request": request.model_dump_json(by_alias=True)},
            )
            reply = validate_query_reply(request, decode_query_reply(raw_reply))
            assert reply.data.operation == "list"
            assert len(reply.data.events) <= 1
            await connection.rollback()
    finally:
        await engine.dispose()


async def test_owner_query_time_bounds_match_legacy_asyncpg_binding(test_engine):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    target_type = "audit_query_time_parity"
    target_id = str(uuid4())
    lower = datetime.fromisoformat("2026-10-20T12:00:00+00:00")
    upper = datetime.fromisoformat("2026-10-20T13:00:00+00:00")
    rows = [
        ("audit.query.time.before", lower.replace(hour=11)),
        ("audit.query.time.lower", lower),
        ("audit.query.time.upper", upper),
        ("audit.query.time.after", upper.replace(hour=14)),
    ]

    def error_result(error):
        original = getattr(error, "orig", error)
        return (
            "error",
            type(error).__name__,
            getattr(original, "sqlstate", None),
        )

    async def legacy_result(db, *, from_, to):
        statement = select(AuditLog.id).where(
            AuditLog.target_type == target_type,
            AuditLog.target_id == target_id,
        )
        if from_ is not None:
            statement = statement.where(AuditLog.created_at >= from_)
        if to is not None:
            statement = statement.where(AuditLog.created_at <= to)
        statement = statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        savepoint = await db.begin_nested()
        try:
            ids = (await db.scalars(statement)).all()
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [str(id_) for id_ in ids]

    async def owner_result(db, *, from_, to):
        filters = _query_filters(
            action=None,
            target_type=target_type,
            target_id=target_id,
            actor_id=None,
            from_=from_,
            to=to,
            business_only=False,
            detail_key=None,
            detail_value=None,
        )
        request = AuditQueryRequest.list_page(filters, page=1, page_size=200)
        savepoint = await db.begin_nested()
        try:
            reply = await AuditQueryPostgresData(db).query(request)
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [event.id for event in reply.data.events]

    async def legacy_cursor_result(db, *, created_at, id_):
        statement = select(AuditLog.id).where(
            AuditLog.target_type == target_type,
            AuditLog.target_id == target_id,
            (AuditLog.created_at < created_at)
            | ((AuditLog.created_at == created_at) & (AuditLog.id < id_)),
        )
        statement = statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        savepoint = await db.begin_nested()
        try:
            ids = (await db.scalars(statement)).all()
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [str(id_) for id_ in ids]

    async def owner_cursor_result(db, *, created_at, id_):
        filters = _query_filters(
            action=None,
            target_type=target_type,
            target_id=target_id,
            actor_id=None,
            from_=None,
            to=None,
            business_only=False,
            detail_key=None,
            detail_value=None,
        )
        request = AuditQueryRequest.list_page(
            filters,
            page=99,
            page_size=200,
            after=AuditQueryAfter(
                created_at=_legacy_query_datetime(created_at).isoformat(),
                id=str(id_),
            ),
        )
        savepoint = await db.begin_nested()
        try:
            reply = await AuditQueryPostgresData(db).query(request)
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [event.id for event in reply.data.events]

    try:
        entries = [
            AuditLog(
                action=action,
                target_type=target_type,
                target_id=target_id,
                created_at=created_at,
            )
            for action, created_at in rows
        ]
        async with session_factory() as db:
            db.add_all(entries)
            await db.flush()
            expected_ids = [str(entry.id) for entry in (entries[2], entries[1])]
            legacy_aware = await legacy_result(db, from_=lower, to=upper)
            owner_aware = await owner_result(db, from_=lower, to=upper)
            assert owner_aware == legacy_aware == ("ok", expected_ids)

            naive_lower = lower.replace(tzinfo=None)
            naive_upper = upper.replace(tzinfo=None)
            for from_, to in ((naive_lower, None), (None, naive_upper)):
                owner_naive = await owner_result(db, from_=from_, to=to)
                legacy_naive = await legacy_result(db, from_=from_, to=to)
                assert owner_naive[0] == legacy_naive[0], (
                    owner_naive,
                    legacy_naive,
                )
                if owner_naive[0] == "ok":
                    assert owner_naive[1] == legacy_naive[1]

            naive_cursor = upper.astimezone().replace(tzinfo=None)
            owner_cursor = await owner_cursor_result(
                db, created_at=naive_cursor, id_=entries[2].id
            )
            legacy_cursor = await legacy_cursor_result(
                db, created_at=naive_cursor, id_=entries[2].id
            )
            assert owner_cursor == legacy_cursor
            await db.rollback()
    finally:
        await engine.dispose()


async def test_owner_query_actor_uuid_forms_match_legacy_asyncpg_binding(test_engine):
    engine = create_async_engine(test_engine.url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    actor_id = uuid4()
    target_type = "audit_query_actor_parity"
    target_id = str(uuid4())

    def error_result(error):
        original = getattr(error, "orig", error)
        return (
            "error",
            type(error).__name__,
            getattr(original, "sqlstate", None),
        )

    async def legacy_result(db, actor_value):
        statement = select(AuditLog.id).where(
            AuditLog.target_type == target_type,
            AuditLog.target_id == target_id,
            AuditLog.actor_id == actor_value,
        )
        savepoint = await db.begin_nested()
        try:
            ids = (await db.scalars(statement)).all()
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [str(id_) for id_ in ids]

    async def owner_result(db, actor_value):
        filters = _query_filters(
            action=None,
            target_type=target_type,
            target_id=target_id,
            actor_id=actor_value,
            from_=None,
            to=None,
            business_only=False,
            detail_key=None,
            detail_value=None,
        )
        request = AuditQueryRequest.list_page(filters, page=1, page_size=20)
        savepoint = await db.begin_nested()
        try:
            reply = await AuditQueryPostgresData(db).query(request)
        except Exception as error:
            await savepoint.rollback()
            return error_result(error)
        await savepoint.commit()
        return "ok", [event.id for event in reply.data.events]

    try:
        async with session_factory() as db:
            user = _user(actor_id)
            db.add(user)
            await db.flush()
            db.add(
                AuditLog(
                    actor_id=actor_id,
                    actor_email=user.email,
                    actor_role=user.role,
                    action="audit.query.actor.parity",
                    target_type=target_type,
                    target_id=target_id,
                )
            )
            await db.flush()
            entry_id = await db.scalar(
                select(AuditLog.id).where(AuditLog.target_id == target_id)
            )
            for actor_value in (
                str(actor_id),
                actor_id.hex,
                "{" + str(actor_id) + "}",
            ):
                legacy = await legacy_result(db, actor_value)
                owner = await owner_result(db, actor_value)
                assert owner[0] == legacy[0], (actor_value, owner, legacy)
                if legacy[0] == "ok":
                    assert owner[1] == legacy[1] == [str(entry_id)]
            await db.rollback()
    finally:
        await engine.dispose()


def _user(actor_id=None):
    actor_id = actor_id or uuid4()
    return User(
        id=actor_id,
        email=f"audit-append-{actor_id}@example.test",
        name="Audit append baseline",
        password_hash="opaque-test-hash",
        role="employee",
    )


async def _pending_new_actor_result(engine, *, owner_append: bool):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        actor = _user(_BASELINE_ACTOR_ID)
        db.add(actor)
        try:
            with db.no_autoflush:
                if owner_append:
                    entry = await AuditService.log(
                        db,
                        actor=actor,
                        action="audit.pending_new_actor",
                        target_type="user",
                        target_id=actor.id,
                        status_code=200,
                    )
                else:
                    entry = AuditLog(
                        actor_id=actor.id,
                        actor_email=actor.email,
                        actor_role=actor.role,
                        action="audit.pending_new_actor",
                        target_type="user",
                        target_id=str(actor.id),
                        status_code=200,
                    )
                    db.add(entry)
                    await db.flush()
        except IntegrityError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            await db.rollback()
            return ("integrity-error", state)
        else:
            result = ("inserted", entry.actor_id, entry.actor_email, entry.actor_role)
            await db.rollback()
            return result


async def test_owner_append_preserves_pending_new_actor_orm_baseline(test_engine):
    legacy = await _pending_new_actor_result(test_engine, owner_append=False)
    owner = await _pending_new_actor_result(test_engine, owner_append=True)
    assert legacy == ("integrity-error", "23503")
    assert owner == legacy


async def _pending_deleted_actor_result(engine, *, owner_append: bool):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        actor = _user(_BASELINE_ACTOR_ID)
        db.add(actor)
        await db.flush()
        await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await db.delete(actor)
        with db.no_autoflush:
            if owner_append:
                entry = await AuditService.log(
                    db,
                    actor=actor,
                    action="audit.pending_deleted_actor",
                    target_type="user",
                    target_id=actor.id,
                    status_code=200,
                )
            else:
                entry = AuditLog(
                    actor_id=actor.id,
                    actor_email=actor.email,
                    actor_role=actor.role,
                    action="audit.pending_deleted_actor",
                    target_type="user",
                    target_id=str(actor.id),
                    status_code=200,
                )
                db.add(entry)
                await db.flush()
        stored_actor_id = await db.scalar(
            text("SELECT actor_id FROM audit_logs WHERE id=:id"), {"id": entry.id}
        )
        result = (
            "inserted",
            stored_actor_id,
            entry.actor_id,
            entry.actor_email,
            entry.actor_role,
        )
        await db.rollback()
        return result


async def test_owner_append_preserves_pending_deleted_actor_orm_baseline(test_engine):
    legacy = await _pending_deleted_actor_result(test_engine, owner_append=False)
    owner = await _pending_deleted_actor_result(test_engine, owner_append=True)
    assert owner == legacy
    assert owner[0:2] == ("inserted", None)
    assert owner[2] == _BASELINE_ACTOR_ID
    assert owner[3].startswith("audit-append-")
    assert owner[4] == "employee"


async def test_owner_append_preserves_json_null_inet_and_transaction_timestamp(
    test_engine,
):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as db:
        actor = _user(_BASELINE_ACTOR_ID)
        db.add(actor)
        await db.flush()
        baseline = AuditLog(
            actor_id=actor.id,
            actor_email=actor.email,
            actor_role=actor.role,
            action="audit.orm.baseline",
            method="POST",
            path="/audit-baseline",
            status_code=201,
            ip="127.0.0.1",
            detail_json=None,
        )
        db.add(baseline)
        await db.flush()

        statements = []

        def capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", capture_sql)
        try:
            with db.no_autoflush:
                appended = await AuditService.log(
                    db,
                    actor=actor,
                    action="audit.owner.append",
                    target_type="user",
                    target_id=actor.id,
                    origin=AuditOriginContext(
                        method="POST", path="/audit-baseline", ip="127.0.0.1"
                    ),
                    status_code=201,
                    detail=None,
                )
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", capture_sql)
        assert not any(
            "select" in statement.lower() and "audit_logs" in statement.lower()
            for statement in statements
        )

        legacy_out = AuditLogOut.model_validate(baseline)
        owner_out = AuditLogOut.model_validate(appended)
        assert owner_out.ip == legacy_out.ip == "127.0.0.1"
        assert owner_out.detail_json == legacy_out.detail_json
        assert owner_out.actor_email == legacy_out.actor_email == actor.email
        assert owner_out.actor_role == legacy_out.actor_role == actor.role

        rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, created_at, detail_json IS NULL AS sql_null, "
                        "detail_json = 'null'::jsonb AS json_null, ip::text AS ip_text "
                        "FROM audit_logs WHERE id = ANY(:ids) ORDER BY id"
                    ),
                    {"ids": [baseline.id, appended.id]},
                )
            )
            .mappings()
            .all()
        )
        transaction_time = await db.scalar(text("SELECT transaction_timestamp()"))

        assert len(rows) == 2
        assert all(
            row["sql_null"] is False and row["json_null"] is True for row in rows
        )
        assert all(row["ip_text"] == "127.0.0.1/32" for row in rows)
        assert all(row["created_at"] == transaction_time for row in rows)
        assert isinstance(transaction_time, datetime)
        assert await db.get(AuditLog, appended.id) is appended
        await db.rollback()
