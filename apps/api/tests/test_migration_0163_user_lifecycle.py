"""Verify legacy lifecycle classification through the real migration."""

import asyncio
import uuid

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.test_migration_0173_project_role_preparation import _replay


def test_legacy_history_classification(test_db_url, apply_migrations):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_db_url)
    migration = ScriptDirectory.from_config(config).get_revision("0163").module
    cases = {
        name: uuid.uuid4()
        for name in (
            "actor",
            "suspended",
            "deleted",
            "self_deleted",
            "unknown",
            "failed",
            "active",
        )
    }

    async def database_step(step):
        engine = create_async_engine(test_db_url)
        try:
            async with engine.begin() as db:
                if step == "seed":
                    for name, user_id in cases.items():
                        await db.execute(
                            text("""
                            INSERT INTO users (id, email, name, password_hash, role, is_active)
                            VALUES (:id, :email, :name, 'unused', 'annotator', :active)
                        """),
                            {
                                "id": user_id,
                                "email": f"migration-{user_id}@test.local",
                                "name": name,
                                "active": name in {"actor", "active"},
                            },
                        )
                    for name, action, status_code in (
                        ("suspended", "user.deactivate", 200),
                        ("deleted", "user.delete", 200),
                        ("deleted", "user.deactivate", 200),
                        ("self_deleted", "user.deactivation_approve", 200),
                        ("failed", "user.deactivate", 403),
                        ("active", "user.deactivate", 200),
                    ):
                        await db.execute(
                            text("""
                            INSERT INTO audit_logs
                                (actor_id, action, target_type, target_id, status_code)
                            VALUES (:actor, :action, 'user', :target, :status)
                        """),
                            {
                                "actor": cases["actor"],
                                "action": action,
                                "target": str(cases[name]),
                                "status": status_code,
                            },
                        )
                elif step == "verify":
                    # Exercise the historical classifier without traversing
                    # irreversible 0174 or persisting changes to unrelated rows.
                    await db.run_sync(_replay, migration, "downgrade")
                    await db.run_sync(_replay, migration, "upgrade")
                    for name, expected in (
                        ("suspended", "suspended"),
                        ("deleted", "deleted"),
                        ("self_deleted", "deleted"),
                        ("unknown", "historical_unknown"),
                        ("failed", "historical_unknown"),
                        ("active", None),
                    ):
                        row = (
                            await db.execute(
                                text("""
                            SELECT disabled_kind, disabled_at, disabled_by
                            FROM users WHERE id = :id
                        """),
                                {"id": cases[name]},
                            )
                        ).one()
                        assert row.disabled_kind == expected, name
                        if expected in {"suspended", "deleted"}:
                            assert row.disabled_at is not None
                            assert row.disabled_by == cases["actor"]
                        else:
                            assert row.disabled_at is None
                            assert row.disabled_by is None
                    await db.rollback()
                else:
                    await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
                    await db.execute(
                        text("DELETE FROM audit_logs WHERE actor_id = :actor"),
                        {"actor": cases["actor"]},
                    )
                    await db.execute(
                        text("DELETE FROM users WHERE id = ANY(:ids)"),
                        {"ids": list(cases.values())},
                    )
        finally:
            await engine.dispose()

    try:
        asyncio.run(database_step("seed"))
        asyncio.run(database_step("verify"))
    finally:
        asyncio.run(database_step("cleanup"))
