"""Committed legacy upgrade, bounded physical reads and reviewed CLI plans."""

import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid

import httpx
from PIL import Image
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.project import Project
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.user import User
from app.services import legacy_asset_verification as service
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.reference_keys import reference_key
from app.services.storage import storage_service
from tests.factory import create_project
from tests.test_dataset_asset_backfill import backfill
from tests.test_managed_original_reader import Provider, error

spec = importlib.util.spec_from_file_location(
    "legacy_verification_cli",
    Path(__file__).parents[1] / "scripts/verify_legacy_dataset_assets.py",
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
async def legacy(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    identifier = uuid.uuid4().hex
    stream = io.BytesIO()
    Image.new("RGB", (19, 11), (20, 80, 120)).save(stream, format="PNG")
    raw = stream.getvalue()
    provider = Provider()
    provider.raw = provider.actual = raw
    provider.meta.endpoint_url = storage_service.client.meta.endpoint_url
    monkeypatch.setattr(service.reader, "create_client", lambda **_: provider)
    async with factory() as db:
        purposes = set(await db.scalars(select(StorageBinding.purpose)))
        had_location = await db.get(StorageLocation, LEGACY_LOCATION_ID) is not None
        operations = set(await db.scalars(select(StorageOperation.id)))
        user = User(
            id=uuid.uuid4(),
            email=f"legacy-{identifier}@example.test",
            name="Legacy upgrade",
            password_hash="unused",
            role="super_admin",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        project = await create_project(db, owner_id=user.id)
        dataset = Dataset(
            display_id="old-" + identifier[:12],
            name="Renamed legacy dataset",
            created_by=user.id,
            file_count=1,
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="原图.png",
            file_path=f"historical-{identifier}/旧名称/原图 %25.png",
            file_type="image",
            file_size=len(raw),
            width=19,
            height=11,
            content_hash="a" * 32,
        )
        db.add(item)
        await db.flush()
        task = Task(
            display_id="old-" + identifier[:12],
            project_id=project.id,
            dataset_item_id=item.id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            status="in_progress",
        )
        db.add_all([task, ProjectDataset(project_id=project.id, dataset_id=dataset.id)])
        await db.flush()
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=user.id,
                class_name="car",
                geometry={"type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4},
            )
        )
        frozen = TrainingDatasetVersion(
            project_id=project.id,
            creator_id=user.id,
            request_id=uuid.uuid4(),
            request_digest="b" * 64,
            name="Historical version",
            status="ready",
            snapshot={
                "tasks": [str(task.id)],
                "item_id": str(item.id),
                "legacy_path": item.file_path,
            },
            snapshot_digest="c" * 64,
            manifest={"historical": True},
        )
        db.add(frozen)
        await ensure_legacy_bindings(db)
        await db.commit()
    world = SimpleNamespace(
        factory=factory,
        user=user,
        project=project,
        dataset=dataset,
        item=item,
        task=task,
        provider=provider,
        raw=raw,
        sha=hashlib.sha256(raw).hexdigest(),
        initial_operations=operations,
    )
    try:
        yield world
    finally:
        async with factory() as db:
            asset_ids = list(
                await db.scalars(
                    select(DatasetAsset.id).where(
                        DatasetAsset.dataset_item_id.in_(
                            select(DatasetItem.id).where(
                                DatasetItem.dataset_id == dataset.id
                            )
                        )
                    )
                )
            )
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                delete(AuditLog).where(
                    (AuditLog.actor_id == user.id)
                    | AuditLog.target_id.in_([str(x) for x in asset_ids])
                )
            )
            await db.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.project_id == project.id
                )
            )
            await db.execute(
                delete(Annotation).where(Annotation.project_id == project.id)
            )
            await db.execute(delete(Task).where(Task.project_id == project.id))
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset.id))
            await db.execute(delete(User).where(User.id == user.id))
            await db.execute(
                delete(StorageOperation).where(
                    StorageOperation.id.notin_(operations),
                    StorageOperation.purpose == "legacy_asset_verification",
                )
            )
            await db.execute(
                delete(StorageBinding).where(
                    StorageBinding.purpose.notin_(purposes),
                    StorageBinding.location_id == LEGACY_LOCATION_ID,
                )
            )
            if not had_location:
                await db.execute(
                    delete(Gate).where(Gate.location_id == LEGACY_LOCATION_ID)
                )
                await db.execute(
                    delete(StorageLocation).where(
                        StorageLocation.id == LEGACY_LOCATION_ID
                    )
                )
            await db.commit()
        assert all(body.closed for body in provider.bodies)


