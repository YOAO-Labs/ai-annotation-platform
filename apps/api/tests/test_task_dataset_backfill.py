"""Resumable metadata-only backfill and database-enforced read-only modes."""

from argparse import Namespace
import copy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.db.models.project import Project
from app.db.models.project_task_dataset import (
    ProjectTaskDataset,
    TaskDatasetVersionMembership,
)
from app.services.project_task_dataset import ensure_membership
from scripts.backfill_task_datasets import (
    backfill_batch,
    parser,
    run,
    verify_high_water,
)
from scripts import backfill_task_datasets as command
from tests.factory import create_project
from tests.test_project_task_datasets import add_version
from tests.test_project_task_datasets import (
    committed_stream_project as committed_stream_project,
)


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--all-projects"],
        ["--dry-run"],
        ["--all-projects", "--apply", "--verify"],
        [
            "--all-projects",
            "--dry-run",
            "--project-id",
            "00000000-0000-0000-0000-000000000001",
        ],
    ],
)
def test_command_requires_one_target_and_one_mode(arguments):
    with pytest.raises(SystemExit):
        parser().parse_args(arguments)


async def test_backfill_batches_preserve_identities_append_late_history_and_resume(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    now = datetime.now(timezone.utc)
    versions = [
        await add_version(
            db_session, project, created_at=now + timedelta(seconds=index)
        )
        for index in range(4)
    ]
    invalid = await add_version(
        db_session,
        project,
        snapshot={"schema": 1, "task": "unknown", "classes": {"corrupt": True}},
        created_at=now + timedelta(seconds=5),
    )
    saved = {
        version.id: (
            copy.deepcopy(version.snapshot),
            copy.deepcopy(version.manifest),
            version.snapshot_digest,
            version.package_sha256,
        )
        for version in versions + [invalid]
    }
    dry, _ = await backfill_batch(db_session, project.id, mode="dry-run", batch_size=2)
    assert dry["pending"] == 2 and dry["associated"] == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProjectTaskDataset)
            .where(ProjectTaskDataset.project_id == project.id)
        )
        == 0
    )
    first, _ = await backfill_batch(db_session, project.id, mode="apply", batch_size=2)
    assert first["associated"] == 2
    # Restart without a saved cursor; previously issued numbers do not change.
    resumed, cursor = await backfill_batch(
        db_session, project.id, mode="apply", batch_size=3
    )
    assert resumed["already_associated"] == 2 and resumed["associated"] == 1
    last, _ = await backfill_batch(
        db_session, project.id, mode="apply", batch_size=3, after=cursor
    )
    assert last["associated"] == 1 and last["unrecognized"] == 1
    for ordinal, version in enumerate(versions, 1):
        assert (
            await db_session.get(TaskDatasetVersionMembership, version.id)
        ).ordinal == ordinal
    older = await add_version(db_session, project, created_at=now - timedelta(days=30))
    membership = await ensure_membership(db_session, older)
    assert membership.ordinal == 5
    await db_session.refresh(older)
    assert older.created_at < now
    assert await verify_high_water(db_session, project.id) == []
    for version in versions + [invalid]:
        await db_session.refresh(
            version,
            attribute_names=[
                "snapshot",
                "manifest",
                "snapshot_digest",
                "package_sha256",
            ],
        )
        assert (
            version.snapshot,
            version.manifest,
            version.snapshot_digest,
            version.package_sha256,
        ) == saved[version.id]
    assert await db_session.get(TaskDatasetVersionMembership, invalid.id) is None
    verify, _ = await backfill_batch(
        db_session, project.id, mode="verify", batch_size=100
    )
    assert (
        verify["pending"] == 0
        and verify["already_associated"] == 5
        and verify["unrecognized"] == 1
    )


async def test_verify_reports_high_water_corruption_without_repair(
    db_session, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    version = await add_version(db_session, project)
    membership = await ensure_membership(db_session, version)
    stream = await db_session.get(ProjectTaskDataset, membership.task_dataset_id)
    stream.next_ordinal = 1
    await db_session.flush()
    findings = await verify_high_water(db_session, project.id)
    assert findings[0]["code"] == "task_dataset_high_water_invalid"
    await db_session.refresh(stream)
    assert stream.next_ordinal == 1


async def test_command_dry_run_verify_and_apply_are_repeatable(
    committed_stream_project,
):
    factory, project_id = committed_stream_project
    async with factory() as db:
        project = await db.get(Project, project_id)
        version = await add_version(db, project)
        version_id = version.id
        await db.commit()
    events = []
    arguments = Namespace(
        project_id=project_id, all_projects=False, mode="dry-run", batch_size=1
    )
    assert await run(arguments, factory, events.append) == 0
    assert events[-1]["pending"] == 1 and events[-1]["associated"] == 0
    async with factory() as db:
        assert await db.get(TaskDatasetVersionMembership, version_id) is None
    arguments.mode = "verify"
    assert await run(arguments, factory, events.append) == 1
    arguments.mode = "apply"
    assert await run(arguments, factory, events.append) == 0
    assert events[-1]["associated"] == 1
    assert await run(arguments, factory, events.append) == 0
    assert events[-1]["associated"] == 0 and events[-1]["already_associated"] == 1
    arguments.mode = "verify"
    assert await run(arguments, factory, events.append) == 0
    assert events[-1]["pending"] == 0


@pytest.mark.parametrize("mode", ["dry-run", "verify"])
async def test_read_only_modes_reject_an_accidental_database_write(
    committed_stream_project, monkeypatch, mode
):
    factory, project_id = committed_stream_project

    async def accidental_write(db, selected_project, **_kwargs):
        db.add(ProjectTaskDataset(project_id=selected_project, task="detect"))
        await db.flush()
        raise AssertionError("PostgreSQL must reject the write")

    monkeypatch.setattr(command, "backfill_batch", accidental_write)
    args = Namespace(project_id=project_id, all_projects=False, mode=mode, batch_size=1)
    with pytest.raises(DBAPIError) as rejected:
        await run(args, factory, lambda _event: None)
    assert rejected.value.orig.sqlstate == "25006"
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ProjectTaskDataset)
                .where(ProjectTaskDataset.project_id == project_id)
            )
            == 0
        )
