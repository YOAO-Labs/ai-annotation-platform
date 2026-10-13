"""Run the assignment backfill against real inherited and task-specific rows."""

import asyncio
import uuid

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from tests.factory import create_project, create_user
from tests.test_migration_0173_project_role_preparation import _replay


def test_assignment_override_backfill_and_roundtrip(test_db_url, apply_migrations):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_db_url)
    migration = ScriptDirectory.from_config(config).get_revision("0171").module
    saved = {}
    expected = {
        "inherited": (False, False),
        "overridden": (True, True),
        "mixed": (True, False),
        "unassigned": (False, False),
        "unbatched": (True, True),
    }

    async def step(action):
        engine = create_async_engine(test_db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                if action == "seed":
                    people = [
                        await create_user(
                            db, "annotator", f"override-{uuid.uuid4()}@test.local", name
                        )
                        for name in ("Default", "Selected")
                    ]
                    saved["users"] = [person.id for person in people]
                    project = await create_project(db, owner_id=people[0].id)
                    saved["project"] = project.id
                    batch = TaskBatch(
                        project_id=project.id,
                        display_id=f"B-MIG-OVR-{uuid.uuid4().hex[:8]}",
                        name="Assignment migration",
                        annotator_id=people[0].id,
                        reviewer_id=people[0].id,
                    )
                    db.add(batch)
                    await db.flush()
                    for name, (assignee, reviewer) in {
                        "inherited": (people[0].id, people[0].id),
                        "overridden": (people[1].id, people[1].id),
                        "mixed": (people[1].id, people[0].id),
                        "unassigned": (None, None),
                        "unbatched": (people[0].id, people[0].id),
                    }.items():
                        db.add(
                            Task(
                                project_id=project.id,
                                batch_id=None if name == "unbatched" else batch.id,
                                display_id=f"T-MIG-OVR-{uuid.uuid4().hex[:8]}",
                                file_name=name,
                                file_path=name,
                                assignee_id=assignee,
                                reviewer_id=reviewer,
                            )
                        )
                elif action == "verify":
                    # Replay only the targeted revision in a rollback-only
                    # transaction, leaving irreversible 0174 and its data intact.
                    connection = await db.connection()
                    await connection.run_sync(_replay, migration, "downgrade")
                    await connection.run_sync(_replay, migration, "upgrade")
                    rows = await db.scalars(
                        select(Task).where(Task.project_id == saved["project"])
                    )
                    assert {
                        row.file_name: (
                            row.assignee_is_override,
                            row.reviewer_is_override,
                        )
                        for row in rows
                    } == expected
                    await db.rollback()
                    return
                else:
                    if "project" in saved:
                        await db.execute(
                            delete(Task).where(Task.project_id == saved["project"])
                        )
                        await db.execute(
                            delete(TaskBatch).where(
                                TaskBatch.project_id == saved["project"]
                            )
                        )
                        await db.execute(
                            delete(Project).where(Project.id == saved["project"])
                        )
                    if "users" in saved:
                        await db.execute(
                            delete(User).where(User.id.in_(saved["users"]))
                        )
                await db.commit()
        finally:
            await engine.dispose()

    try:
        asyncio.run(step("seed"))
        asyncio.run(step("verify"))
    finally:
        asyncio.run(step("cleanup"))