async def map_legacy(world):
    args = backfill.parser().parse_args(
        ["--dataset-id", str(world.dataset.id), "--apply", "--batch-size", "1"]
    )
    assert await backfill.run(args, world.factory, lambda _: None) == 0


async def business(world):
    """Compare full stored rows, including historical JSON and updated_at."""
    filters = (
        (Dataset, Dataset.id == world.dataset.id),
        (DatasetItem, DatasetItem.dataset_id == world.dataset.id),
        (ProjectDataset, ProjectDataset.dataset_id == world.dataset.id),
        (Task, Task.project_id == world.project.id),
        (Annotation, Annotation.project_id == world.project.id),
        (TrainingDatasetVersion, TrainingDatasetVersion.project_id == world.project.id),
    )
    async with world.factory() as db:
        return {
            model.__tablename__: list(
                (
                    await db.execute(
                        select(model.__table__).where(condition).order_by(model.id)
                    )
                ).mappings()
            )
            for model, condition in filters
        }


async def identity(world):
    async with world.factory() as db:
        asset = await db.scalar(
            select(DatasetAsset).where(DatasetAsset.dataset_item_id == world.item.id)
        )
        return (
            None
            if asset is None
            else (
                asset.id,
                asset.verification_state,
                asset.checksum_value,
                asset.size_bytes,
            )
        )


async def preview(world):
    result = await service.verify(world.factory, world.item.id)
    return service.Preview.parse(
        {key: value for key, value in result.items() if key != "status"}
    )


async def test_formal_backfill_preview_apply_preserves_all_business_rows(legacy):
    before = await business(legacy)
    await map_legacy(legacy)
    unverified = await identity(legacy)
    observed = await preview(legacy)
    assert await identity(legacy) == unverified
    assert observed.sha256 == legacy.sha and observed.size_bytes == len(legacy.raw)
    assert (await service.verify(legacy.factory, legacy.item.id, expected=observed))[
        "status"
    ] == "verified"
    assert await identity(legacy) == (
        unverified[0],
        "verified",
        legacy.sha,
        len(legacy.raw),
    )
    calls = list(legacy.provider.calls)
    assert (await service.verify(legacy.factory, legacy.item.id, expected=observed))[
        "status"
    ] == "already_verified"
    assert legacy.provider.calls == calls
    assert await business(legacy) == before
    async with legacy.factory() as db:
        replica = await db.scalar(
            select(DatasetAssetLocation).where(
                DatasetAssetLocation.asset_id == unverified[0]
            )
        )
        assert replica.reference_state == "present" and replica.verified_at
        assert (
            replica.object_key == legacy.item.file_path and replica.creation_id is None
        )
        logs = list(
            await db.scalars(
                select(AuditLog).where(AuditLog.target_id == str(unverified[0]))
            )
        )
        assert len(logs) == 1
        assert logs[0].detail_json == {
            "purpose": "legacy_current_baseline",
            **observed.public(),
        }
        assert (
            set(await db.scalars(select(StorageOperation.id)))
            == legacy.initial_operations
        )
    text_result = json.dumps(observed.public())
    assert (
        legacy.item.file_path not in text_result
        and legacy.provider.meta.endpoint_url not in text_result
    )


async def test_changed_current_bytes_never_publish_reviewed_identity(legacy):
    await map_legacy(legacy)
    observed = await preview(legacy)
    legacy.provider.raw = legacy.provider.actual = b"x" * len(legacy.raw)
    with pytest.raises(ContentError, match="legacy_verification_content_changed"):
        await service.verify(legacy.factory, legacy.item.id, expected=observed)
    assert (await identity(legacy))[1:3] == ("unverified", None)


