from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.services.notification import NotificationService


@pytest.mark.asyncio
async def test_application_role_reads_notifications_only_through_fixed_functions(
    test_db_url: str,
):
    app_url = make_url(settings.database_url)
    owner_url = make_url(test_db_url)
    assert app_url.database == owner_url.database
    assert app_url.database and app_url.database.endswith("_test")
    assert app_url.username and app_url.username != owner_url.username

    user_id = uuid.uuid4()
    notification_id = uuid.uuid4()
    task_id = uuid.uuid4()
    job_notification_id = uuid.uuid4()
    job_id = uuid.uuid4()
    owner_engine = create_async_engine(owner_url)
    app_engine = create_async_engine(app_url)
    try:
        async with owner_engine.begin() as connection:
            database = await connection.scalar(text("SELECT current_database()"))
            assert database == owner_url.database
            await connection.execute(
                text(
                    "INSERT INTO public.users "
                    "(id,email,name,password_hash,role,is_active,status) "
                    "VALUES (:id,:email,:name,:password_hash,'employee',true,'offline')"
                ),
                {
                    "id": user_id,
                    "email": f"notification-read-{user_id}@test.invalid",
                    "name": "Notification read relation fixture",
                    "password_hash": "test-only-placeholder",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO public.notifications "
                    "(id,user_id,type,target_type,target_id,payload) "
                    "VALUES (:id,:user_id,:type,:target_type,:target_id,"
                    "CAST(:payload AS pg_catalog.jsonb))"
                ),
                {
                    "id": notification_id,
                    "user_id": user_id,
                    "type": "read_relation.fixture",
                    "target_type": "task",
                    "target_id": task_id,
                    "payload": json.dumps({"read_relation": True}),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO public.notifications "
                    "(id,user_id,type,target_type,target_id,payload) "
                    "VALUES (:id,:user_id,'job.completed','async_job',:target_id,'{}')"
                ),
                {
                    "id": job_notification_id,
                    "user_id": user_id,
                    "target_id": job_id,
                },
            )

        async with AsyncSession(app_engine, expire_on_commit=False) as session:
            (
                role,
                table_select,
                table_column_select,
                view_select,
                owner_select,
                view_owner,
                security_barrier,
                is_login,
                is_unprivileged,
                has_role_membership,
            ) = (
                await session.execute(
                    text(
                        "SELECT current_user, "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.notifications', 'SELECT'), "
                        "pg_catalog.has_any_column_privilege(current_user, "
                        "'public.notifications', 'SELECT'), "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'aap_notifications_v1.notification_facts', 'SELECT'), "
                        "pg_catalog.has_table_privilege('notifications_code', "
                        "'public.notifications', 'SELECT'), "
                        "pg_catalog.pg_get_userbyid(relation.relowner), "
                        "relation.reloptions @> ARRAY['security_barrier=true'] "
                        ", role.rolcanlogin, "
                        "NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole "
                        "OR role.rolreplication OR role.rolbypassrls), "
                        "EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS membership "
                        "WHERE membership.member=role.oid) "
                        "FROM pg_catalog.pg_class AS relation "
                        ", pg_catalog.pg_roles AS role "
                        "WHERE relation.oid = "
                        "'aap_notifications_v1.notification_facts'::pg_catalog.regclass "
                        "AND role.rolname=current_user"
                    )
                )
            ).one()
            assert role == app_url.username
            assert table_select is False
            assert table_column_select is False
            assert view_select is False
            assert owner_select is True
            assert view_owner == "notifications_code"
            assert security_barrier is True
            assert is_login is True
            assert is_unprivileged is True
            assert has_role_membership is False

            for relation in (
                "public.notifications",
                "aap_notifications_v1.notification_facts",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with session.begin_nested():
                        await session.execute(
                            text(f"SELECT id FROM {relation} LIMIT 1")
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"

            service = NotificationService(session)
            loaded = await service.notification_by_id(notification_id)
            assert loaded is not None
            assert loaded.id == notification_id
            assert loaded.user_id == user_id
            assert loaded.payload == {"read_relation": True}
            assert await service.terminal_notification_exists(
                user_id, "job.completed", job_id
            )

            items, total, unread = await service.list_for_user(user_id)
            assert [item.id for item in items] == [notification_id]
            assert items[0] is loaded
            assert (total, unread) == (1, 1)
            assert await service.unread_count(user_id) == 1

            with pytest.raises(DBAPIError) as denied:
                await session.execute(
                    text("SELECT id FROM public.notifications WHERE id=:id"),
                    {"id": notification_id},
                )
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await session.rollback()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": user_id}
            )
        await app_engine.dispose()
        await owner_engine.dispose()
