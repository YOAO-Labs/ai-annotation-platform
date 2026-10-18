"""Labeling's source identity and Task-image publication projections."""

from __future__ import annotations

import json
from pathlib import Path
import re
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.models.dataset import DatasetItem
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import create_project
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_IMAGE_AUTHORITIES_SQL = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "sql"
    / "0220_image_authorities.sql"
)
_MODULE_DATA_GRANTS = (
    Path(__file__).resolve().parents[3] / "infra" / "docker" / "module-data-grants.sql"
)


def _sql_function_body(source: str, name: str) -> str:
    prefix = f"CREATE FUNCTION aap_labeling_v1.{name}"
    start = source.index(prefix)
    end = source.index("$function$;", start)
    return source[start:end]


_READ = text(
    "SELECT aap_labeling_v1.read_image_task_source("
    "CAST(:task_id AS uuid), CAST(:actor_id AS uuid), CAST(:lock_source AS boolean))"
)
_OBSERVE = text(
    "SELECT aap_labeling_v1.observe_image_task_source("
    "CAST(:task_id AS uuid), CAST(:expected_snapshot AS text), "
    "CAST(:identity AS jsonb))"
)
_PUBLISH = text(
    "SELECT aap_labeling_v1.publish_task_image_thumbnail("
    "CAST(:task_id AS uuid), CAST(:expected_snapshot AS text), "
    "CAST(:source_generation AS bigint), CAST(:key AS text), "
    "CAST(:blurhash AS text))"
)
_REFERENCES = text(
    "SELECT aap_labeling_v1.image_thumbnail_references(CAST(:prefix AS text))"
)
_SCAN_TASKS = text(
    "SELECT aap_labeling_v1.scan_image_task_sources(CAST(:selection AS jsonb), "
    "CAST(:after_id AS uuid), CAST(:page_size AS integer))"
)
_INVALIDATE_THUMBNAIL = text(
    "SELECT aap_labeling_v1.invalidate_task_image_thumbnail("
    "CAST(:task_id AS uuid), CAST(:expected_snapshot AS text), CAST(:expected_key AS text))"
)


async def test_image_task_authorities_use_only_granted_task_columns():
    source = _IMAGE_AUTHORITIES_SQL.read_text(encoding="utf-8")
    for function_name in (
        "read_image_task_source",
        "observe_image_task_source",
        "publish_task_image_thumbnail",
        "scan_image_task_sources",
        "invalidate_task_image_thumbnail",
    ):
        body = _sql_function_body(source, function_name)
        assert (
            re.search(r"\b(?:SELECT|RETURNING)\s+task\s*\.\s*\*", body, re.IGNORECASE)
            is None
        ), function_name

    scan = _sql_function_body(source, "scan_image_task_sources")
    assert "_image_source_snapshot_fields(" in scan
    assert "_image_source_snapshot(task)" not in scan

    grants = _MODULE_DATA_GRANTS.read_text(encoding="utf-8")
    assert 'GRANT UPDATE (updated_at) ON public.tasks TO :"labeling_owner";' in grants
    assert (
        re.search(
            r"GRANT\s+(?:ALL(?:\s+PRIVILEGES)?|SELECT(?!\s*\())[^;]*"
            r"\s+ON\s+public\.tasks\b",
            grants,
            re.IGNORECASE,
        )
        is None
    )


def _object(value: object) -> dict:
    parsed = json.loads(value) if isinstance(value, str) else value
    assert isinstance(parsed, dict)
    return parsed


def _data(reply: object, schema: str = "aap-labeling-image-source/v1") -> dict:
    parsed = _object(reply)
    assert set(parsed) == {"schema", "ok", "data"}, parsed
    assert parsed["schema"] == schema
    assert parsed["ok"] is True
    assert isinstance(parsed["data"], dict)
    return parsed["data"]


def _error(
    reply: object, code: str, schema: str = "aap-labeling-image-source/v1"
) -> None:
    assert _object(reply) == {
        "schema": schema,
        "ok": False,
        "error": {"code": code},
    }


