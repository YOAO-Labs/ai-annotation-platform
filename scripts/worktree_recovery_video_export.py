"""Real schema-four video components in the isolated offline recovery fixture.

Create two tiny self-contained videos and publish their canonical originals
before the export's independent sessions start. Ready uses the shared publisher
inline; building executes copy/probe/serialization and saves a pending ZIP, then
stops before publication. This qualifies offline components, not HTTP/Celery or
browser decoding. Verification reads saved delivery copies only and never
regenerates videos, resolves old sources, signs URLs or resumes old attempts.
"""

import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import uuid
import zipfile

from worktree_recovery_export import _read_exact, validate_media_pairs
from worktree_recovery_image_export import _historical_url

STATES = ("building", "ready")
CASES = ("dataset", "standalone")
TARGETS = ["aap_json", "kitti", "mot", "video_json"]
OPTIONS = {
    "include_attributes": True,
    "video_frame_mode": "keyframes",
    "axis_frame": "iso",
}
FIXTURE_KEY = "recovery_video_fixture"
QUALIFICATION = "offline-internal-inline-video-components; no HTTP or Celery dispatch"
MAX_BYTES = 1024 * 1024
VIDEO = {
    "fps": 3.0,
    "frame_count": 6,
    "width": 64,
    "height": 48,
    "duration_ms": 2000,
    "codec": "h264",
}


def identifier(kind):
    return uuid.uuid5(uuid.NAMESPACE_URL, "aap:recovery-video-export:" + kind)


def job_identifier(state):
    assert state in STATES
    return identifier("job:" + state)


def attempt_identifier(state):
    assert state in STATES
    return str(identifier("inline-attempt:" + state))


def make_videos():
    """Real ffmpeg output is retained in the fixture receipt, not regenerated later."""
    result = {}
    with tempfile.TemporaryDirectory(prefix="aap-recovery-video-") as temporary:
        for case, color in zip(CASES, ("red", "blue"), strict=True):
            path = Path(temporary) / case / "same.mp4"
            path.parent.mkdir()
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=64x48:r=3",
                        "-frames:v",
                        "6",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-pix_fmt",
                        "yuv420p",
                        "-threads",
                        "1",
                        "-g",
                        "3",
                        "-bf",
                        "0",
                        "-movflags",
                        "+faststart",
                        "-y",
                        str(path),
                    ],
                    check=True,
                    timeout=30,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError(
                    "Offline video fixture requires a working local ffmpeg encoder"
                ) from None
            raw = path.read_bytes()
            assert 0 < len(raw) <= MAX_BYTES
            result[case] = raw
    assert result["dataset"] != result["standalone"]
    return result


def expected_sources(raw_by_case):
    assert set(raw_by_case) == set(CASES)
    result = {}
    for case, raw in raw_by_case.items():
        assert type(raw) is bytes and 0 < len(raw) <= MAX_BYTES
        result[str(identifier("task:" + case))] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "content_base64": base64.b64encode(raw).decode("ascii"),
            "owner_kind": "dataset_asset" if case == "dataset" else "task_original",
        }
    return result


def _content(expected):
    assert type(expected) is dict and len(expected) == 2
    result = {}
    for task_id, receipt in expected.items():
        assert task_id in {str(identifier("task:" + case)) for case in CASES}
        assert (
            type(receipt["size_bytes"]) is int
            and 0 < receipt["size_bytes"] <= MAX_BYTES
        )
        assert (
            type(receipt["content_base64"]) is str
            and len(receipt["content_base64"]) <= 2 * MAX_BYTES
        )
        raw = base64.b64decode(receipt["content_base64"], validate=True)
        assert len(raw) == receipt["size_bytes"]
        assert hashlib.sha256(raw).hexdigest() == receipt["sha256"]
        result[task_id] = raw
    return result


def _receipt(expected):
    return {name: expected[name] for name in ("sha256", "size_bytes")}