@pytest.mark.parametrize(
    "mutation", ["item", "lifecycle", "location", "binding", "maintenance"]
)
async def test_reviewed_plan_rejects_changed_database_facts_before_io(legacy, mutation):
    await map_legacy(legacy)
    observed = await preview(legacy)
    async with legacy.factory() as db:
        if mutation == "item":
            await db.execute(
                update(DatasetItem)
                .where(DatasetItem.id == legacy.item.id)
                .values(width=20)
            )
        elif mutation == "lifecycle":
            await db.execute(
                update(Dataset)
                .where(Dataset.id == legacy.dataset.id)
                .values(lifecycle_revision=2)
            )
        elif mutation == "location":
            await db.execute(
                update(StorageLocation)
                .where(StorageLocation.id == LEGACY_LOCATION_ID)
                .values(enabled=False)
            )
        elif mutation == "binding":
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "datasets")
                .values(bucket="wrong-target")
            )
        else:
            await db.execute(
                update(Gate)
                .where(Gate.location_id == LEGACY_LOCATION_ID)
                .values(revision=2)
            )
        await db.commit()
    calls = list(legacy.provider.calls)
    try:
        with pytest.raises(
            ContentError,
            match="legacy_verification_(preview_stale|configuration_changed)",
        ):
            await service.verify(legacy.factory, legacy.item.id, expected=observed)
        assert legacy.provider.calls == calls
    finally:
        async with legacy.factory() as db:
            await db.execute(
                update(StorageLocation)
                .where(StorageLocation.id == LEGACY_LOCATION_ID)
                .values(enabled=True)
            )
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "datasets")
                .values(bucket=storage_service.datasets_bucket)
            )
            await db.execute(
                update(Gate)
                .where(Gate.location_id == LEGACY_LOCATION_ID)
                .values(revision=1)
            )
            await db.commit()


async def test_apply_rechecks_after_io_and_does_not_hold_business_lock(
    legacy, monkeypatch
):
    await map_legacy(legacy)
    observed = await preview(legacy)
    original = service.observe

    def changed(*args):
        result = original(*args)

        async def mutate():
            async with legacy.factory() as db:
                await db.execute(text("SET LOCAL lock_timeout = '1s'"))
                await db.execute(
                    update(DatasetItem)
                    .where(DatasetItem.id == legacy.item.id)
                    .values(height=12)
                )
                await db.commit()

        # A separate event loop/engine is unnecessary: schedule on the test loop.
        asyncio.run_coroutine_threadsafe(mutate(), loop).result(3)
        return result

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(service, "observe", changed)
    with pytest.raises(ContentError, match="legacy_verification_preview_stale"):
        await service.verify(legacy.factory, legacy.item.id, expected=observed)
    assert (await identity(legacy))[1] == "unverified"


async def test_competing_item_lock_returns_busy_and_same_plan_can_retry(legacy):
    await map_legacy(legacy)
    observed = await preview(legacy)
    async with legacy.factory() as blocker:
        await blocker.scalar(
            select(DatasetItem)
            .where(DatasetItem.id == legacy.item.id)
            .with_for_update()
        )
        with pytest.raises(ContentError, match="legacy_verification_busy"):
            await asyncio.wait_for(
                service.verify(legacy.factory, legacy.item.id, expected=observed), 5
            )
        await blocker.rollback()
    assert (await service.verify(legacy.factory, legacy.item.id, expected=observed))[
        "status"
    ] == "verified"


@pytest.mark.parametrize("kind", ["missing", "reference", "size"])
async def test_unsupported_or_changed_legacy_records_do_not_acquire_identity(
    legacy, kind
):
    if kind != "missing":
        await map_legacy(legacy)
    if kind == "reference":
        async with legacy.factory() as db:
            await db.execute(
                update(DatasetItem)
                .where(DatasetItem.id == legacy.item.id)
                .values(file_path=reference_key(uuid.uuid4(), "f" * 64))
            )
            await db.commit()
    if kind == "size":
        legacy.provider.raw = legacy.provider.actual = legacy.raw + b"extra"
    with pytest.raises(
        ContentError,
        match={
            "missing": "mapping_required",
            "reference": "not_legacy_managed",
            "size": "size_changed",
        }[kind],
    ):
        await preview(legacy)
    if kind != "size":
        assert not legacy.provider.calls