async def _seed_image_task(
    db_session,
    *,
    actor_id,
    status: str = "pending",
    assignee_id=None,
    file_path: str | None = None,
) -> Task:
    project = await create_project(db_session, owner_id=actor_id)
    db_session.add(
        ProjectMember(
            project_id=project.id,
            user_id=actor_id,
            role="annotator",
            assigned_by=actor_id,
        )
    )
    task = Task(
        project_id=project.id,
        display_id=f"T-IMG-{uuid4().hex[:12]}",
        file_name="source.png",
        file_path=file_path or f"image-source/{uuid4()}.png",
        file_type="image",
        status=status,
        assignee_id=assignee_id if assignee_id is not None else actor_id,
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def _read(db_session, task_id, *, actor_id=None, lock_source=False) -> dict:
    return _object(
        await db_session.scalar(
            _READ,
            {
                "task_id": task_id,
                "actor_id": actor_id,
                "lock_source": lock_source,
            },
        )
    )


async def _observe(db_session, task_id, snapshot: str, identity: dict) -> dict:
    return _object(
        await db_session.scalar(
            _OBSERVE,
            {
                "task_id": task_id,
                "expected_snapshot": snapshot,
                "identity": json.dumps(identity),
            },
        )
    )


async def _publish(
    db_session,
    task_id,
    *,
    snapshot: str,
    generation: int,
    key: str,
    blurhash: str | None,
) -> dict:
    return _object(
        await db_session.scalar(
            _PUBLISH,
            {
                "task_id": task_id,
                "expected_snapshot": snapshot,
                "source_generation": generation,
                "key": key,
                "blurhash": blurhash,
            },
        )
    )


async def _source_generation_and_task_version(db_session, task_id):
    row = (
        await db_session.execute(
            text(
                "SELECT media_source_generation, version FROM public.tasks "
                "WHERE id = CAST(:task_id AS uuid)"
            ),
            {"task_id": task_id},
        )
    ).one()
    return int(row.media_source_generation), int(row.version)


async def test_task_source_visibility_and_upload_readiness_are_current(db_session):
    actor_id = await _seed_actor(db_session, role="employee")
    other_id = await _seed_actor(db_session, role="employee")
    task = await _seed_image_task(db_session, actor_id=actor_id)
    db_session.add(
        ProjectMember(
            project_id=task.project_id,
            user_id=other_id,
            role="annotator",
            assigned_by=actor_id,
        )
    )
    await db_session.flush()

    source = _data(
        await _read(db_session, task.id, actor_id=actor_id, lock_source=True)
    )
    assert source["task_id"] == str(task.id)
    assert source["project_id"] == str(task.project_id)
    assert source["dataset_item_id"] is None
    assert source["source_generation"] == 1
    assert len(source["source_snapshot"]) == 64
    assert source["upload_ready"] is True
    assert set(source) == {
        "task_id",
        "project_id",
        "dataset_item_id",
        "file_type",
        "file_path",
        "file_name",
        "created_at",
        "source_generation",
        "source_snapshot",
        "object_identity",
        "source_drifted",
        "upload_ready",
        "thumbnail_path",
        "blurhash",
        "thumbnail_source_generation",
    }

    _error(
        await _read(db_session, task.id, actor_id=other_id),
        "task_not_visible",
    )

    task.status = "uploading"
    await db_session.flush()
    visible_upload = _data(await _read(db_session, task.id, actor_id=actor_id))
    assert visible_upload["upload_ready"] is False
    assert visible_upload["source_snapshot"] == source["source_snapshot"]
    _error(await _read(db_session, task.id), "source_not_ready")

    task.status = "pending"
    await db_session.flush()
    actor = await db_session.get(User, actor_id)
    assert actor is not None
    actor.is_active = False
    await db_session.flush()
    _error(
        await _read(db_session, task.id, actor_id=actor_id),
        "account_not_active",
    )
    trusted = _data(await _read(db_session, task.id))
    assert trusted["source_snapshot"] == source["source_snapshot"]
    identity = {"etag": "accepted-before-disable", "byte_size": 1024}
    observed = _data(
        await _observe(db_session, task.id, trusted["source_snapshot"], identity)
    )
    background_prefix = f"image-assets/{uuid4()}/g1/a{uuid4()}/"
    background_publish = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=observed["source_snapshot"],
            generation=observed["source_generation"],
            key=background_prefix + "thumbnail.webp",
            blurhash=None,
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert background_publish == {"task_id": str(task.id), "applied": True}
    assert await db_session.scalar(_REFERENCES, {"prefix": background_prefix}) is True


async def test_source_generation_tracks_source_fields_and_rejects_aba(db_session):
    actor_id = await _seed_actor(db_session)
    original_path = f"image-source/{uuid4()}.png"
    task = await _seed_image_task(
        db_session, actor_id=actor_id, file_path=original_path
    )
    source = _data(await _read(db_session, task.id))
    original_generation = source["source_generation"]
    original_snapshot = source["source_snapshot"]
    original_task_version = task.version

    task.file_name = "renamed-without-changing-source.png"
    task.status = "in_progress"
    task.thumbnail_path = "thumbnails/legacy.webp"
    task.blurhash = "legacy-blurhash"
    await db_session.flush()
    unchanged = _data(await _read(db_session, task.id))
    assert unchanged["source_generation"] == original_generation
    assert unchanged["source_snapshot"] == original_snapshot

    task.file_path = f"image-source/{uuid4()}.png"
    await db_session.flush()
    changed = _data(await _read(db_session, task.id))
    assert changed["source_generation"] == original_generation + 1
    assert changed["source_snapshot"] != original_snapshot
    assert changed["object_identity"] is None
    assert changed["source_drifted"] is False
    assert changed["thumbnail_path"] is None
    assert changed["blurhash"] is None

    task.file_path = original_path
    await db_session.flush()
    returned = _data(await _read(db_session, task.id))
    assert returned["source_generation"] == original_generation + 2
    assert returned["source_snapshot"] != original_snapshot

    before_type_change = returned["source_snapshot"]
    task.file_type = "video"
    await db_session.flush()
    assert await _source_generation_and_task_version(db_session, task.id) == (
        original_generation + 3,
        original_task_version,
    )
    _error(await _read(db_session, task.id), "not_image")
    task.file_type = "image"
    await db_session.flush()
    after_type_change = _data(await _read(db_session, task.id))
    assert after_type_change["source_generation"] == original_generation + 4
    assert after_type_change["source_snapshot"] != before_type_change

    other_project = await create_project(db_session, owner_id=actor_id)
    task.project_id = other_project.id
    await db_session.flush()
    after_project_change = _data(await _read(db_session, task.id))
    assert after_project_change["source_generation"] == original_generation + 5
    assert after_project_change["project_id"] == str(other_project.id)
    assert (
        after_project_change["source_snapshot"] != after_type_change["source_snapshot"]
    )
    assert await _source_generation_and_task_version(db_session, task.id) == (
        original_generation + 5,
        original_task_version,
    )


async def test_observation_binds_once_then_drift_fences_thumbnail_publication(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    initial = _data(await _read(db_session, task.id))
    identity = {"etag": "image-v1", "byte_size": 8192, "version_id": "v1"}
    bound = _data(
        await _observe(db_session, task.id, initial["source_snapshot"], identity)
    )
    assert bound["source_generation"] == initial["source_generation"]
    assert bound["source_snapshot"] == initial["source_snapshot"]
    assert bound["object_identity"] == identity
    assert bound["source_drifted"] is False

    same = _data(
        await _observe(db_session, task.id, bound["source_snapshot"], identity)
    )
    assert same["source_generation"] == bound["source_generation"]

    task.status = "completed"
    await db_session.flush()
    asset_id, attempt = uuid4(), uuid4()
    prefix = f"image-assets/{asset_id}/g1/a{attempt}/"
    key = prefix + "thumbnail.webp"
    published = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=bound["source_snapshot"],
            generation=bound["source_generation"],
            key=key,
            blurhash="new-blurhash",
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert published == {"task_id": str(task.id), "applied": True}
    assert await db_session.scalar(_REFERENCES, {"prefix": prefix}) is True

    task.blurhash = "preserve-this"
    await db_session.flush()
    unchanged_hash = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=bound["source_snapshot"],
            generation=bound["source_generation"],
            key=key,
            blurhash=None,
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert unchanged_hash["applied"] is True
    assert task.blurhash == "preserve-this"

    changed_identity = {
        "etag": "image-v2",
        "byte_size": 8192,
        "version_id": "v2",
    }
    drifted = _data(
        await _observe(db_session, task.id, bound["source_snapshot"], changed_identity)
    )
    assert drifted["source_generation"] == bound["source_generation"] + 1
    assert drifted["source_snapshot"] != bound["source_snapshot"]
    assert drifted["object_identity"] == changed_identity
    assert drifted["source_drifted"] is True
    drifted_source = _data(await _read(db_session, task.id))
    assert drifted_source["thumbnail_path"] is None
    assert drifted_source["blurhash"] is None
    assert await db_session.scalar(_REFERENCES, {"prefix": prefix}) is False

    stale = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=bound["source_snapshot"],
            generation=bound["source_generation"],
            key=key,
            blurhash="stale",
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert stale == {"task_id": str(task.id), "applied": False}

    _error(
        await _observe(db_session, task.id, bound["source_snapshot"], identity),
        "source_changed",
    )
    assert (
        await db_session.scalar(_REFERENCES, {"prefix": "unexpected-prefix/"}) is True
    )


async def test_task_relink_advances_source_and_cannot_publish_old_thumbnail(db_session):
    actor_id = await _seed_actor(db_session)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    source = _data(await _read(db_session, task.id))
    identity = {"etag": "legacy-task-image", "byte_size": 2048}
    bound = _data(
        await _observe(db_session, task.id, source["source_snapshot"], identity)
    )
    assert bound["object_identity"]["version_id"] is None
    same_identity = _data(
        await _observe(
            db_session,
            task.id,
            bound["source_snapshot"],
            {**identity, "version_id": None},
        )
    )
    assert same_identity["source_generation"] == bound["source_generation"]
    _error(
        await _observe(
            db_session,
            task.id,
            bound["source_snapshot"],
            {**identity, "unexpected": "field"},
        ),
        "invalid_request",
    )

    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="image")
    item = DatasetItem(
        id=uuid4(),
        dataset_id=dataset_id,
        file_name="linked.png",
        file_path=f"dataset-items/{uuid4()}.png",
        file_type="image",
        file_size=2048,
        content_hash="a" * 64,
    )
    db_session.add(item)
    await db_session.flush()

    task.dataset_item_id = item.id
    await db_session.flush()
    relinked = _data(await _read(db_session, task.id))
    assert relinked["source_generation"] == bound["source_generation"] + 1
    assert relinked["dataset_item_id"] == str(item.id)
    assert relinked["object_identity"] is None
    assert relinked["thumbnail_path"] is None

    stale = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=bound["source_snapshot"],
            generation=bound["source_generation"],
            key=f"image-pyramids/{uuid4()}/g1/a{uuid4()}/overview.webp",
            blurhash="stale",
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert stale == {"task_id": str(task.id), "applied": False}


async def test_thumbnail_output_and_cleanup_scopes_are_narrow(db_session):
    actor_id = await _seed_actor(db_session)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    source = _data(await _read(db_session, task.id))
    identity = {"etag": "image-source", "byte_size": 4096}
    bound = _data(
        await _observe(db_session, task.id, source["source_snapshot"], identity)
    )

    asset_id, attempt = uuid4(), uuid4()
    root_prefix = f"image-pyramids/{asset_id}/"
    generation_prefix = root_prefix + "g2/"
    attempt_prefix = generation_prefix + f"a{attempt}/"
    key = attempt_prefix + "overview.webp"
    published = _data(
        await _publish(
            db_session,
            task.id,
            snapshot=bound["source_snapshot"],
            generation=bound["source_generation"],
            key=key,
            blurhash=None,
        ),
        "aap-labeling-image-thumbnail/v1",
    )
    assert published["applied"] is True
    assert await db_session.scalar(_REFERENCES, {"prefix": attempt_prefix}) is True
    assert await db_session.scalar(_REFERENCES, {"prefix": generation_prefix}) is True
    assert await db_session.scalar(_REFERENCES, {"prefix": root_prefix}) is True
    assert (
        await db_session.scalar(_REFERENCES, {"prefix": f"image-pyramids/{uuid4()}/"})
        is False
    )

    invalid_key = await _publish(
        db_session,
        task.id,
        snapshot=bound["source_snapshot"],
        generation=bound["source_generation"],
        key="thumbnails/legacy.webp",
        blurhash="must-not-write",
    )
    _error(invalid_key, "invalid_request", "aap-labeling-image-thumbnail/v1")
    await db_session.refresh(task)
    assert task.thumbnail_path == key
    assert task.blurhash is None


async def test_task_backfill_scope_has_stable_source_snapshot_and_paging(db_session):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    tasks = [
        Task(
            project_id=project.id,
            display_id=f"T-IMG-SCAN-{index}-{uuid4().hex[:8]}",
            file_name=f"scan-{index}.png",
            file_path=f"image-source/scan-{uuid4()}.png",
            file_type="image",
            status="pending",
            assignee_id=actor_id,
        )
        for index in range(2)
    ]
    uploading = Task(
        project_id=project.id,
        display_id=f"T-IMG-UP-{uuid4().hex[:8]}",
        file_name="uploading.png",
        file_path=f"image-source/uploading-{uuid4()}.png",
        file_type="image",
        status="uploading",
        assignee_id=actor_id,
    )
    db_session.add_all([*tasks, uploading])
    await db_session.flush()
    tasks.sort(key=lambda task: task.id.int)

    source = _data(await _read(db_session, tasks[0].id))
    identity = {"etag": "backfill-source", "byte_size": 2048}
    bound = _data(
        await _observe(db_session, tasks[0].id, source["source_snapshot"], identity)
    )
    key = f"image-assets/{uuid4()}/g1/a{uuid4()}/thumbnail.webp"
    assert (
        _data(
            await _publish(
                db_session,
                tasks[0].id,
                snapshot=bound["source_snapshot"],
                generation=bound["source_generation"],
                key=key,
                blurhash="fresh",
            ),
            "aap-labeling-image-thumbnail/v1",
        )["applied"]
        is True
    )

    cutoff = await db_session.scalar(text("SELECT clock_timestamp()"))
    selection = {
        "owner_kind": "task",
        "dataset_id": None,
        "project_id": str(project.id),
        "owner_ids": [],
        "all_owners": False,
        "purpose": "thumbnail",
        "only_missing": True,
        "accepted_before": cutoff.isoformat(),
    }

    async def scan(after_id=None):
        return _object(
            await db_session.scalar(
                _SCAN_TASKS,
                {
                    "selection": json.dumps(selection),
                    "after_id": after_id,
                    "page_size": 1,
                },
            )
        )

    first = _data(await scan())
    assert first["total"] == 2
    assert first["complete"] is False
    assert first["items"][0]["task_id"] == str(tasks[0].id)
    assert first["items"][0]["thumbnail_path"] == key
    assert first["next_id"] == str(tasks[0].id)

    second = _data(await scan(first["next_id"]))
    assert second["complete"] is True
    assert second["items"][0]["task_id"] == str(tasks[1].id)
    assert second["next_id"] is None
    assert (first["snapshot_digest"], first["total"], first["accepted_before"]) == (
        second["snapshot_digest"],
        second["total"],
        second["accepted_before"],
    )


async def test_task_source_snapshot_and_backfill_digest_are_timezone_independent(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    cutoff = await db_session.scalar(
        text("SELECT clock_timestamp() + interval '1 minute'")
    )
    selection = {
        "owner_kind": "task",
        "dataset_id": None,
        "project_id": str(task.project_id),
        "owner_ids": [],
        "all_owners": False,
        "purpose": "thumbnail",
        "only_missing": False,
        "accepted_before": cutoff.isoformat(),
    }

    async def read_source_and_scan():
        source = _data(await _read(db_session, task.id))["source_snapshot"]
        scan = _data(
            await db_session.scalar(
                _SCAN_TASKS,
                {
                    "selection": json.dumps(selection),
                    "after_id": None,
                    "page_size": 10,
                },
            )
        )
        return source, scan

    await db_session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    utc_source_snapshot, utc_scan = await read_source_and_scan()
    await db_session.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
    shanghai_source_snapshot, shanghai_scan = await read_source_and_scan()

    assert shanghai_source_snapshot == utc_source_snapshot
    assert shanghai_scan["snapshot_digest"] == utc_scan["snapshot_digest"]
    assert shanghai_scan["accepted_before"] != utc_scan["accepted_before"]


async def test_task_thumbnail_invalidation_is_snapshot_and_key_cas(db_session):
    actor_id = await _seed_actor(db_session)
    task = await _seed_image_task(db_session, actor_id=actor_id)
    source = _data(await _read(db_session, task.id))
    bound = _data(
        await _observe(
            db_session,
            task.id,
            source["source_snapshot"],
            {"etag": "invalidate-source", "byte_size": 1024},
        )
    )
    key = f"image-assets/{uuid4()}/g1/a{uuid4()}/thumbnail.webp"
    assert (
        _data(
            await _publish(
                db_session,
                task.id,
                snapshot=bound["source_snapshot"],
                generation=bound["source_generation"],
                key=key,
                blurhash="keep-until-invalidation",
            ),
            "aap-labeling-image-thumbnail/v1",
        )["applied"]
        is True
    )

    invalidated = _object(
        await db_session.scalar(
            _INVALIDATE_THUMBNAIL,
            {
                "task_id": task.id,
                "expected_snapshot": bound["source_snapshot"],
                "expected_key": key,
            },
        )
    )
    assert _data(invalidated, "aap-labeling-image-thumbnail/v1") == {
        "task_id": str(task.id),
        "applied": True,
    }
    await db_session.refresh(task)
    assert task.thumbnail_path is None and task.blurhash is None
    assert task.media_source_generation == bound["source_generation"]
    assert (
        _data(await _read(db_session, task.id))["source_snapshot"]
        == bound["source_snapshot"]
    )

    stale = _object(
        await db_session.scalar(
            _INVALIDATE_THUMBNAIL,
            {
                "task_id": task.id,
                "expected_snapshot": bound["source_snapshot"],
                "expected_key": key,
            },
        )
    )
    assert _data(stale, "aap-labeling-image-thumbnail/v1")["applied"] is False
