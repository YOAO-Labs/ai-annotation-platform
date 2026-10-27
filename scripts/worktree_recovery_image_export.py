"""Real image-copy/format components in the isolated offline recovery fixture.

Only explicitly supplied, already committed verified image Tasks participate.
The ready case invokes the shared publisher inline. The building case executes
claim, transfer, serialization and a pending ZIP write, then intentionally stops
before publication. Neither case claims HTTP or Celery delivery qualification.
The fixture owns its transaction boundaries; restoring never resumes these Jobs
or rewrites their immutable URLs, business snapshots or seven-day deadlines.
"""

from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

from worktree_recovery_export import _read_exact, validate_media_pairs

STATES = ("building", "ready")
TARGETS = ["aap_json", "coco", "yolo-det"]
OPTIONS = {
    "include_attributes": True,
    "video_frame_mode": "keyframes",
    "axis_frame": "iso",
}
FIXTURE_KEY = "recovery_image_fixture"
QUALIFICATION = "offline-internal-inline-components; no HTTP or Celery dispatch"
MAX_BYTES = 1024 * 1024


def job_identifier(state):
    assert state in STATES
    return uuid.uuid5(uuid.NAMESPACE_URL, "aap:recovery-image-export-job:" + state)


def attempt_identifier(state):
    assert state in STATES
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, "aap:recovery-image-inline-attempt:" + state)
    )


def _expected(raw_by_task):
    assert type(raw_by_task) is dict and len(raw_by_task) == 2
    result = {}
    for identifier, raw in raw_by_task.items():
        assert isinstance(identifier, uuid.UUID)
        assert type(raw) is bytes and 0 < len(raw) <= MAX_BYTES
        result[str(identifier)] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    return result


def _assert_input(snapshot, expected):
    """Never widen an explicit known selection to the fixture's legacy Tasks."""
    assert snapshot["schema"] == 3
    assert snapshot["task_ids"] == sorted(expected)
    assert sorted(row["id"] for row in snapshot["tasks"]) == sorted(expected)
    assert snapshot["references"] == {}
    assert snapshot["targets"] == TARGETS and snapshot["options"] == OPTIONS
    assert len(snapshot["media"]) == 2
    assert {row["source"]["owner_kind"] for row in snapshot["media"]} == {
        "dataset_asset",
        "task_original",
    }
    for binding in snapshot["media"]:
        assert binding["role"] == "image" and binding["link_id"] is None
        assert binding["source"]["media_type"] == "image"
        assert {
            name: binding["source"][name] for name in ("sha256", "size_bytes")
        } == expected[binding["task_id"]]


async def _new_job(db, *, state, project, actor, storage, expected):
    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.services.exporting import image_jobs, output_authority

    selection = [uuid.UUID(identifier) for identifier in sorted(expected)]
    request = SimpleNamespace(
        state=SimpleNamespace(api_key_id=None, api_key_scopes=None),
        headers={},
    )
    job = AsyncJob(
        id=job_identifier(state),
        kind="export",
        project_id=project.id,
        user_id=actor.id,
        status="pending",
        celery_task_id=attempt_identifier(state),
        payload={
            "project_id": str(project.id),
            "project_display_id": project.display_id,
            "batch_id": None,
            "targets": TARGETS,
            **OPTIONS,
            "task_ids": sorted(expected),
            "scope": {"task_ids": sorted(expected)},
            "output_authority": await output_authority.capture(
                db, project, actor, request
            ),
            FIXTURE_KEY: {
                "schema": 1,
                "qualification": QUALIFICATION,
                "state": state,
                "source_export_bucket": storage.export_bucket,
                "source_public_path_prefix": urlsplit(
                    settings.minio_public_url or ""
                ).path.rstrip("/"),
                "expected": expected,
            },
        },
        result={},
    )
    db.add(job)
    await db.flush()
    receipt = await image_jobs.capture_job(
        db,
        job,
        project,
        batch_id=None,
        targets=TARGETS,
        opts=OPTIONS,
        task_ids=selection,
    )
    _assert_input(receipt.input_snapshot, expected)
    # The physical transfer and publisher use independent, freshly authorized
    # sessions. Every source row and newly allocated responsibility must exist
    # outside this transaction before they start.
    await db.commit()
    return receipt


