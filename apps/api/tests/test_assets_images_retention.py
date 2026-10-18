"""Native image-generation URL retention and retirement regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from aap_assets_images.policy import ImagePolicy
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset
from tests.test_assets_images_execution import (
    _FINISH,
    _RECONCILE,
    _claim,
    _data,
    _enqueue,
    _reply,
    _seed_image,
    _source,
    _spec,
    _worker_result,
)

pytestmark = pytest.mark.asyncio

_READ_GENERATION = text(
    "SELECT aap_assets_v1.read_image_generation(CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:item_id AS uuid), "
    "CAST(:generation AS integer), CAST(:profile_version AS text))"
)
_CLAIM_GARBAGE = text(
    "SELECT aap_assets_v1.claim_image_garbage(CAST(:worker_id AS uuid), "
    ":lease_seconds, :batch_size)"
)


def _legacy_identity(source: dict) -> str:
    identity = source["object_identity"]
    value = f"etag:{identity['etag']}:bytes:{identity['byte_size']}"
    version_id = identity.get("version_id")
    if version_id:
        value += f":version:{version_id}"
    return value


def _ready_generation(
    asset_id,
    generation: int,
    source: dict,
    *,
    updated_at: datetime,
    last_accessed_at: datetime | None = None,
) -> ImagePyramidGeneration:
    generation_prefix = f"image-pyramids/{asset_id}/g{generation}/"
    prefix = generation_prefix + f"a{uuid4()}/"
    return ImagePyramidGeneration(
        asset_id=asset_id,
        generation=generation,
        job_id=None,
        job_generation=None,
        source_generation=source["source_generation"],
        source_snapshot=source["source_snapshot"],
        output_prefix=prefix,
        manifest={"schema": "aap-image-pyramid/v1", "generation": generation},
        policy=ImagePolicy().model_dump(mode="json"),
        source_identity=_legacy_identity(source),
        source_fingerprint="sha256:" + "a" * 64,
        status="ready",
        width=1,
        height=1,
        format="webp",
        normalization_version="exif-autorotate-srgb-v1",
        manifest_key=prefix + "manifest.json",
        manifest_digest="sha256:" + "b" * 64,
        overview_key=prefix + "overview.webp",
        overview_width=1,
        overview_height=1,
        overview_digest="sha256:" + "c" * 64,
        tile_count=1,
        retained_bytes=12,
        last_accessed_at=last_accessed_at,
        updated_at=updated_at,
    )


async def _read_generation(
    db_session, actor_id, item_id, generation: int | None
) -> dict:
    return _data(
        _reply(
            await db_session.scalar(
                _READ_GENERATION,
                {
                    "actor_id": actor_id,
                    "task_id": None,
                    "item_id": item_id,
                    "generation": generation,
                    "profile_version": "pyramid-v1",
                },
            )
        )
    )


async def _claim_garbage(db_session) -> dict:
    return _data(
        _reply(
            await db_session.scalar(
                _CLAIM_GARBAGE,
                {"worker_id": uuid4(), "lease_seconds": 300, "batch_size": 10},
            )
        )
    )


async def _publish_pyramid(db_session, source: dict, *, force: bool = False) -> dict:
    policy = ImagePolicy().model_dump(mode="json")
    job_id, _admission = await _enqueue(
        db_session, source, _spec("pyramid", source, policy=policy), force=force
    )
    claim = await _claim(db_session, job_id=job_id, kind="pyramid")
    public_generation = claim["pyramid_generation"]
    prefix = claim["output_prefix"]
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
            "sha256": "b" * 64,
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
                    "sha256": "d" * 64,
                },
            }
        ],
        tile_count=1,
        retained_bytes=33,
    )
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
    assert finished["outcome"] == "published"
    return claim


async def test_explicit_ready_generation_access_is_rate_limited_and_extends_retention(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    source = await _source(db_session, item_id)
    asset_id = uuid4()
    old_update = datetime.now(UTC) - timedelta(days=3)
    asset = ImagePyramidAsset(
        id=asset_id,
        dataset_item_id=item_id,
        profile_version="pyramid-v1",
        active_generation=2,
    )
    db_session.add_all(
        [
            asset,
            _ready_generation(asset_id, 1, source, updated_at=old_update),
            _ready_generation(asset_id, 2, source, updated_at=old_update),
        ]
    )
    await db_session.flush()
    prefix = f"image-pyramids/{asset_id}/g1/"
    await db_session.execute(
        text(
            "INSERT INTO public.asset_image_garbage(prefix,reason,not_before) "
            "VALUES (:prefix,'generation_retired',clock_timestamp()+interval '1 hour')"
        ),
        {"prefix": prefix},
    )

    read_started = await db_session.scalar(text("SELECT clock_timestamp()"))
    explicit = await _read_generation(db_session, actor_id, item_id, 1)
    assert explicit["pyramid"]["status"] == "ready"
    touched, retained_until = (
        await db_session.execute(
            text(
                "SELECT generation.last_accessed_at, garbage.not_before "
                "FROM public.image_pyramid_generations AS generation "
                "JOIN public.asset_image_garbage AS garbage "
                "ON garbage.prefix=:prefix "
                "WHERE generation.asset_id=:asset_id AND generation.generation=1"
            ),
            {"asset_id": asset_id, "prefix": prefix},
        )
    ).one()
    assert touched is not None and touched >= read_started
    assert retained_until >= touched + timedelta(hours=24)

    recent_access = (
        await db_session.execute(
            text(
                "UPDATE public.image_pyramid_generations "
                "SET last_accessed_at=clock_timestamp()-interval '5 minutes' "
                "WHERE asset_id=:asset_id AND generation=1 "
                "RETURNING last_accessed_at"
            ),
            {"asset_id": asset_id},
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "UPDATE public.asset_image_garbage AS garbage "
            "SET not_before=CAST(:last_access AS timestamptz)+interval '24 hours' "
            "FROM public.image_pyramid_generations AS generation "
            "WHERE garbage.prefix=:prefix AND generation.asset_id=:asset_id "
            "AND generation.generation=1"
        ),
        {"asset_id": asset_id, "prefix": prefix, "last_access": recent_access},
    )
    not_before = await db_session.scalar(
        text("SELECT not_before FROM public.asset_image_garbage WHERE prefix=:prefix"),
        {"prefix": prefix},
    )
    throttled = await _read_generation(db_session, actor_id, item_id, 1)
    assert throttled["pyramid"]["status"] == "ready"
    unchanged_access = await db_session.scalar(
        text(
            "SELECT last_accessed_at FROM public.image_pyramid_generations "
            "WHERE asset_id=:asset_id AND generation=1"
        ),
        {"asset_id": asset_id},
    )
    unchanged_not_before = await db_session.scalar(
        text("SELECT not_before FROM public.asset_image_garbage WHERE prefix=:prefix"),
        {"prefix": prefix},
    )
    assert unchanged_access == recent_access
    assert unchanged_not_before == not_before

    current_manifest = await _read_generation(db_session, actor_id, item_id, None)
    assert current_manifest["pyramid"]["generation"] == 2
    assert (
        await db_session.scalar(
            text(
                "SELECT last_accessed_at FROM public.image_pyramid_generations "
                "WHERE asset_id=:asset_id AND generation=2"
            ),
            {"asset_id": asset_id},
        )
        is None
    )

    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET file_path='images/changed.png' WHERE id=:item_id"
        ),
        {"item_id": item_id},
    )
    stale = await _read_generation(db_session, actor_id, item_id, 1)
    assert stale["pyramid"]["status"] == "stale"
    assert (
        await db_session.scalar(
            text(
                "SELECT last_accessed_at FROM public.image_pyramid_generations "
                "WHERE asset_id=:asset_id AND generation=1"
            ),
            {"asset_id": asset_id},
        )
        == recent_access
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT not_before FROM public.asset_image_garbage WHERE prefix=:prefix"
            ),
            {"prefix": prefix},
        )
        == not_before
    )


async def test_recent_explicit_generation_read_prevents_due_garbage_claim(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    source = await _source(db_session, item_id)
    asset_id = uuid4()
    asset = ImagePyramidAsset(
        id=asset_id,
        dataset_item_id=item_id,
        profile_version="pyramid-v1",
        active_generation=2,
    )
    db_session.add_all(
        [
            asset,
            _ready_generation(asset_id, 1, source, updated_at=datetime.now(UTC)),
            _ready_generation(asset_id, 2, source, updated_at=datetime.now(UTC)),
        ]
    )
    await db_session.flush()

    attempt_prefix = await db_session.scalar(
        text(
            "SELECT output_prefix FROM public.image_pyramid_generations "
            "WHERE asset_id=:asset_id AND generation=1"
        ),
        {"asset_id": asset_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.asset_image_garbage(prefix,reason,not_before) "
            "VALUES (:prefix,'attempt_abandoned',clock_timestamp()-interval '1 second')"
        ),
        {"prefix": attempt_prefix},
    )
    read = await _read_generation(db_session, actor_id, item_id, 1)
    assert read["pyramid"]["status"] == "ready"
    generation_prefix = f"image-pyramids/{asset_id}/g1/"
    await db_session.execute(
        text(
            "UPDATE public.asset_image_garbage "
            "SET not_before=clock_timestamp()-interval '1 second' "
            "WHERE prefix IN (:generation_prefix, :attempt_prefix)"
        ),
        {"generation_prefix": generation_prefix, "attempt_prefix": attempt_prefix},
    )

    claimed = await _claim_garbage(db_session)
    assert claimed["items"] == []
    garbage = (
        await db_session.execute(
            text(
                "SELECT prefix, not_before, lease_token FROM public.asset_image_garbage "
                "WHERE prefix IN (:generation_prefix, :attempt_prefix) ORDER BY prefix"
            ),
            {"generation_prefix": generation_prefix, "attempt_prefix": attempt_prefix},
        )
    ).all()
    assert len(garbage) == 2
    assert all(row.not_before > datetime.now(UTC) for row in garbage)
    assert all(row.lease_token is None for row in garbage)


@pytest.mark.parametrize(
    "garbage_scope",
    [
        "asset",
        "generation",
        "attempt",
        "expired_attempt",
        "completed_attempt",
        "acked_attempt",
    ],
)
async def test_explicit_read_rejects_generation_with_claimed_garbage(
    db_session, garbage_scope
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    source = await _source(db_session, item_id)
    asset_id = uuid4()
    asset = ImagePyramidAsset(
        id=asset_id,
        dataset_item_id=item_id,
        profile_version="pyramid-v1",
        active_generation=2,
    )
    db_session.add_all(
        [
            asset,
            _ready_generation(asset_id, 1, source, updated_at=datetime.now(UTC)),
            _ready_generation(asset_id, 2, source, updated_at=datetime.now(UTC)),
        ]
    )
    await db_session.flush()
    generation_prefix = f"image-pyramids/{asset_id}/g1/"
    attempt_prefix = await db_session.scalar(
        text(
            "SELECT output_prefix FROM public.image_pyramid_generations "
            "WHERE asset_id=:asset_id AND generation=1"
        ),
        {"asset_id": asset_id},
    )
    prefixes = {
        "asset": f"image-pyramids/{asset_id}/",
        "generation": generation_prefix,
        "attempt": attempt_prefix,
        "expired_attempt": attempt_prefix,
        "completed_attempt": attempt_prefix,
        "acked_attempt": attempt_prefix,
    }
    prefix = prefixes[garbage_scope]
    if garbage_scope == "generation":
        await db_session.execute(
            text(
                "INSERT INTO public.asset_image_garbage(prefix,reason,not_before) "
                "VALUES (:prefix,'generation_retired',clock_timestamp()-interval '1 second')"
            ),
            {"prefix": prefix},
        )
        claimed = await _claim_garbage(db_session)
        assert len(claimed["items"]) == 1
        assert claimed["items"][0]["prefix"] == prefix
    else:
        lease_seconds = -1 if garbage_scope == "expired_attempt" else 300
        if garbage_scope in {"completed_attempt", "acked_attempt"}:
            if garbage_scope == "completed_attempt":
                await db_session.execute(
                    text(
                        "INSERT INTO public.asset_image_garbage("
                        "prefix,reason,not_before,completed_at) "
                        "VALUES (:prefix,'attempt_abandoned',clock_timestamp()-interval '1 second',"
                        "clock_timestamp()-interval '1 second')"
                    ),
                    {"prefix": prefix},
                )
            else:
                await db_session.execute(
                    text(
                        "INSERT INTO public.asset_image_garbage("
                        "prefix,reason,not_before,last_ack_token) "
                        "VALUES (:prefix,'attempt_abandoned',clock_timestamp()-interval '1 second',"
                        ":token)"
                    ),
                    {"prefix": prefix, "token": uuid4()},
                )
        else:
            await db_session.execute(
                text(
                    "INSERT INTO public.asset_image_garbage("
                    "prefix,reason,not_before,lease_owner,lease_token,lease_expires_at) "
                    "VALUES (:prefix,'generation_retired',clock_timestamp()-interval '1 second', "
                    ":worker_id,:token,clock_timestamp()+make_interval(secs=>:lease_seconds))"
                ),
                {
                    "prefix": prefix,
                    "worker_id": uuid4(),
                    "token": uuid4(),
                    "lease_seconds": lease_seconds,
                },
            )

    response = _reply(
        await db_session.scalar(
            _READ_GENERATION,
            {
                "actor_id": actor_id,
                "task_id": None,
                "item_id": item_id,
                "generation": 1,
                "profile_version": "pyramid-v1",
            },
        )
    )
    assert response == {
        "schema": "aap-assets-images/v1",
        "ok": False,
        "error": {
            "code": "asset_missing"
            if garbage_scope in {"completed_attempt", "acked_attempt"}
            else "source_busy"
        },
    }
    garbage = (
        await db_session.execute(
            text(
                "SELECT lease_token, completed_at, last_ack_token "
                "FROM public.asset_image_garbage "
                "WHERE prefix=:prefix"
            ),
            {"prefix": prefix},
        )
    ).one()
    if garbage_scope in {"completed_attempt", "acked_attempt"}:
        assert (garbage.completed_at is not None) == (
            garbage_scope == "completed_attempt"
        )
        assert (garbage.last_ack_token is not None) == (
            garbage_scope == "acked_attempt"
        )
        assert garbage.lease_token is None
    else:
        assert garbage.lease_token is not None
    assert (
        await db_session.scalar(
            text(
                "SELECT last_accessed_at FROM public.image_pyramid_generations "
                "WHERE asset_id=:asset_id AND generation=1"
            ),
            {"asset_id": asset_id},
        )
        is None
    )


async def test_successful_refresh_starts_old_active_generation_grace_at_retirement(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    source = await _source(db_session, item_id)
    first = await _publish_pyramid(db_session, source)
    generation_row = (
        await db_session.execute(
            text(
                "SELECT asset.id, asset.active_generation "
                "FROM public.image_pyramid_assets AS asset "
                "WHERE asset.dataset_item_id=:item_id AND asset.profile_version='pyramid-v1'"
            ),
            {"item_id": item_id},
        )
    ).one()
    asset_id = generation_row.id
    assert generation_row.active_generation == first["pyramid_generation"] == 1
    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_generations SET updated_at=clock_timestamp()-interval '3 days' "
            "WHERE asset_id=:asset_id AND generation=1"
        ),
        {"asset_id": asset_id},
    )
    retirement_started = await db_session.scalar(text("SELECT clock_timestamp()"))

    second = await _publish_pyramid(db_session, source, force=True)

    assert second["pyramid_generation"] == 2
    assert (
        await db_session.scalar(
            text(
                "SELECT active_generation FROM public.image_pyramid_assets WHERE id=:asset_id"
            ),
            {"asset_id": asset_id},
        )
        == 2
    )
    queued = await db_session.execute(
        text(
            "SELECT not_before FROM public.asset_image_garbage "
            "WHERE prefix=:prefix AND reason='generation_retired'"
        ),
        {"prefix": f"image-pyramids/{asset_id}/g1/"},
    )
    assert queued.scalar_one() >= retirement_started + timedelta(hours=24)


async def test_reconcile_uses_recent_access_for_retired_generation_grace(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    source = await _source(db_session, item_id)
    asset_id = uuid4()
    old_update = datetime.now(UTC) - timedelta(days=3)
    recent_access = datetime.now(UTC) - timedelta(hours=1)
    asset = ImagePyramidAsset(
        id=asset_id,
        dataset_item_id=item_id,
        profile_version="pyramid-v1",
        active_generation=2,
    )
    generation = _ready_generation(
        asset_id,
        1,
        source,
        updated_at=old_update,
        last_accessed_at=recent_access,
    )
    db_session.add_all([asset, generation])
    await db_session.flush()

    recent = _data(
        _reply(
            await db_session.scalar(_RECONCILE, {"candidates": "[]", "batch_size": 10})
        )
    )
    assert recent["retired_generations"] == 0
    assert recent["more"] is False
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_image_garbage WHERE prefix=:prefix"
            ),
            {"prefix": f"image-pyramids/{asset_id}/g1/"},
        )
        == 0
    )

    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_generations "
            "SET last_accessed_at=clock_timestamp()-interval '25 hours' "
            "WHERE asset_id=:asset_id AND generation=1"
        ),
        {"asset_id": asset_id},
    )
    expired = _data(
        _reply(
            await db_session.scalar(_RECONCILE, {"candidates": "[]", "batch_size": 10})
        )
    )
    assert expired["retired_generations"] == 1
    assert (
        await db_session.scalar(
            text("SELECT reason FROM public.asset_image_garbage WHERE prefix=:prefix"),
            {"prefix": f"image-pyramids/{asset_id}/g1/"},
        )
        == "generation_retired"
    )