def _assert_input(snapshot, expected):
    _content(expected)
    assert type(snapshot["schema"]) is int and snapshot["schema"] == 4
    assert type(snapshot["probe_contract"]) is int and snapshot["probe_contract"] == 1
    assert snapshot["project_id"] == str(identifier("project"))
    assert snapshot["project"]["data_type"] == "video"
    assert snapshot["task_ids"] == sorted(expected)
    assert sorted(task["id"] for task in snapshot["tasks"]) == sorted(expected)
    assert snapshot["references"] == {}
    assert snapshot["segments"] == [] and snapshot["quality_runs"] == []
    assert snapshot["targets"] == TARGETS and snapshot["options"] == OPTIONS
    assert len(snapshot["media"]) == 2
    assert {binding["task_id"] for binding in snapshot["media"]} == set(expected)
    assert {task["file_name"] for task in snapshot["tasks"]} == {"same.mp4"}
    for binding in snapshot["media"]:
        assert binding["role"] == "video" and binding["link_id"] is None
        source = binding["source"]
        assert source["media_type"] == "video"
        assert source["owner_kind"] == expected[binding["task_id"]]["owner_kind"]
        assert _receipt(source) == _receipt(expected[binding["task_id"]])


async def _seed_sources(db, *, actor, storage, raw_by_case):
    from sqlalchemy import select
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.dataset_asset import DatasetAssetLocation
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.task_original import TaskOriginal
    from app.services import managed_original_creation as creations, task_originals
    from app.services.dataset_assets import register_item_asset
    from app.services.exporting.lidar_inputs import row_value
    from tests.factory import build_tool_bindings

    project = Project(
        id=identifier("project"),
        display_id="P-REC-VIDEO",
        name="Offline video delivery",
        type_key="video-track",
        type_label="Video tracking",
        data_type="video",
        owner_id=actor.id,
        tool_bindings=build_tool_bindings(["car"]),
        video_sampling={"mode": "step", "frame_step": 2},
        video_collaboration={},
    )
    dataset = Dataset(
        id=identifier("dataset"),
        display_id="DS-REC-VIDEO",
        name="Offline video originals",
        created_by=actor.id,
        data_type="video",
    )
    db.add(project)
    db.add(dataset)
    tasks = {}
    for case in CASES:
        task = Task(
            id=identifier("task:" + case),
            project_id=project.id,
            display_id="T-REC-VIDEO-" + ("DS" if case == "dataset" else "TASK"),
            file_name="same.mp4",
            file_path="recovery/unallocated-video.mp4",
            file_type="video",
            status="uploading",
        )
        db.add(task)
        tasks[case] = task
    # Creation and TaskOriginal publication use separate durable transactions.
    await db.commit()
    evidence = {}
    for case in CASES:
        task, raw = tasks[case], raw_by_case[case]
        if case == "dataset":
            allocation = await creations.allocate(
                db,
                dataset_id=dataset.id,
                logical_path="same.mp4",
                origin="zip",
                max_bytes=len(raw),
                requested_by=actor.id,
                content_type="video/mp4",
                authority={"operation": "offline-recovery-video-fixture"},
            )
            await db.commit()
            with io.BytesIO(raw) as stream:
                created = await creations.create_stream(
                    db, allocation, stream, content_type="video/mp4"
                )
            item = DatasetItem(
                id=identifier("item"),
                dataset_id=dataset.id,
                file_name="same.mp4",
                file_path=allocation["object_key"],
                file_type="video",
                file_size=len(raw),
                content_hash=created.sha256,
                width=64,
                height=48,
            )
            db.add(item)
            await db.flush()
            owner = await register_item_asset(
                db, item, sha256=created.sha256, size_bytes=len(raw), etag=created.etag
            )
            await creations.attach(db, item, created.creation_id)
            task.dataset_item_id = item.id
            task.file_path = item.file_path
            locator = await db.scalar(
                select(DatasetAssetLocation).where(
                    DatasetAssetLocation.asset_id == owner.id,
                    DatasetAssetLocation.creation_id == created.creation_id,
                )
            )
            assert locator is not None
        else:
            allocation = await creations.allocate_task(
                db,
                project_id=project.id,
                task_id=task.id,
                logical_path="same.mp4",
                max_bytes=len(raw),
                requested_by=actor.id,
                content_type="video/mp4",
                authority={"operation": "offline-recovery-video-fixture"},
            )
            task.file_path = allocation["object_key"]
            await db.commit()
            storage.client.put_object(
                Bucket=allocation["staging_bucket"],
                Key=allocation["staging_key"],
                Body=raw,
                ContentType="video/mp4",
            )
            created = await creations.complete_direct(db, allocation)
            await task_originals.publish(db, task, allocation, created)
            owner = await db.get(
                TaskOriginal, created.creation_id, populate_existing=True
            )
            assert owner is not None
            locator = None
        task.status = "pending"
        db.add(
            Annotation(
                id=identifier("annotation:" + case),
                task_id=task.id,
                project_id=project.id,
                created_by=actor.id,
                class_name="car",
                tool_unit_id="bbox",
                source="manual",
                geometry={
                    "type": "video_track_bbox",
                    "track_id": "recovery-" + case,
                    "keyframes": [
                        {
                            "frame_index": frame,
                            "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                            "source": "manual",
                        }
                        for frame in (0, 5)
                    ],
                    "outside": [],
                },
            )
        )
        await db.commit()
        history = await db.get(
            ManagedOriginalCreation, created.creation_id, populate_existing=True
        )
        assert history is not None and history.status == "verified"
        evidence[str(task.id)] = {
            "creation": row_value(history),
            "owner": row_value(owner),
            "locator": row_value(locator) if locator is not None else None,
        }
    await db.commit()
    return project, evidence