async def _interrupt_after_pending_zip(db, receipt):
    from sqlalchemy import select
    from app.db.models.export_output import ExportOutput
    from app.services import async_job as jobs, export_media_bundles as bundles
    from app.services import export_outputs, export_storage
    from app.services.exporting import image_delivery, image_inputs
    from app.services.exporting.packaging import build_export_zip
    from app.services.exporting.reference_media import check_cancelled
    from app.workers import lidar_export as publisher

    claimed = await bundles.claim(db, receipt.id)
    assert claimed is not None
    invocation = attempt_identifier("building")
    await jobs.mark_running(db, claimed.job_id, celery_task_id=invocation)
    await db.commit()

    async def guard(owned):
        await publisher._guard(owned, claimed, invocation)

    context = await image_delivery.prepare(db, claimed, guard=guard)
    path = None
    try:
        path, count, size = await build_export_zip(
            db,
            claimed.project_id,
            batch_id=None,
            targets=TARGETS,
            include_attributes=OPTIONS["include_attributes"],
            video_frame_mode=OPTIONS["video_frame_mode"],
            axis_frame=OPTIONS["axis_frame"],
            format_options=OPTIONS,
            image_media=context,
            check_cancel=lambda: check_cancelled(db, claimed.job_id),
        )
        await guard(db)
        await bundles.check_claim(db, claimed)
        await db.commit()
        candidate = await export_outputs.create(
            db,
            project_id=claimed.project_id,
            job_id=claimed.job_id,
            media="image",
            format=",".join(TARGETS),
            file_count=count,
            path=path,
            expected_size=size,
            output_id=claimed.zip_output_id,
            media_bundle_id=claimed.id,
            created_at=claimed.created_at,
            expires_at=claimed.expires_at,
        )

        async def zip_guard(owned):
            await guard(owned)
            row = await owned.scalar(
                select(ExportOutput)
                .where(ExportOutput.id == candidate.id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            assert row is not None and row.state == "pending"
            assert export_outputs.same(row, candidate)

        await publisher._io(
            db,
            claimed,
            lambda: export_storage.write(candidate, path),
            guard=zip_guard,
        )
        await guard(db)
        await image_inputs.check(db, claimed.input_snapshot, lock=True)
        # Intentionally retain a real running attempt and verified pending ZIP.
        # There is no publish latch, completed Job, signed ZIP or cache index.
        await db.commit()
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


async def seed_image_exports(db, *, project, actor, storage, raw_by_task):
    """Caller has committed the two exact sources; create real owned components."""
    from app.db.models.async_job import AsyncJob
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.services import export_media_bundles as bundles
    from app.workers import lidar_export as publisher

    expected = _expected(raw_by_task)
    for state in STATES:
        receipt = await _new_job(
            db,
            state=state,
            project=project,
            actor=actor,
            storage=storage,
            expected=expected,
        )
        if state == "building":
            await _interrupt_after_pending_zip(db, receipt)
        else:
            await publisher.run_owned(
                db,
                project_id=str(project.id),
                batch_id=None,
                task_ids=[uuid.UUID(identifier) for identifier in sorted(expected)],
                targets=TARGETS,
                opts=OPTIONS,
                async_job_id=str(receipt.job_id),
                celery_task_id=attempt_identifier(state),
                media_kind="image",
            )
        await db.commit()
        job = await db.get(AsyncJob, receipt.job_id, populate_existing=True)
        row = await db.get(ExportMediaBundle, receipt.id, populate_existing=True)
        assert job is not None and row is not None
        assert job.status == ("completed" if state == "ready" else "running")
        assert row.state == state and row.lease_token is not None
        assert (row.published_at is not None) == (state == "ready")
        _assert_input(bundles.snapshot(row).input_snapshot, expected)
        if state == "ready":
            assert job.result["cache_hit"] is False
    await db.commit()


def _historical_url(url, metadata, key):
    assert isinstance(url, str) and url
    path = unquote(urlsplit(url).path)
    assert (
        path
        == f"{metadata['source_public_path_prefix']}/{metadata['source_export_bucket']}/{key}"
    )


def _verify_archive(raw, *, bundle, output, metadata, expected):
    from app.services.exporting.image_media_paths import archive_paths, paths

    snapshot = bundle.input_snapshot
    relative = paths(snapshot)
    destinations = archive_paths(snapshot)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        manifest = json.loads(archive.read("images_manifest.json"))
        assert manifest["expires_at"] == bundle.expires_at.isoformat()
        assert len(manifest["images"]) == 2
        entries = {entry["task_id"]: entry for entry in manifest["images"]}
        assert set(entries) == set(expected)
        for task_id, entry in entries.items():
            identifier = uuid.UUID(task_id)
            assert entry["storage_mode"] == "managed"
            assert entry["rel_path"] == relative[identifier]
            assert entry["archive_path"] == "images/" + relative[identifier]
            assert entry["archive_paths"] == destinations[identifier]
            assert {name: entry[name] for name in ("sha256", "size_bytes")} == expected[
                task_id
            ]
            assert entry["expires_at"] == bundle.expires_at.isoformat()
            key = bundle.logical_paths["targets"]["coco"][relative[identifier]]
            assert bundle.inventory["objects"][key] == expected[task_id]
            _historical_url(entry["presigned_url"], metadata, key)
            assert not any(path in names for path in destinations[identifier])
            assert (
                "yolo-det/labels/" + relative[identifier].rsplit(".", 1)[0] + ".txt"
                in names
            )
        coco = json.loads(archive.read("coco/annotations.json"))
        aap = json.loads(archive.read("aap_json/annotations.json"))
        image_paths = {"images/" + value for value in relative.values()}
        assert {row["file_name"] for row in coco["images"]} == image_paths
        assert {row["file_path"] for row in aap["tasks"]} == image_paths
        assert output.file_count == 6


async def verify_image_exports(db, *, storage, raw_by_task):
    """Verify relocated copies and historical ZIP bytes; never renew or resume."""
    from sqlalchemy import select
    from app.db.models.async_job import AsyncJob
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput
    from app.services import export_media_bundles as bundles
    from app.services import export_media_storage as physical, export_storage
    from app.services.exporting.image_media_paths import logical_sources

    expected = _expected(raw_by_task)
    objects = 0
    for state in STATES:
        job = await db.get(AsyncJob, job_identifier(state))
        assert job is not None and job.status == (
            "completed" if state == "ready" else "running"
        )
        assert job.celery_task_id == attempt_identifier(state)
        assert (job.completed_at is not None) == (state == "ready")
        metadata = job.payload[FIXTURE_KEY]
        assert metadata["schema"] == 1 and metadata["qualification"] == QUALIFICATION
        assert metadata["state"] == state and metadata["expected"] == expected
        row = await db.get(
            ExportMediaBundle, uuid.UUID(job.payload["image_media"]["bundle_id"])
        )
        assert row is not None and row.state == state and row.job_id == job.id
        assert row.lease_token is not None and row.lease_expires_at is not None
        assert (row.published_at is not None) == (state == "ready")
        assert row.expires_at - row.created_at == timedelta(days=7)
        frozen = bundles.snapshot(row)
        assert frozen.input_digest == job.payload["image_media"]["input_digest"]
        _assert_input(frozen.input_snapshot, expected)
        owner = physical.resolve(frozen)
        assert owner.bucket == storage.export_bucket
        assert metadata["source_export_bucket"] != owner.bucket
        physical.verify_inventory(storage.client, frozen)
        keys = bundles.source_objects(frozen, logical_sources(frozen.input_snapshot))
        for binding in frozen.input_snapshot["media"]:
            source = binding["source"]
            key = keys[binding["source_key"]]
            assert row.inventory["objects"][key] == expected[binding["task_id"]]
            actual = _read_exact(
                storage, owner.bucket, key, source["sha256"], source["size_bytes"]
            )
            assert actual == raw_by_task[uuid.UUID(binding["task_id"])]
            objects += 1
        output = await db.get(ExportOutput, row.zip_output_id)
        assert output is not None and output.state == (
            "ready" if state == "ready" else "pending"
        )
        assert (
            output.created_at == row.created_at and output.expires_at == row.expires_at
        )
        validate_media_pairs([row], [output])
        zip_owner = export_storage.resolve(output)
        assert zip_owner.bucket == owner.bucket
        raw = _read_exact(
            storage,
            zip_owner.bucket,
            zip_owner.object_key,
            output.sha256,
            output.size_bytes,
        )
        _verify_archive(
            raw, bundle=frozen, output=output, metadata=metadata, expected=expected
        )
        assert not list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == output.id)
            )
        )
        if state == "ready":
            assert job.result["expires_at"] == row.expires_at.isoformat()
            assert job.result["object_key"] == zip_owner.object_key
            assert job.result["size_bytes"] == output.size_bytes
            _historical_url(job.result["download_url"], metadata, zip_owner.object_key)
        else:
            assert not job.result.get("download_url")
    return {
        "pinned_image_export_media_bundles": 2,
        "verified_image_export_media_objects": objects,
        "verified_image_export_zip_objects": 2,
        "invalidated_schema3_image_export_cache_indexes": 1,
        "image_export_inline_completed_attempts": 1,
        "image_export_interrupted_building_attempts": 1,
        "image_export_http_or_celery_dispatch_qualified": False,
        "image_export_embedded_urls_remapped": False,
        "image_export_delivery_after_restore": "requires-new-bundle-and-zip",
    }
