"""Owned restore proves membership receipt replay and downgrade protection."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_workspace.contract import decode_membership_request
from aap_workspace.manager import MembershipManager
from aap_workspace.postgres import MembershipPostgresData
from app.db.models.project import Project
from app.db.models.user import User
from tests.test_identity_recovery import _alembic, _postgres_tool

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned Workspace membership acceptance database",
    ),
]


def _membership_dsn(test_engine, database: str | None = None) -> str:
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_MEMBERSHIP_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "workspace_membership_client"
    if database is not None:
        parsed = parsed.set(database=database)
    else:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _add_request(actor_id: uuid.UUID, project_id: uuid.UUID, target_id: uuid.UUID):
    return decode_membership_request(
        {
            "schema": "aap-workspace-membership/v1",
            "operation_id": str(uuid.uuid4()),
            "operation": "add_member",
            "actor_id": str(actor_id),
            "project_id": str(project_id),
            "payload": {
                "target_user_id": str(target_id),
                "project_role": "annotator",
            },
            "audit_context": {
                "method": None,
                "path": None,
                "ip": None,
                "request_id": str(uuid.uuid4()),
            },
        }
    )


async def test_membership_receipt_replays_after_restore_and_blocks_populated_downgrade(
    test_engine, tmp_path
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id, target_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    operation_id = uuid.uuid4()
    actor_email = f"workspace-membership-restore-{actor_id.hex}@example.test"
    target_email = f"workspace-membership-target-{target_id.hex}@example.test"
    display_id = f"WM-{project_id.hex[:16]}"
    project = Project(
        id=project_id,
        display_id=display_id,
        name="Workspace membership restore",
        type_label="Image",
        type_key="image",
        owner_id=actor_id,
    )
    request = _add_request(actor_id, project_id, target_id)
    request = request.model_copy(update={"operation_id": operation_id})
    clone = f"aap_workspace_membership_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = f"workspace-membership-restore:{uuid.uuid4()}"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    restored_owner = create_async_engine(
        test_engine.url.set(database=clone), hide_parameters=True
    )
    snapshot = tmp_path / "workspace-membership.dump"
    created = False

    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=actor_id,
                        email=actor_email,
                        name="Workspace membership administrator",
                        password_hash="fixture-only-workspace-membership",
                        role="super_admin",
                        is_active=True,
                        status="online",
                    ),
                    User(
                        id=target_id,
                        email=target_email,
                        name="Workspace membership target",
                        password_hash="fixture-only-workspace-target",
                        role="employee",
                        is_active=True,
                        status="online",
                    ),
                ]
            )
            await session.flush()
            session.add(project)

        dsn = _membership_dsn(test_engine)
        original_data = MembershipPostgresData(dsn)
        original_policy = await original_data.membership_policy()
        original = await MembershipManager(original_data).execute(request)
        assert original.ok is True
        assert original.data.member.id
        assert original.data.member.role == "annotator"

        async with maker() as session:
            receipt_text = await session.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.workspace_membership_operation_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt_text is not None
            receipt = json.loads(receipt_text)
            assert len(receipt["request_hash"]) == 64
            assert receipt["audit_id"] > 0
            assert "request" not in receipt
            assert receipt["result"]["operation_id"] == str(operation_id)
            assert receipt["result"]["data"]["member"]["id"] == str(
                original.data.member.id
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor AND target_id=:member "
                    "AND action='project.member_add' AND request_id=:request_id"
                ),
                {
                    "actor": actor_id,
                    "member": str(original.data.member.id),
                    "request_id": request.audit_context.request_id,
                },
            )
            assert audit_count == 1

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), "
                            "entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database CROSS JOIN LATERAL "
                            "aclexplode(COALESCE(database.datacl, "
                            "acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )

        restored_dsn = _membership_dsn(test_engine, clone)
        restored_data = MembershipPostgresData(restored_dsn)
        assert await restored_data.membership_policy() == original_policy
        replay = await MembershipManager(restored_data).execute(request)
        assert replay == original
        async with restored_owner.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND target_id=:member "
                        "AND action='project.member_add' AND request_id=:request_id"
                    ),
                    {
                        "actor": actor_id,
                        "member": str(original.data.member.id),
                        "request_id": request.audit_context.request_id,
                    },
                )
                == audit_count
            )

        refused = _alembic(test_engine.url.set(database=clone), "downgrade", "0252")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Workspace membership ownership while receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored_owner.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0276"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') "
                        "FROM pg_database WHERE datname=current_database()"
                    )
                )
                == ownership
            )
            await db.execute(
                text("DELETE FROM public.workspace_membership_operation_receipts")
            )
        empty = _alembic(test_engine.url.set(database=clone), "downgrade", "0252")
        assert empty.returncode == 0, (
            "Owned empty Workspace membership downgrade failed"
        )
        async with restored_owner.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0252"
            )
            assert await db.scalar(
                text("SELECT to_regnamespace('aap_workspace_membership_v1') IS NULL")
            )
            assert await db.scalar(
                text(
                    "SELECT to_regclass('public.workspace_membership_operation_receipts') IS NULL"
                )
            )
    finally:
        await restored_owner.dispose()
        if created:
            async with admin.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') "
                            "FROM pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    == ownership
                )
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await db.execute(text(f'DROP DATABASE "{clone}"'))
        await admin.dispose()
        async with maker.begin() as session:
            await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await session.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=:actor "
                    "AND request_id=:request_id"
                ),
                {"actor": actor_id, "request_id": request.audit_context.request_id},
            )
            await session.execute(
                text(
                    "DELETE FROM public.workspace_membership_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:project_id"),
                {"project_id": project_id},
            )
            await session.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": [actor_id, target_id]},
            )
        snapshot.unlink(missing_ok=True)