@pytest.mark.parametrize(
    "failure",
    ["missing", "conditions", "truncated", "changed_on_close", "unknown_close"],
)
async def test_physical_failures_never_publish_and_close_owned_handles(legacy, failure):
    await map_legacy(legacy)
    if failure == "missing":
        legacy.provider.head_error = error("NoSuchKey", 404)
    elif failure == "conditions":
        legacy.provider.ignore_conditions = True
    elif failure == "truncated":
        legacy.provider.actual = legacy.raw[:-1]
    elif failure == "changed_on_close":
        legacy.provider.body_close_hook = lambda: setattr(
            legacy.provider, "etag", '"new"'
        )
    else:
        legacy.provider.body_close_error = OSError("private failure")
    with pytest.raises(ContentError) as caught:
        await preview(legacy)
    assert legacy.provider.closed == 1
    assert (await identity(legacy))[1] == "unverified"
    async with legacy.factory() as db:
        operations = list(
            await db.scalars(
                select(StorageOperation).where(
                    StorageOperation.purpose == "legacy_asset_verification"
                )
            )
        )
        assert len(operations) == (1 if failure == "unknown_close" else 0)
    assert "private" not in caught.value.code


async def test_cancellation_waits_for_actual_stream_close_without_publishing(legacy):
    await map_legacy(legacy)
    started, release = threading.Event(), threading.Event()

    def hold(_body):
        started.set()
        assert release.wait(5)

    legacy.provider.read_hook = hold
    task = asyncio.create_task(service.verify(legacy.factory, legacy.item.id))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        async with legacy.factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(StorageOperation)
                    .where(
                        StorageOperation.purpose == "legacy_asset_verification",
                        StorageOperation.id.notin_(legacy.initial_operations),
                    )
                )
                == 1
            )
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(body.closed for body in legacy.provider.bodies)
    assert (await identity(legacy))[1] == "unverified"


async def test_cli_plan_is_bounded_private_and_retryable(legacy, tmp_path):
    await map_legacy(legacy)
    path = tmp_path / "review.jsonl"
    events = []
    args = cli.parser().parse_args(
        [
            "--preview",
            "--dataset-id",
            str(legacy.dataset.id),
            "--output",
            str(path),
            "--batch-size",
            "1",
        ]
    )
    assert await cli.run(args, legacy.factory, events.append) == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert events[-1]["previewed"] == 1
    assert (await identity(legacy))[1] == "unverified"
    args = cli.parser().parse_args(["--apply-plan", str(path)])
    assert await cli.run(args, legacy.factory, events.append) == 0
    assert events[-1]["verified"] == 1
    assert await cli.run(args, legacy.factory, events.append) == 0
    assert events[-1]["already_verified"] == 1
    # Bulk preview skips verified entries rather than reinterpreting them.
    args = cli.parser().parse_args(
        [
            "--preview",
            "--dataset-id",
            str(legacy.dataset.id),
            "--output",
            str(tmp_path / "empty.jsonl"),
        ]
    )
    assert await cli.run(args, legacy.factory, events.append) == 0
    assert events[-1]["previewed"] == 0


@pytest.mark.parametrize(
    "change", ["incomplete", "digest", "trailing", "invalid_entry"]
)
async def test_cli_validates_entire_plan_before_any_publication(
    legacy, tmp_path, monkeypatch, change
):
    await map_legacy(legacy)
    observed = await preview(legacy)
    header = cli.line({"type": "plan", "schema": 1})
    entry = cli.line({"type": "asset", "preview": observed.public()})
    footer = cli.line(
        {
            "type": "complete",
            "count": 1,
            "failed": 0,
            "sha256": hashlib.sha256(header + entry).hexdigest(),
        }
    )
    raw = header + entry + footer
    if change == "incomplete":
        raw = header + entry
    elif change == "digest":
        raw = raw.replace(observed.sha256.encode(), b"f" * 64)
    elif change == "trailing":
        raw += entry
    else:
        raw = header + entry + cli.line({"type": "asset", "preview": {}}) + footer
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(raw)

    async def forbidden(*_, **__):
        pytest.fail("Plan validation must finish before executing any entry")

    monkeypatch.setattr(service, "verify", forbidden)
    with pytest.raises((ValueError, ContentError)):
        await cli.run(
            cli.parser().parse_args(["--apply-plan", str(path)]),
            legacy.factory,
            lambda _: None,
        )
    assert (await identity(legacy))[1] == "unverified"


