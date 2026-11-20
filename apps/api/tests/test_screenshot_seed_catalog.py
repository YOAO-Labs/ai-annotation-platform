"""Screenshot catalog and reconciliation against the real run-owned seed."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import closing
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.fixture_run import FixtureRun, FixtureRunResource
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services import screenshot_seed_catalog as catalog
from app.services.annotation_evidence import assert_review_evidence_current
from app.services.fixture_run_ownership import seal
from app.services.screenshot_seed_spec import (
    PROJECT_SPECS,
    SEED_REVISION,
    resolve_project_spec,
)
from scripts.seed_screenshot_profile import (
    FIXED_TIME,
    ScreenshotSeedReconcileError,
    finalize_screenshot_seed_timestamps,
    prepare_screenshot_seed,
    reconcile_screenshot_seed,
)

pytest_plugins = ("tests.screenshot_seed_support",)

pytestmark = pytest.mark.asyncio
requires_screenshot_runtime = pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned module-data verifier and screenshot runtime",
)


async def test_screenshot_reader_reuses_origin_only_after_retirement(db_session):
    first, second = uuid.uuid4(), uuid.uuid4()
    origin = f"http://proxy-{first.hex}.invalid:9000"
    db_session.add_all(
        [FixtureRun(id=run, family="screenshot") for run in (first, second)]
    )
    await db_session.flush()
    # The outer test transaction rolls back these passwordless LOGINs and both runs.
    for run in (first, second):
        await db_session.execute(
            text(f"CREATE ROLE aap_fixture_media_read_{run.hex} LOGIN NOINHERIT")
        )

    async def register(run):
        return await db_session.scalar(
            text(
                "SELECT yoao_assets_v1.register_fixture_native_reader("
                ":run, CAST(:role AS name), :origin)"
            ),
            {"run": run, "role": f"aap_fixture_media_read_{run.hex}", "origin": origin},
        )

    async def identity(run):
        return await db_session.scalar(
            text("SELECT yoao_assets_v1.fixture_native_reader_identity(:run)"),
            {"run": run},
        )

    assert await register(first) is True
    original = await identity(first)
    assert await register(second) is False
    assert await identity(second) is None
    await seal(db_session, first, family="screenshot")
    assert await register(second) is False  # Sealing is not reader retirement.
    retired = await db_session.scalar(
        text("SELECT yoao_assets_v1.retire_fixture_native_reader(:run)"), {"run": first}
    )
    assert retired is True

    assert await register(second) is True
    assert await identity(first) == {**original, "state": "retired"}
    replacement = await identity(second)
    assert replacement["proxy_origin"] == original["proxy_origin"] == origin
    assert replacement["name"] == f"aap_fixture_media_read_{second.hex}"
    assert replacement["state"] == "active" and replacement["oid"] != original["oid"]


@pytest.fixture
async def owner_db(screenshot_seed):
    """Keep mutations and internal commits inside an outer rolled-back transaction."""
    engine = screenshot_seed.sessions.kw["bind"]
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            try:
                yield db
            finally:
                await db.close()
                await transaction.rollback()


async def _owned_id(db, seed, kind, key):
    value = await db.scalar(
        select(FixtureRunResource.resource_id).where(
            FixtureRunResource.run_id == seed.run_id,
            FixtureRunResource.kind == kind,
            FixtureRunResource.logical_key == key,
            FixtureRunResource.state == "published",
        )
    )
    assert value is not None
    return value


async def _catalog(db, seed, requirements="none", *, run_id=None):
    selected_run = seed.run_id if run_id is None else run_id
    async with seed.media_gateway(selected_run) as media:
        return await catalog.build_screenshot_seed_catalog(
            db,
            run_id=selected_run,
            backend_requirements=requirements,
            media=media,
        )


@requires_screenshot_runtime
async def test_catalog_returns_real_logical_resources(screenshot_seed, owner_db):
    response = await screenshot_seed.catalog()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(screenshot_seed.run_id)
    assert body["schema_version"] == 1
    assert body["seed_revision"] == SEED_REVISION
    assert set(PROJECT_SPECS).issubset(body["projects"])
    image = body["projects"]["image_demo"]
    assert image["id"] == str(
        await _owned_id(owner_db, screenshot_seed, "project", "image_demo")
    )
    assert image["tasks"]["review"]["status"] == "review"
    assert image["tasks"]["review"]["file_path"].endswith("screenshot_05.jpg")
    image_anchor = image["tasks"]["annotating"]["recording_anchors"]["primary_vehicle"]
    assert image_anchor["label"] == "car"
    pointcloud = body["projects"]["pointcloud_demo"]
    assert set(pointcloud["tasks"]) == {f"frame_{index:03d}" for index in range(39)}
    anchor = pointcloud["tasks"]["frame_000"]["recording_anchors"]["foreground_object"]
    assert anchor["label"] == "object"
    assert set(body["projects"]["pointcloud_multicam_demo"]["tasks"]) == {"frame_000"}
    video = body["projects"]["video_demo"]["tasks"]["tracking"]["recording_anchors"]
    assert video["front_truck_f4"]["frame_index"] == 4


@requires_screenshot_runtime
async def test_private_catalog_protocol_denies_invalid_and_drifted_scope(
    screenshot_seed,
):
    invalid = await screenshot_seed.catalog(backend_requirements="not-a-backend")
    assert invalid.status_code == 422

    async with screenshot_seed.sessions() as db:
        actor_id = await _owned_id(db, screenshot_seed, "user", "annotator")
        actor = await db.get(User, actor_id)
        assert actor is not None and actor.is_active
    failure = None
    try:
        async with screenshot_seed.sessions.begin() as db:
            actor = await db.get(User, actor_id)
            actor.is_active = False
        response = await screenshot_seed.catalog()
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "screenshot_seed_not_ready"
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            async with screenshot_seed.sessions.begin() as db:
                actor = await db.get(User, actor_id)
                actor.is_active = True
        except BaseException as restore_failure:
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "Private catalog request and actor restoration failed",
                    [failure, restore_failure],
                ) from None
            raise
    recovered = await screenshot_seed.catalog()
    assert recovered.status_code == 200, recovered.text


@requires_screenshot_runtime
async def test_catalog_ignores_soft_deleted_annotations(screenshot_seed, owner_db):
    body = await _catalog(owner_db, screenshot_seed)
    project = body["projects"]["pointcloud_demo"]
    task_id = uuid.UUID(project["tasks"]["frame_000"]["id"])
    actor = uuid.UUID(body["users"]["admin"]["id"])
    owner_db.add(
        Annotation(
            id=uuid.uuid4(),
            task_id=task_id,
            project_id=uuid.UUID(project["id"]),
            user_id=actor,
            source="manual",
            annotation_type="box_3d",
            tool_unit_id="lidar_box_3d",
            class_name="object",
            geometry={
                "type": "box_3d",
                "center": [2.0, 0.0, 0.0],
                "size": [1.0, 1.0, 1.0],
                "rotation": [0.0, 0.0, 0.0],
            },
            is_active=False,
            was_cancelled=False,
        )
    )
    await owner_db.flush()
    assert (await _catalog(owner_db, screenshot_seed))["run_id"] == str(
        screenshot_seed.run_id
    )


@requires_screenshot_runtime
async def test_catalog_rejects_incomplete_optional_large_image(
    screenshot_seed, owner_db
):
    body = await _catalog(owner_db, screenshot_seed)
    owner_db.add(
        Project(
            id=uuid.uuid4(),
            display_id="P-LARGE-IMG",
            name="incomplete large image",
            type_label="image",
            type_key="image-seg",
            data_type="image",
            owner_id=uuid.UUID(body["users"]["admin"]["id"]),
        )
    )
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, "none")


@requires_screenshot_runtime
async def test_catalog_rejects_unexpected_task(screenshot_seed, owner_db):
    project_id = await _owned_id(owner_db, screenshot_seed, "project", "ocr_demo")
    owner_db.add(
        Task(
            project_id=project_id,
            display_id="T-UNEXPECTED-" + uuid.uuid4().hex[:10],
            file_name="unexpected.jpg",
            file_path="unexpected.jpg",
            file_type="image",
        )
    )
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, "none")


@requires_screenshot_runtime
async def test_catalog_backend_scope_does_not_hide_fixture_drift(
    screenshot_seed, owner_db
):
    project_id = await _owned_id(owner_db, screenshot_seed, "project", "image_demo")
    project = await owner_db.get(Project, project_id)
    project.ml_backend_pool_id = None
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, None)
    result = await _catalog(owner_db, screenshot_seed, "none")
    assert result["projects"]["image_demo"]["ml_backend"] is None
    with pytest.raises(ValueError):
        await _catalog(owner_db, screenshot_seed, "typo")
    item_id = await _owned_id(owner_db, screenshot_seed, "dataset", "image_demo")
    dataset = await owner_db.get(Dataset, item_id)
    dataset.file_count += 1
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, "none")


@requires_screenshot_runtime
async def test_catalog_fails_closed_for_changed_original(screenshot_seed, owner_db):
    dataset_id = await _owned_id(owner_db, screenshot_seed, "dataset", "image_demo")
    item = await owner_db.scalar(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
    )
    item.file_path = "missing/" + uuid.uuid4().hex + ".jpg"
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, "none")


@requires_screenshot_runtime
async def test_catalog_rejects_multicamera_without_calibration(
    screenshot_seed, owner_db
):
    dataset_id = await _owned_id(
        owner_db, screenshot_seed, "dataset", "pointcloud_multicam_demo"
    )
    item = await owner_db.scalar(
        select(DatasetItem).where(
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.metadata_["storage_relative_path"].astext
            == "camera/back_right/000000.jpg",
        )
    )
    assert item is not None
    item.metadata_ = {
        key: value for key, value in item.metadata_.items() if key != "calibration"
    }
    await owner_db.flush()
    with pytest.raises(catalog.ScreenshotSeedCatalogError):
        await _catalog(owner_db, screenshot_seed, "none")


@requires_screenshot_runtime
async def test_reconcile_is_idempotent_and_preserves_neighbor(screenshot_seed):
    async with screenshot_seed.sessions.begin() as db:
        actor = await _owned_id(db, screenshot_seed, "user", "project_admin")
        neighbor = Project(
            id=uuid.uuid4(),
            display_id="P-KEEP-" + uuid.uuid4().hex[:10],
            name="user-owned project",
            type_label="image",
            type_key="image-det",
            data_type="image",
            owner_id=actor,
        )
        db.add(neighbor)
        await db.flush()
        neighbor_task = Task(
            project_id=neighbor.id,
            display_id="T-KEEP-" + uuid.uuid4().hex[:10],
            file_name="neighbor.jpg",
            file_path="neighbor.jpg",
            status="review",
        )
        db.add(neighbor_task)
        await db.flush()
        neighbor_task.annotation_contributor_ids = None

    try:
        # Media admits the read in another process. Reconcile must commit its
        # root lock, not release a savepoint inside a test-owned transaction.
        async with screenshot_seed.sessions() as writer:
            response = await screenshot_seed.catalog()
            assert response.status_code == 200, response.text
            body = response.json()
            ids = {key: body["projects"][key]["id"] for key in PROJECT_SPECS}
            preparation = await prepare_screenshot_seed(
                writer, run_id=screenshot_seed.run_id
            )
            assert preparation.existing_keys == frozenset(PROJECT_SPECS)
            for _ in range(2):
                report = await reconcile_screenshot_seed(
                    writer,
                    preparation=preparation,
                    asset_sha256=screenshot_seed.digests,
                )
                assert report == {"projects": 5, "tasks": 50, "batches": 5}
                response = await screenshot_seed.catalog()
                assert response.status_code == 200, response.text
                current = response.json()
                assert {key: current["projects"][key]["id"] for key in ids} == ids
                async with screenshot_seed.sessions() as reader:
                    kept_project = await reader.get(Project, neighbor.id)
                    kept_task = await reader.get(Task, neighbor_task.id)
                    assert (
                        kept_project is not None
                        and kept_project.name == "user-owned project"
                    )
                    assert kept_task is not None and kept_task.project_id == neighbor.id
                    review_id = uuid.UUID(
                        current["projects"]["image_demo"]["tasks"]["review"]["id"]
                    )
                    review_task = await reader.get(Task, review_id)
                    with pytest.raises(HTTPException) as self_review:
                        assert_review_evidence_current(
                            review_task, uuid.UUID(current["users"]["annotator"]["id"])
                        )
                    assert self_review.value.status_code == 403
                    assert self_review.value.detail == {"reason": "self_review_denied"}
                    with pytest.raises(HTTPException) as unknown_review:
                        assert_review_evidence_current(kept_task, actor)
                    assert unknown_review.value.status_code == 409
                    assert unknown_review.value.detail == {
                        "reason": "review_contributors_unknown"
                    }
    finally:
        async with screenshot_seed.sessions.begin() as db:
            await db.execute(delete(Task).where(Task.id == neighbor_task.id))
            await db.execute(delete(Project).where(Project.id == neighbor.id))


@requires_screenshot_runtime
async def test_reconcile_refuses_task_id_collision_and_preserves_neighbor(
    screenshot_seed, owner_db
):
    body = await _catalog(owner_db, screenshot_seed)
    image = body["projects"]["image_demo"]
    original = await owner_db.get(Task, uuid.UUID(image["tasks"]["clean"]["id"]))
    expected_display = original.display_id
    original.display_id = "T-TEMP-" + uuid.uuid4().hex[:12]
    neighbor = Project(
        id=uuid.uuid4(),
        display_id="P-KEEP-" + uuid.uuid4().hex[:10],
        name="neighbor",
        type_label="image",
        type_key="image-det",
        data_type="image",
        owner_id=uuid.UUID(body["users"]["project_admin"]["id"]),
    )
    owner_db.add(neighbor)
    await owner_db.flush()
    other = Task(
        project_id=neighbor.id,
        display_id=expected_display,
        file_name="user.jpg",
        file_path="user/user.jpg",
    )
    owner_db.add(other)
    await owner_db.flush()
    preparation = await prepare_screenshot_seed(owner_db, run_id=screenshot_seed.run_id)
    with pytest.raises(ScreenshotSeedReconcileError):
        await reconcile_screenshot_seed(
            owner_db, preparation=preparation, asset_sha256=screenshot_seed.digests
        )
    await owner_db.refresh(other)
    assert other.display_id == expected_display
    assert other.file_path == "user/user.jpg"


@requires_screenshot_runtime
async def test_reconcile_preserves_prediction_metadata(screenshot_seed, owner_db):
    body = await _catalog(owner_db, screenshot_seed)
    task_id = uuid.UUID(body["projects"]["image_demo"]["tasks"]["predicted"]["id"])
    prediction = await owner_db.scalar(
        select(Prediction).where(Prediction.task_id == task_id)
    )
    assert prediction is not None
    prediction_id, result = prediction.id, prediction.result
    metadata = await owner_db.scalar(
        select(PredictionMeta).where(
            PredictionMeta.prediction_id == prediction.id,
            PredictionMeta.prediction_created_at == prediction.created_at,
        )
    )
    if metadata is not None:
        metadata.prediction_created_at = None
        await owner_db.flush()
    prediction.created_at = datetime.now(UTC)
    await owner_db.flush()
    if metadata is None:
        metadata = PredictionMeta(prediction_id=prediction.id)
        owner_db.add(metadata)
    metadata.prediction_created_at = prediction.created_at
    metadata.inference_time_ms = 123
    metadata.extra = {"import_format": "yolo"}
    await owner_db.flush()
    preparation = await prepare_screenshot_seed(owner_db, run_id=screenshot_seed.run_id)
    for _ in range(2):
        await reconcile_screenshot_seed(
            owner_db, preparation=preparation, asset_sha256=screenshot_seed.digests
        )
        await owner_db.refresh(prediction)
        await owner_db.refresh(metadata)
        assert prediction.id == prediction_id
        assert prediction.result == result
        assert metadata.prediction_id == prediction_id
        assert prediction.created_at == metadata.prediction_created_at == FIXED_TIME
        assert metadata.inference_time_ms == 123
        assert metadata.extra == {"import_format": "yolo"}


@requires_screenshot_runtime
async def test_final_timestamps_preserve_later_seed_updates(screenshot_seed, owner_db):
    body = await _catalog(owner_db, screenshot_seed)
    project = await owner_db.get(
        Project, uuid.UUID(body["projects"]["image_demo"]["id"])
    )
    task = await owner_db.get(
        Task, uuid.UUID(body["projects"]["image_demo"]["tasks"]["completed"]["id"])
    )
    dataset_id = await _owned_id(owner_db, screenshot_seed, "dataset", "image_demo")
    dataset = await owner_db.get(Dataset, dataset_id)
    project.preannotate_pipeline = [{"stage": 0, "model_id": "fixture"}]
    dataset.description = "updated by a later seed stage"
    task.skip_reason = "updated by a later seed stage"
    await owner_db.flush()
    await finalize_screenshot_seed_timestamps(
        owner_db, run_id=screenshot_seed.run_id, asset_sha256=screenshot_seed.digests
    )
    for resource in (project, dataset, task):
        await owner_db.refresh(resource)
    assert project.updated_at == dataset.updated_at == FIXED_TIME
    assert task.updated_at == FIXED_TIME + timedelta(hours=6)
    assert project.preannotate_pipeline[0]["model_id"] == "fixture"
    assert dataset.description == "updated by a later seed stage"
    assert task.skip_reason == "updated by a later seed stage"


@requires_screenshot_runtime
async def test_nuscenes_storage_resolves_only_its_dataset(screenshot_seed, owner_db):
    dataset_id = await _owned_id(
        owner_db, screenshot_seed, "dataset", "pointcloud_demo"
    )
    dataset = await owner_db.get(Dataset, dataset_id)
    spec = PROJECT_SPECS["pointcloud_demo"]
    resolved = resolve_project_spec(spec, dataset.id, dataset.metadata_)
    assert resolved.tasks[0].file_path != spec.tasks[0].file_path
    with pytest.raises(ValueError):
        resolve_project_spec(spec, uuid.uuid4(), dataset.metadata_)
    body = await _catalog(owner_db, screenshot_seed)
    frame = body["projects"]["pointcloud_demo"]["tasks"]["frame_000"]
    task = await owner_db.get(Task, uuid.UUID(frame["id"]))
    item = await owner_db.get(DatasetItem, task.dataset_item_id)
    assert item.dataset_id == dataset_id
    assert frame["file_path"] == task.file_path == item.file_path


@requires_screenshot_runtime
async def test_screenshot_retirement_preserves_another_owned_run(screenshot_seed):
    from tests.seed_owner_support import _fixture_versions, fixture_original_reader

    def read_original_fingerprints(rows):
        result = {}
        with closing(fixture_original_reader()) as reader:
            for row in rows:
                key = (row["bucket"], row["object_key"], row["version_id"])
                response = reader.get_object(
                    Bucket=key[0], Key=key[1], VersionId=key[2]
                )
                assert response["VersionId"] == key[2]
                digest = hashlib.sha256()
                size = 0
                with closing(response["Body"]) as body:
                    while chunk := body.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                assert (digest.hexdigest(), size) == (row["sha256"], row["size_bytes"])
                result[key] = (digest.hexdigest(), size)
        return result

    async def catalog_of(run_id):
        response = await screenshot_seed.catalog(run_id=run_id)
        assert response.status_code == 200, response.text
        return response.json()

    second = uuid.uuid4()
    owner = screenshot_seed.owner
    await owner.prepare_suite(second, "screenshot")
    failure = None
    try:
        await screenshot_seed.seed_run(second)
        first = await catalog_of(screenshot_seed.run_id)
        before = await catalog_of(second)
        assert {user["id"] for user in first["users"].values()}.isdisjoint(
            user["id"] for user in before["users"].values()
        )
        engine = screenshot_seed.sessions.kw["bind"]
        second_scope = SimpleNamespace(run_id=second)
        versions_before = await _fixture_versions(engine, second_scope)
        fingerprints_before = await asyncio.to_thread(
            read_original_fingerprints, versions_before
        )
        response = await owner.post(
            "/api/v1/__test/seed/screenshot-cleanup",
            json={"run_id": str(screenshot_seed.run_id)},
        )
        assert response.status_code == 200, response.text
        versions_after = await _fixture_versions(engine, second_scope)
        assert {row["id"]: dict(row) for row in versions_after} == {
            row["id"]: dict(row) for row in versions_before
        }
        assert (
            await asyncio.to_thread(read_original_fingerprints, versions_after)
            == fingerprints_before
        )
        after = await catalog_of(second)
        assert after["run_id"] == str(second)
        assert after["users"] == before["users"]
        assert after["projects"] == before["projects"]
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            response = await owner.post(
                "/api/v1/__test/seed/screenshot-cleanup",
                json={"run_id": str(second)},
            )
            assert response.status_code == 200, response.text
        except BaseException as cleanup_failure:
            if failure is not None:
                raise builtins.BaseExceptionGroup(
                    "Neighbor screenshot run and exact cleanup failed",
                    [failure, cleanup_failure],
                ) from None
            raise


async def test_large_image_seed_actor_rechecks_current_project_owner(monkeypatch):
    original_owner, current_owner = uuid.uuid4(), uuid.uuid4()
    project = SimpleNamespace(
        id=uuid.uuid4(), owner_id=original_owner, display_id="P-LARGE-IMG"
    )
    access = SimpleNamespace(
        user_id=original_owner, project_id=project.id, is_manager=True
    )

    async def resolve_access(db, *, actor_id, project):
        project.owner_id = current_owner
        return access

    monkeypatch.setattr(catalog, "resolve_project_access", resolve_access)
    issues = []
    assert (
        await catalog._resolve_large_image_seed_actor(object(), project, issues) is None
    )