async def _new_job(db, *, state, project, actor, storage, expected, source_facts):
    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.services.exporting import output_authority, video_jobs
    from urllib.parse import urlsplit

    request = SimpleNamespace(
        state=SimpleNamespace(api_key_id=None, api_key_scopes=None), headers={}
    )
    job = AsyncJob(
        id=job_identifier(state),
        kind="export",
        project_id=project.id,
        user_id=actor.id,
        status="pending",
        celery_task_id=attempt_identifier(state),
        result={},
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
                "source_facts": source_facts,
            },
        },
    )
    db.add(job)
    await db.flush()
    receipt = await video_jobs.capture_job(
        db,
        job,
        project,
        batch_id=None,
        targets=TARGETS,
        opts=OPTIONS,
        task_ids=[uuid.UUID(key) for key in sorted(expected)],
    )
    _assert_input(receipt.input_snapshot, expected)
    await db.commit()
    return receipt


async def _interrupt_after_pending_zip(db, receipt):
    from sqlalchemy import select
    from app.db.models.export_output import ExportOutput
    from app.services import async_job as jobs, export_media_bundles as bundles
    from app.services import export_outputs, export_storage
    from app.services.exporting import video_delivery, video_inputs
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

    context = await video_delivery.prepare(db, claimed, guard=guard)
    path = None
    try:
        path, count, size = await build_export_zip(
            db,
            claimed.project_id,
            batch_id=None,
            targets=TARGETS,
            include_attributes=True,
            video_frame_mode="keyframes",
            axis_frame="iso",
            format_options=OPTIONS,
            video_media=context,
            check_cancel=lambda: check_cancelled(db, claimed.job_id),
        )
        await guard(db)
        await bundles.check_claim(db, claimed)
        await db.commit()
        candidate = await export_outputs.create(
            db,
            project_id=claimed.project_id,
            job_id=claimed.job_id,
            media="video",
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
            current = await owned.scalar(
                select(ExportOutput)
                .where(ExportOutput.id == candidate.id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            assert current is not None and current.state == "pending"
            assert export_outputs.same(current, candidate)

        await publisher._io(
            db, claimed, lambda: export_storage.write(candidate, path), guard=zip_guard
        )
        await guard(db)
        await video_inputs.check(db, claimed.input_snapshot, lock=True)
        await db.commit()
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


def _archive(raw, *, bundle, metadata, expected):
    """Validate actual serializer output and return the exact immutable proof."""
    from app.services.exporting.video_media_paths import paths, sequence_paths

    relative, sequences = (
        paths(bundle.input_snapshot),
        sequence_paths(bundle.input_snapshot),
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        assert (
            len(names) == len(set(names))
            and len(relative) == len(set(relative.values())) == 2
        )
        manifest_raw = archive.read("manifest.json")
        manifest = json.loads(manifest_raw)
        assert manifest["targets"] == TARGETS
        assert manifest["expires_at"] == bundle.expires_at.isoformat()
        entries = {entry["task_id"]: entry for entry in manifest["videos"]}
        assert set(entries) == set(expected) and len(manifest["videos"]) == 2
        urls, receipts = {}, {}
        for task_id, entry in entries.items():
            task = uuid.UUID(task_id)
            assert (
                entry["rel_path"] == relative[task]
                and entry["sequence"] == sequences[task]
            )
            assert entry["expires_at"] == bundle.expires_at.isoformat()
            assert _receipt(entry) == _receipt(expected[task_id])
            assert entry["source_sha256"] == expected[task_id]["sha256"]
            assert entry["source_size_bytes"] == expected[task_id]["size_bytes"]
            proof = entry["metadata_receipt"]
            assert type(proof["probe_contract"]) is int and proof["probe_contract"] == 1
            assert _receipt(proof) == _receipt(expected[task_id])
            assert proof["video"] == entry["video_metadata"]
            for field, value in VIDEO.items():
                assert proof["video"][field] == value
            assert entry["grid_source_frames"] == [0, 2, 4]
            key = bundle.logical_paths["targets"]["mot"][relative[task]]
            assert bundle.inventory["objects"][key] == _receipt(expected[task_id])
            _historical_url(entry["presigned_url"], metadata, key)
            assert "videos/" + relative[task] not in names
            outputs = {
                entry["dir"].split("/", 1)[0]: entry for entry in entry["frame_outputs"]
            }
            assert (
                outputs["mot"]["start_number"] == 1
                and outputs["kitti"]["start_number"] == 0
            )
            mot = archive.read(f"mot/{sequences[task]}/gt/gt.txt").decode()
            kitti = archive.read(f"kitti/labels/{sequences[task]}.txt").decode()
            assert sorted({int(line.split(",")[0]) for line in mot.splitlines()}) == [
                1,
                2,
                3,
            ]
            assert sorted({int(line.split()[0]) for line in kitti.splitlines()}) == [
                0,
                1,
                2,
            ]
            receipts[task_id], urls[task_id] = proof, entry["presigned_url"]
        image_paths = {"videos/" + value for value in relative.values()}
        for name in ("aap_json/annotations.json", "video_json/annotations.json"):
            document = json.loads(archive.read(name))
            assert {task["file_path"] for task in document["tasks"]} == image_paths
    return {
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "metadata_receipts": receipts,
        "media_urls": urls,
    }


async def seed_video_exports(db, *, actor, storage):
    from app.db.models.async_job import AsyncJob
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput
    from app.services import export_media_bundles as bundles, export_storage
    from app.workers import lidar_export as publisher
    from sqlalchemy import select

    raw = make_videos()
    expected = expected_sources(raw)
    project, facts = await _seed_sources(
        db, actor=actor, storage=storage, raw_by_case=raw
    )
    for state in STATES:
        receipt = await _new_job(
            db,
            state=state,
            project=project,
            actor=actor,
            storage=storage,
            expected=expected,
            source_facts=facts,
        )
        if state == "building":
            await _interrupt_after_pending_zip(db, receipt)
        else:
            await publisher.run_owned(
                db,
                project_id=str(project.id),
                batch_id=None,
                task_ids=[uuid.UUID(key) for key in sorted(expected)],
                targets=TARGETS,
                opts=OPTIONS,
                async_job_id=str(receipt.job_id),
                celery_task_id=attempt_identifier(state),
                media_kind="video",
            )
        await db.commit()
        job = await db.get(AsyncJob, receipt.job_id, populate_existing=True)
        row = await db.get(ExportMediaBundle, receipt.id, populate_existing=True)
        output = await db.get(
            ExportOutput, receipt.zip_output_id, populate_existing=True
        )
        assert job is not None and row is not None and output is not None
        assert job.status == ("completed" if state == "ready" else "running")
        assert row.state == state and row.lease_token is not None
        assert (
            output.state == ("ready" if state == "ready" else "pending")
            and output.media == "video"
        )
        frozen = bundles.snapshot(row)
        _assert_input(frozen.input_snapshot, expected)
        owner = export_storage.resolve(output)
        archive = _read_exact(
            storage, owner.bucket, owner.object_key, output.sha256, output.size_bytes
        )
        metadata = deepcopy(job.payload[FIXTURE_KEY])
        proof = _archive(archive, bundle=frozen, metadata=metadata, expected=expected)
        metadata["delivery"] = {
            **proof,
            "input_digest": frozen.input_digest,
            "zip_sha256": output.sha256,
            "zip_size_bytes": output.size_bytes,
            "file_count": output.file_count,
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "lease_token": str(row.lease_token),
            "lease_expires_at": row.lease_expires_at.isoformat(),
            "download_url": job.result.get("download_url"),
        }
        job.payload = {**job.payload, FIXTURE_KEY: metadata}
        caches = list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == output.id)
            )
        )
        assert len(caches) == (1 if state == "ready" else 0)
        if state == "ready":
            assert job.result["cache_hit"] is False
        await db.commit()


async def _verify_sources(db, source_facts, expected, storage):
    """Check both actual canonical owners and all historical Creation facts, no I/O."""
    from sqlalchemy import select
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.task import Task
    from app.db.models.task_original import TaskOriginal
    from app.services.exporting.lidar_inputs import row_value

    assert set(source_facts) == set(expected)
    for task_id, facts in source_facts.items():
        creation = await db.get(
            ManagedOriginalCreation, uuid.UUID(facts["creation"]["id"])
        )
        assert creation is not None and row_value(creation) == facts["creation"]
        assert (
            creation.status == "verified"
            and creation.sha256 == expected[task_id]["sha256"]
        )
        assert creation.size_bytes == expected[task_id]["size_bytes"]
        assert creation.receipt is not None and creation.verification is not None
        assert creation.settled_at is not None
        task = await db.get(Task, uuid.UUID(task_id))
        assert task is not None and task.status == "pending"
        assert task.project_id == identifier("project") and task.file_type == "video"
        assert task.file_path == creation.object_key and task.file_name == "same.mp4"
        if expected[task_id]["owner_kind"] == "dataset_asset":
            assert (
                creation.owner_kind == "dataset"
                and creation.dataset_id == identifier("dataset")
            )
            assert (
                task.dataset_item_id == identifier("item") and task.original_id is None
            )
            item = await db.get(DatasetItem, task.dataset_item_id)
            assert item is not None and item.file_path == creation.object_key
            owner = await db.get(DatasetAsset, uuid.UUID(facts["owner"]["id"]))
            assert owner is not None and row_value(owner) == facts["owner"]
            assert owner.dataset_item_id == item.id and owner.media_type == "video"
            assert (
                owner.verification_state == "verified"
                and owner.checksum_algorithm == "sha256"
            )
            assert (
                owner.checksum_value == creation.sha256
                and owner.size_bytes == creation.size_bytes
            )
            locator = await db.scalar(
                select(DatasetAssetLocation).where(
                    DatasetAssetLocation.asset_id == owner.id,
                    DatasetAssetLocation.creation_id == creation.id,
                )
            )
            assert locator is not None and locator.bucket == storage.datasets_bucket
            assert locator.bucket != facts["locator"]["bucket"]
            assert (
                locator.object_key == creation.object_key
                and locator.role == "managed_original"
            )
            assert locator.reference_state == "present"
            assert {k: v for k, v in row_value(locator).items() if k != "bucket"} == {
                k: v for k, v in facts["locator"].items() if k != "bucket"
            }
        else:
            assert creation.owner_kind == "task" and creation.task_id == task.id
            assert task.original_id == creation.id and task.dataset_item_id is None
            owner = await db.get(TaskOriginal, creation.id)
            assert owner is not None and owner.bucket == storage.bucket
            assert owner.id == owner.creation_id == creation.id
            assert (
                owner.sha256 == creation.sha256
                and owner.size_bytes == creation.size_bytes
            )
            assert {
                k: v
                for k, v in row_value(owner).items()
                if k not in {"bucket", "storage_identity"}
            } == {
                k: v
                for k, v in facts["owner"].items()
                if k not in {"bucket", "storage_identity"}
            }
            assert owner.storage_identity != facts["owner"]["storage_identity"]


async def verify_video_exports(db, *, storage):
    """Read exact saved delivery objects after restore; no current original fallback."""
    from sqlalchemy import select
    from app.db.models.async_job import AsyncJob
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput
    from app.services import (
        export_media_bundles as bundles,
        export_media_storage as physical,
        export_storage,
    )
    from app.services.exporting.video_media_paths import logical_sources

    count, source_facts = 0, None
    for state in STATES:
        job = await db.get(AsyncJob, job_identifier(state))
        assert job is not None and job.status == (
            "completed" if state == "ready" else "running"
        )
        assert job.celery_task_id == attempt_identifier(state)
        assert (job.completed_at is not None) == (state == "ready")
        metadata = job.payload[FIXTURE_KEY]
        assert (
            metadata["schema"] == 1
            and metadata["qualification"] == QUALIFICATION
            and metadata["state"] == state
        )
        expected, saved = metadata["expected"], metadata["delivery"]
        raw_by_task = _content(expected)
        if source_facts is None:
            source_facts = metadata["source_facts"]
            await _verify_sources(db, source_facts, expected, storage)
        else:
            assert metadata["source_facts"] == source_facts
        proof = job.payload["video_media"]
        assert type(proof["schema"]) is int and proof["schema"] == 4
        row = await db.get(ExportMediaBundle, uuid.UUID(proof["bundle_id"]))
        assert row is not None and row.state == state and row.job_id == job.id
        assert row.input_schema == 4 and row.lease_token is not None
        assert (row.published_at is not None) == (state == "ready")
        assert row.expires_at - row.created_at == timedelta(days=7)
        assert (
            row.created_at.isoformat() == saved["created_at"]
            and row.expires_at.isoformat() == saved["expires_at"]
        )
        assert (
            str(row.lease_token) == saved["lease_token"]
            and row.lease_expires_at.isoformat() == saved["lease_expires_at"]
        )
        frozen = bundles.snapshot(row)
        assert frozen.input_digest == proof["input_digest"] == saved["input_digest"]
        _assert_input(frozen.input_snapshot, expected)
        owner = physical.resolve(frozen)
        assert (
            owner.bucket == storage.export_bucket
            and owner.bucket != metadata["source_export_bucket"]
        )
        physical.verify_inventory(storage.client, frozen)
        objects = bundles.source_objects(frozen, logical_sources(frozen.input_snapshot))
        for binding in frozen.input_snapshot["media"]:
            key, source = objects[binding["source_key"]], binding["source"]
            assert row.inventory["objects"][key] == _receipt(
                expected[binding["task_id"]]
            )
            actual = _read_exact(
                storage, owner.bucket, key, source["sha256"], source["size_bytes"]
            )
            assert actual == raw_by_task[binding["task_id"]]
            count += 1
        output = await db.get(ExportOutput, row.zip_output_id)
        assert output is not None and output.media == "video"
        assert output.state == ("ready" if state == "ready" else "pending")
        assert (
            output.created_at == row.created_at and output.expires_at == row.expires_at
        )
        assert (
            output.sha256 == saved["zip_sha256"]
            and output.size_bytes == saved["zip_size_bytes"]
        )
        assert output.file_count == saved["file_count"]
        validate_media_pairs([row], [output])
        zip_owner = export_storage.resolve(output)
        assert zip_owner.bucket == owner.bucket
        archive = _read_exact(
            storage,
            owner.bucket,
            zip_owner.object_key,
            output.sha256,
            output.size_bytes,
        )
        actual = _archive(archive, bundle=frozen, metadata=metadata, expected=expected)
        assert actual == {
            name: saved[name]
            for name in ("manifest_sha256", "metadata_receipts", "media_urls")
        }
        assert not list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == output.id)
            )
        )
        assert job.result.get("download_url") == saved["download_url"]
        if state == "ready":
            assert job.result["expires_at"] == row.expires_at.isoformat()
            assert (
                job.result["object_key"] == zip_owner.object_key
                and job.result["size_bytes"] == output.size_bytes
            )
            _historical_url(job.result["download_url"], metadata, zip_owner.object_key)
        else:
            assert not job.result.get("download_url")
    return {
        "pinned_video_export_media_bundles": 2,
        "verified_video_export_media_objects": count,
        "verified_video_export_zip_objects": 2,
        "verified_video_export_probe_receipts": count,
        "invalidated_schema4_video_export_cache_indexes": 1,
        "video_export_inline_completed_attempts": 1,
        "video_export_interrupted_building_attempts": 1,
        "video_export_http_or_celery_dispatch_qualified": False,
        "video_export_embedded_urls_remapped": False,
        "video_export_original_fallback_used": False,
        "video_export_delivery_after_restore": "requires-new-bundle-and-zip",
    }