async def test_cli_requires_existing_target_and_never_overwrites_plan(legacy, tmp_path):
    path = tmp_path / "plan.jsonl"
    with pytest.raises(ValueError, match="explicit target"):
        await cli.run(
            cli.parser().parse_args(["--preview", "--output", str(path)]),
            legacy.factory,
            lambda _: None,
        )
    with pytest.raises(ValueError, match="dataset does not exist"):
        await cli.run(
            cli.parser().parse_args(
                ["--preview", "--dataset-id", str(uuid.uuid4()), "--output", str(path)]
            ),
            legacy.factory,
            lambda _: None,
        )
    assert not path.exists()
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        await cli.run(
            cli.parser().parse_args(
                ["--preview", "--all-datasets", "--output", str(path)]
            ),
            legacy.factory,
            lambda _: None,
        )
    assert path.read_bytes() == b"keep"


async def test_real_minio_upgrade_restores_existing_task_and_item_downloads(
    legacy, app_module, monkeypatch, tmp_path
):
    if os.environ.get("AAP_WORKTREE_MODE") != "test":
        pytest.skip("Live object writes require an owned worktree test environment")
    # Restore the real guarded constructor only for this exact owned object.
    monkeypatch.undo()
    key, bucket = legacy.item.file_path, storage_service.datasets_bucket
    assert bucket.startswith("aap-wt-") and "-test-" in bucket
    client = storage_service.client
    client.put_object(Bucket=bucket, Key=key, Body=legacy.raw)
    from app.deps import get_db

    async def database():
        async with legacy.factory() as db:
            yield db

    app_module.dependency_overrides[get_db] = database
    headers = {
        "Authorization": "Bearer "
        + create_access_token(str(legacy.user.id), legacy.user.role)
    }
    before = await business(legacy)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://test",
            headers=headers,
        ) as http:
            task_url = f"/api/v1/files/tasks/{legacy.task.id}/original-identity"
            assert (await http.get(task_url)).json()["detail"][
                "code"
            ] == "content_unverified"
            await map_legacy(legacy)
            assert (await http.get(task_url)).json()["detail"][
                "code"
            ] == "content_unverified"
            plan = tmp_path / "real-plan.jsonl"
            for argv in (
                [
                    "--preview",
                    "--dataset-id",
                    str(legacy.dataset.id),
                    "--output",
                    str(plan),
                ],
                ["--apply-plan", str(plan)],
            ):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(cli.__file__)),
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                assert process.returncode == 0, (stdout, stderr)
                assert json.loads(stdout.splitlines()[-1])["failed"] == 0
            for url in (
                task_url,
                f"/api/v1/datasets/{legacy.dataset.id}/items/{legacy.item.id}/original-identity",
            ):
                response = await http.get(url)
                assert response.status_code == 200, response.text
                info = response.json()
                assert info["sha256"] == legacy.sha
                downloaded = await http.get(info["download_path"])
                assert downloaded.status_code == 200, downloaded.text
                assert downloaded.content == legacy.raw
            assert await business(legacy) == before
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            try:
                assert body.read() == legacy.raw
            finally:
                body.close()
    finally:
        app_module.dependency_overrides.pop(get_db, None)
        client.delete_object(Bucket=bucket, Key=key)


def test_preview_rejects_forged_checksum_or_boolean_size():
    value = dict(
        item_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        fingerprint="a" * 64,
        sha256="b" * 64,
        size_bytes=0,
    )
    preview = service.Preview(**value)
    for changes in ({"sha256": "old-md5"}, {"size_bytes": True}):
        with pytest.raises(ContentError, match="invalid_preview"):
            replace(preview, **changes)
