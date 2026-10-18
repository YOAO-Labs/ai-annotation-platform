"""Native image worker leases, publication, backfill and reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest
from sqlalchemy import text

from aap_assets_images.policy import ImagePolicy
from app.db.models.asset_image import AssetImageJob
from app.db.models.dataset import DatasetItem
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from app.db.models.task import Task
from tests.factory import create_project
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_RESOLVE = text(
    "SELECT aap_assets_v1._image_resolve_owner("
    "'dataset_item', CAST(:item_id AS uuid), false)"
)
_RESOLVE_TASK = text(
    "SELECT aap_assets_v1._image_resolve_owner('task', CAST(:task_id AS uuid), false)"
)
_OBSERVE_TASK = text(
    "SELECT aap_labeling_v1.observe_image_task_source("
    "CAST(:task_id AS uuid), CAST(:expected_snapshot AS text), "
    "CAST(:identity AS jsonb))"
)
_ENQUEUE = text(
    "SELECT aap_assets_v1._image_enqueue("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:source AS jsonb), CAST(:spec AS jsonb), CAST(:force AS boolean))"
)
_CLAIM = text(
    "SELECT aap_assets_v1.claim_image_media("
    "CAST(:worker_id AS uuid), CAST(:job_id AS uuid), :lease_seconds, "
    "ARRAY[:kind]::text[])"
)
_HEARTBEAT = text(
    "SELECT aap_assets_v1.heartbeat_image_media("
    "CAST(:job_id AS uuid), CAST(:generation AS bigint), "
    "CAST(:lease_token AS uuid), :lease_seconds)"
)
_FINISH = text(
    "SELECT aap_assets_v1.finish_image_media("
    "CAST(:job_id AS uuid), CAST(:generation AS bigint), "
    "CAST(:lease_token AS uuid), CAST(:result AS jsonb))"
)
_FAIL = text(
    "SELECT aap_assets_v1.fail_image_media("
    "CAST(:job_id AS uuid), CAST(:generation AS bigint), "
    "CAST(:lease_token AS uuid), :error_code, :error_detail)"
)
_ADVANCE = text(
    "SELECT aap_assets_v1.advance_image_backfill("
    "CAST(:job_id AS uuid), CAST(:generation AS bigint), "
    "CAST(:lease_token AS uuid), :batch_size, CAST(:observations AS jsonb))"
)
_RECONCILE = text(
    "SELECT aap_assets_v1.reconcile_image_media("
    "CAST(:candidates AS jsonb), :batch_size)"
)


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _data(reply: dict) -> dict:
    assert set(reply) == {"schema", "ok", "data"}, reply
    assert reply["schema"] == "aap-assets-images/v1"
    assert reply["ok"] is True, reply.get("error")
    return reply["data"]


def _error(reply: dict, code: str) -> None:
    assert reply == {
        "schema": "aap-assets-images/v1",
        "ok": False,
        "error": {"code": code},
    }


def _identity(size: int = 128) -> dict:
    return {"etag": "registered-image-etag", "byte_size": size, "version_id": None}


async def _seed_image(
    db_session, *, actor_id, dataset_id, width=None, height=None, bind_identity=True
):
    item_id = uuid.uuid4()
    db_session.add(
        DatasetItem(
            id=item_id,
            dataset_id=dataset_id,
            file_name=f"image-{item_id.hex[:8]}.png",
            file_path=f"images/{item_id}.png",
            file_type="image",
            file_size=128,
            width=width,
            height=height,
            metadata_={},
            media_source_generation=1,
            media_materialized_generation=0,
            media_object_identity=_identity() if bind_identity else None,
            media_source_drifted=False,
        )
    )
    await db_session.flush()
    return item_id


async def _source(db_session, item_id):
    reply = _reply(await db_session.scalar(_RESOLVE, {"item_id": item_id}))
    return _data(reply)


async def _enqueue(db_session, source, spec, *, force=False):
    reply = _reply(
        await db_session.scalar(
            _ENQUEUE,
            {
                "operation_id": None,
                "actor_id": None,
                "source": None if source is None else json.dumps(source),
                "spec": json.dumps(spec),
                "force": force,
            },
        )
    )
    data = _data(reply)
    return uuid.UUID(data["work"]["job_id"]), data


async def _claim(db_session, *, job_id, kind, lease_seconds=30):
    reply = _reply(
        await db_session.scalar(
            _CLAIM,
            {
                "worker_id": uuid.uuid4(),
                "job_id": job_id,
                "kind": kind,
                "lease_seconds": lease_seconds,
            },
        )
    )
    return _data(reply)["claim"]


def _worker_result(kind, source, **extra):
    return {
        "kind": kind,
        "source_identity": source["object_identity"],
        **extra,
    }


def _spec(kind, source, *, policy=None, **extra):
    return {
        "kind": kind,
        "expected_source_snapshot": source["source_snapshot"],
        "policy": policy or ImagePolicy().model_dump(mode="json"),
        **extra,
    }


async def test_dimensions_finish_persists_facts_and_chains_a_small_preview(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(db_session, actor_id=actor_id, dataset_id=dataset_id)
    source = await _source(db_session, item_id)
    spec = _spec("dimensions", source, generate_preview=True)
    job_id, _ = await _enqueue(db_session, source, spec)
    claim = await _claim(db_session, job_id=job_id, kind="dimensions")
    assert claim["source"]["source_snapshot"] == source["source_snapshot"]
    assert claim["output_prefix"] is None
    lease_expiry = datetime.fromisoformat(claim["lease_expires_at"])
    assert lease_expiry > datetime.now(UTC) + timedelta(seconds=2000)
    heartbeat = _data(
        _reply(
            await db_session.scalar(
                _HEARTBEAT,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "lease_seconds": 30,
                },
            )
        )
    )
    assert heartbeat["active"] is True
    assert datetime.fromisoformat(heartbeat["lease_expires_at"]) > lease_expiry

    result = _worker_result("dimensions", source, width=320, height=200)
    finished = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(result),
                },
            )
        )
    )
    assert finished == {
        "job_id": str(job_id),
        "generation": claim["generation"],
        "outcome": "published",
    }
    item = await db_session.get(DatasetItem, item_id)
    assert item is not None
    await db_session.refresh(item)
    assert (item.width, item.height, item.media_materialized_generation) == (
        320,
        200,
        1,
    )
    parent = await db_session.get(AssetImageJob, job_id)
    assert parent is not None and parent.status == "ready"
    assert parent.result == result

    # The same lease cannot publish twice after response loss.
    replay = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(result),
                },
            )
        )
    )
    assert replay["outcome"] == "replayed"
    children = (
        await db_session.scalars(
            text(
                "SELECT job_id FROM public.asset_image_jobs "
                "WHERE owner_kind='dataset_item' AND owner_id=:item_id "
                "AND kind='thumbnail'"
            ),
            {"item_id": item_id},
        )
    ).all()
    assert len(children) == 1


async def test_task_dimensions_finish_chains_preview_and_projects_dimensions(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    project = await create_project(db_session, owner_id=actor_id)
    task_id = uuid.uuid4()
    db_session.add(
        Task(
            id=task_id,
            project_id=project.id,
            display_id=f"T-IMG-{task_id.hex[:12]}",
            file_name="task-image.png",
            file_path=f"task-images/{task_id}.png",
            file_type="image",
            status="pending",
            assignee_id=actor_id,
            media_source_generation=1,
            media_object_identity=None,
            media_source_drifted=False,
        )
    )
    await db_session.flush()
    source = _data(_reply(await db_session.scalar(_RESOLVE_TASK, {"task_id": task_id})))
    identity = _identity()
    observed = _reply(
        await db_session.scalar(
            _OBSERVE_TASK,
            {
                "task_id": task_id,
                "expected_snapshot": source["source_snapshot"],
                "identity": json.dumps(identity),
            },
        )
    )
    assert observed["schema"] == "aap-labeling-image-source/v1"
    assert observed["ok"] is True
    source = _data(_reply(await db_session.scalar(_RESOLVE_TASK, {"task_id": task_id})))
    assert source["width"] is None and source["height"] is None
    job_id, _ = await _enqueue(
        db_session, source, _spec("dimensions", source, generate_preview=True)
    )
    claim = await _claim(db_session, job_id=job_id, kind="dimensions")
    published = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(
                        _worker_result("dimensions", source, width=40, height=30)
                    ),
                },
            )
        )
    )
    assert published["outcome"] == "published"
    source_after = _data(
        _reply(await db_session.scalar(_RESOLVE_TASK, {"task_id": task_id}))
    )
    assert (source_after["width"], source_after["height"]) == (40, 30)
    child = (
        await db_session.execute(
            text(
                "SELECT kind, status FROM public.asset_image_jobs "
                "WHERE owner_kind='task' AND owner_id=:task_id "
                "AND kind='thumbnail'"
            ),
            {"task_id": task_id},
        )
    ).one()
    assert child == ("thumbnail", "pending")


async def test_thumbnail_rejects_a_key_outside_its_attempt_before_publication(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=320, height=200
    )
    source = await _source(db_session, item_id)
    job_id, _ = await _enqueue(db_session, source, _spec("thumbnail", source))
    claim = await _claim(db_session, job_id=job_id, kind="thumbnail")
    artifact = {
        "key": f"image-assets/{uuid.uuid4()}/g1/a{uuid.uuid4()}/thumbnail.webp",
        "byte_size": 12,
        "sha256": "b" * 64,
    }
    bad = _worker_result(
        "thumbnail",
        source,
        source_sha256="a" * 64,
        width=320,
        height=200,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        artifact=artifact,
    )
    rejected = _reply(
        await db_session.scalar(
            _FINISH,
            {
                "job_id": job_id,
                "generation": claim["generation"],
                "lease_token": claim["lease_token"],
                "result": json.dumps(bad),
            },
        )
    )
    _error(rejected, "invalid_request")
    item = await db_session.get(DatasetItem, item_id)
    assert item is not None and item.thumbnail_path is None
    job = await db_session.get(AssetImageJob, job_id)
    assert job is not None and job.status == "running"

    valid = dict(bad)
    valid["artifact"] = {
        **artifact,
        "key": claim["output_prefix"] + "thumbnail.webp",
    }
    published = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(valid),
                },
            )
        )
    )
    assert published["outcome"] == "published"
    item = await db_session.get(DatasetItem, item_id)
    assert item is not None
    await db_session.refresh(item)
    assert item.thumbnail_path == claim["output_prefix"] + "thumbnail.webp"
    assert item.blurhash == valid["blurhash"]


async def test_source_generation_change_fences_a_claimed_thumbnail(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=320, height=200
    )
    source = await _source(db_session, item_id)
    job_id, _ = await _enqueue(db_session, source, _spec("thumbnail", source))
    claim = await _claim(db_session, job_id=job_id, kind="thumbnail")
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_source_generation=2, "
            "media_object_identity=NULL WHERE id=:item_id"
        ),
        {"item_id": item_id},
    )
    result = _worker_result(
        "thumbnail",
        source,
        source_sha256="a" * 64,
        width=320,
        height=200,
        blurhash="LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        artifact={
            "key": claim["output_prefix"] + "thumbnail.webp",
            "byte_size": 12,
            "sha256": "b" * 64,
        },
    )
    outcome = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(result),
                },
            )
        )
    )
    assert outcome["outcome"] == "source_changed"
    item = await db_session.get(DatasetItem, item_id)
    job = await db_session.get(AssetImageJob, job_id)
    assert item is not None and item.file_path == source["file_path"]
    assert item.thumbnail_path is None
    assert job is not None and job.status == "canceled"


async def test_pyramid_finish_validates_the_complete_grid_and_publishes_atomically(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
    )
    source = await _source(db_session, item_id)
    policy = ImagePolicy().model_dump(mode="json")
    job_id, _ = await _enqueue(
        db_session, source, _spec("pyramid", source, policy=policy)
    )
    claim = await _claim(db_session, job_id=job_id, kind="pyramid")
    prefix = claim["output_prefix"]
    asset_id = uuid.UUID(claim["pyramid_asset_id"])
    public_generation = claim["pyramid_generation"]
    heartbeat = _data(
        _reply(
            await db_session.scalar(
                _HEARTBEAT,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "lease_seconds": 30,
                },
            )
        )
    )
    assert heartbeat["active"] is True
    manifest = {
        "schema": "aap-image-pyramid/v1",
        "generation": public_generation,
        "sourceFingerprint": "sha256:" + "a" * 64,
        "normalizationVersion": policy["normalization_version"],
        "width": 1,
        "height": 1,
        "tileSize": 512,
        "overlap": 1,
        "format": "webp",
        "levels": [
            {
                "level": 0,
                "scaleFactor": 1,
                "width": 1,
                "height": 1,
                "columns": 1,
                "rows": 1,
            }
        ],
        "overview": {
            "width": 1,
            "height": 1,
            "contentDigest": "sha256:" + "c" * 64,
        },
    }
    result = _worker_result(
        "pyramid",
        source,
        source_sha256="a" * 64,
        manifest=manifest,
        manifest_artifact={
            "key": prefix + "manifest.json",
            "byte_size": 10,
            "sha256": "d" * 64,
        },
        overview_artifact={
            "key": prefix + "overview.webp",
            "byte_size": 11,
            "sha256": "c" * 64,
        },
        tile_artifacts=[
            {
                "level": 0,
                "x": 0,
                "y": 0,
                "artifact": {
                    "key": prefix + "tiles/0/0/0.webp",
                    "byte_size": 12,
                    "sha256": "e" * 64,
                },
            }
        ],
        tile_count=1,
        retained_bytes=33,
    )
    bad = json.loads(json.dumps(result))
    bad["tile_artifacts"][0]["artifact"]["key"] = prefix + "tiles/0/1/0.webp"
    rejected = _reply(
        await db_session.scalar(
            _FINISH,
            {
                "job_id": job_id,
                "generation": claim["generation"],
                "lease_token": claim["lease_token"],
                "result": json.dumps(bad),
            },
        )
    )
    _error(rejected, "invalid_request")
    asset = await db_session.get(ImagePyramidAsset, asset_id)
    assert asset is not None and asset.active_generation is None

    published = _data(
        _reply(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "result": json.dumps(result),
                },
            )
        )
    )
    assert published["outcome"] == "published"
    asset = await db_session.get(ImagePyramidAsset, asset_id)
    generation = await db_session.get(
        ImagePyramidGeneration, (asset_id, public_generation)
    )
    assert asset is not None
    await db_session.refresh(asset)
    assert asset.active_generation == public_generation
    assert asset.building_generation is None
    assert generation is not None and generation.status == "ready"
    assert generation.manifest == manifest
    assert generation.retained_bytes == 33

    refresh_id, _ = await _enqueue(
        db_session, source, _spec("pyramid", source, policy=policy), force=True
    )
    refresh_claim = await _claim(db_session, job_id=refresh_id, kind="pyramid")
    failed = _data(
        _reply(
            await db_session.scalar(
                _FAIL,
                {
                    "job_id": refresh_id,
                    "generation": refresh_claim["generation"],
                    "lease_token": refresh_claim["lease_token"],
                    "error_code": "decode_failed",
                    "error_detail": "fixture decode failure",
                },
            )
        )
    )
    assert failed["outcome"] == "failed"
    asset = await db_session.get(ImagePyramidAsset, asset_id)
    refresh_generation = await db_session.get(
        ImagePyramidGeneration,
        (asset_id, refresh_claim["pyramid_generation"]),
    )
    assert asset is not None
    await db_session.refresh(asset)
    assert asset.active_generation == public_generation
    assert asset.building_generation is None
    assert refresh_generation is not None and refresh_generation.status == "failed"

    orphan = {
        "asset_id": str(asset_id),
        "generation": refresh_claim["pyramid_generation"] + 1,
        "last_modified": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
    }
    reconciled = _data(
        _reply(
            await db_session.scalar(
                _RECONCILE,
                {"candidates": json.dumps([orphan]), "batch_size": 10},
            )
        )
    )
    assert reconciled["queued_orphans"] == 1
    prefix = f"image-pyramids/{asset_id}/g{refresh_claim['pyramid_generation'] + 1}/"
    assert (
        await db_session.scalar(
            text("SELECT prefix FROM public.asset_image_garbage WHERE prefix=:prefix"),
            {"prefix": prefix},
        )
        == prefix
    )


async def test_expired_image_lease_requeues_then_fails_after_three_attempts(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(db_session, actor_id=actor_id, dataset_id=dataset_id)
    source = await _source(db_session, item_id)
    job_id, _ = await _enqueue(db_session, source, _spec("dimensions", source))
    claim = await _claim(db_session, job_id=job_id, kind="dimensions")
    await db_session.execute(
        text(
            "UPDATE public.asset_image_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job_id"
        ),
        {"job_id": job_id},
    )
    first = _data(
        _reply(
            await db_session.scalar(_RECONCILE, {"candidates": "[]", "batch_size": 10})
        )
    )
    assert first["expired_jobs"] == 1
    assert first["requeued_jobs"] == 1
    job = await db_session.get(AssetImageJob, job_id)
    assert job is not None and job.status == "pending"
    assert job.attempt_count == 1

    second_claim = await _claim(db_session, job_id=job_id, kind="dimensions")
    assert second_claim["generation"] == claim["generation"]
    await db_session.execute(
        text(
            "UPDATE public.asset_image_jobs SET attempt_count=3, "
            "lease_expires_at=clock_timestamp()-interval '1 second' WHERE job_id=:job_id"
        ),
        {"job_id": job_id},
    )
    final = _data(
        _reply(
            await db_session.scalar(_RECONCILE, {"candidates": "[]", "batch_size": 10})
        )
    )
    assert final["expired_jobs"] == 1
    assert final["failed_jobs"] == 1
    job = await db_session.get(AssetImageJob, job_id)
    assert job is not None
    await db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_code == "timeout"


async def test_image_backfill_observation_commit_is_atomic_and_replayable(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        bind_identity=False,
    )
    cutoff = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    spec = {
        "kind": "image_backfill",
        "selection": {
            "owner_kind": "dataset_item",
            "dataset_id": str(dataset_id),
            "project_id": None,
            "owner_ids": [],
            "all_owners": False,
            "purpose": "dimensions",
            "only_missing": True,
            "accepted_before": cutoff,
        },
        "policy": ImagePolicy().model_dump(mode="json"),
    }
    job_id, _ = await _enqueue(db_session, None, spec)
    claim = await _claim(db_session, job_id=job_id, kind="image_backfill")
    prepared = _data(
        _reply(
            await db_session.scalar(
                _ADVANCE,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "batch_size": 10,
                    "observations": None,
                },
            )
        )
    )
    assert prepared["needs_observation"] is True
    assert prepared["complete"] is False
    assert len(prepared["candidates"]) == 1
    candidate = prepared["candidates"][0]
    assert candidate["owner"]["id"] == str(item_id)
    observations = {
        "batch_token": prepared["batch_token"],
        "items": [
            {
                "owner": candidate["owner"],
                "source_snapshot": candidate["source_snapshot"],
                "object_identity": _identity(),
                "error_code": None,
            }
        ],
    }
    args = {
        "job_id": job_id,
        "generation": claim["generation"],
        "lease_token": claim["lease_token"],
        "batch_size": 10,
        "observations": json.dumps(observations),
    }
    committed = _data(_reply(await db_session.scalar(_ADVANCE, args)))
    assert committed["scheduled"] == 1
    assert committed["skipped"] == 0
    assert committed["complete"] is True
    assert (
        _reply(
            await db_session.scalar(
                text(
                    "SELECT media_object_identity FROM public.dataset_items WHERE id=:item_id"
                ),
                {"item_id": item_id},
            )
        )
        == _identity()
    )
    replay = _data(_reply(await db_session.scalar(_ADVANCE, args)))
    assert replay == committed
    child_ids = (
        await db_session.scalars(
            text(
                "SELECT job_id FROM public.asset_image_jobs "
                "WHERE owner_kind='dataset_item' AND owner_id=:item_id "
                "AND kind='dimensions'"
            ),
            {"item_id": item_id},
        )
    ).all()
    assert len(child_ids) == 1


async def test_image_backfill_records_source_drift_and_skips_only_that_item(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(db_session, actor_id=actor_id, dataset_id=dataset_id)
    spec = {
        "kind": "image_backfill",
        "selection": {
            "owner_kind": "dataset_item",
            "dataset_id": str(dataset_id),
            "project_id": None,
            "owner_ids": [],
            "all_owners": False,
            "purpose": "dimensions",
            "only_missing": True,
            "accepted_before": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
        "policy": ImagePolicy().model_dump(mode="json"),
    }
    job_id, _ = await _enqueue(db_session, None, spec)
    claim = await _claim(db_session, job_id=job_id, kind="image_backfill")
    prepared = _data(
        _reply(
            await db_session.scalar(
                _ADVANCE,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "batch_size": 10,
                    "observations": None,
                },
            )
        )
    )
    candidate = prepared["candidates"][0]
    changed_identity = {**_identity(), "etag": "changed-image-etag"}
    committed = _data(
        _reply(
            await db_session.scalar(
                _ADVANCE,
                {
                    "job_id": job_id,
                    "generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "batch_size": 10,
                    "observations": json.dumps(
                        {
                            "batch_token": prepared["batch_token"],
                            "items": [
                                {
                                    "owner": candidate["owner"],
                                    "source_snapshot": candidate["source_snapshot"],
                                    "object_identity": changed_identity,
                                    "error_code": None,
                                }
                            ],
                        }
                    ),
                },
            )
        )
    )
    assert committed["complete"] is True
    assert committed["scheduled"] == 0
    assert committed["skipped"] == 1
    item = await db_session.get(DatasetItem, item_id)
    assert item is not None
    await db_session.refresh(item)
    assert item.media_source_drifted is True
    assert item.media_object_identity == changed_identity
    assert item.media_source_generation == 2
    child_count = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.asset_image_jobs "
            "WHERE owner_kind='dataset_item' AND owner_id=:item_id"
        ),
        {"item_id": item_id},
    )
    assert child_count == 0
