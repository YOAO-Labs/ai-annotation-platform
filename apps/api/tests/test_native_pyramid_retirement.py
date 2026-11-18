"""Focused durable-edge tests for ordinary native pyramid retirement.

These use the repository's disposable ``test_engine`` fixture and roll back, so
they are not part of the cleanup package's pure unit suite.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_assets_catalog_native import _seed_dataset
from tests.test_assets_images_execution import (
    _ALLOCATE,
    _CANCEL,
    _claim as _claim_image,
    _data,
    _enqueue,
    _seed_actor,
    _seed_image,
    _source,
    _spec,
)
from yoao_assets_images.policy import ImagePolicy

pytestmark = pytest.mark.asyncio

ENDPOINT = "https://cache.example"
BUCKET = "media-cache-test"
LOCATION = uuid.UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7")
POLICY = '{"gc_grace_hours": 0, "url_expiry_seconds": 1}'


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


def _storage_ref(output_id):
    key_prefix = ""
    storage_identity = hashlib.sha256(
        json.dumps([ENDPOINT, BUCKET]).encode("utf-8")
    ).hexdigest()
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(LOCATION),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": ENDPOINT,
        "bucket": BUCKET,
        "key_prefix": key_prefix,
        "prefix": key_prefix + f"image-pyramids/objects/{output_id}/",
        "storage_identity": storage_identity,
    }


async def _seed_obligation(session):
    output_id, asset_id, lease_token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ref = _storage_ref(output_id)
    # The output birth trigger requires its owning pyramid asset; a random
    # dataset item keeps this synthetic obligation outside fixture ownership.
    await session.execute(
        text(
            "INSERT INTO public.image_pyramid_assets "
            "(id, dataset_item_id, profile_version) "
            "VALUES (CAST(:asset_id AS uuid), CAST(:item_id AS uuid), 'pyramid-v1')"
        ),
        {"asset_id": asset_id, "item_id": uuid.uuid4()},
    )
    await session.execute(
        text(
            "INSERT INTO public.image_pyramid_outputs "
            "(id, asset_id, generation, lease_token, storage_ref, state) "
            "VALUES (CAST(:output_id AS uuid), CAST(:asset_id AS uuid), 1, "
            "CAST(:lease_token AS uuid), CAST(:ref AS jsonb), 'building')"
        ),
        {
            "output_id": output_id,
            "asset_id": asset_id,
            "lease_token": lease_token,
            "ref": json.dumps(ref),
        },
    )
    await session.execute(
        text(
            "INSERT INTO public.storage_maintenance_gates "
            "(location_id, state, revision) "
            "VALUES (CAST(:location_id AS uuid), 'active', 1) "
            "ON CONFLICT (location_id) DO UPDATE SET state = 'active'"
        ),
        {"location_id": LOCATION},
    )
    await session.execute(
        text(
            "INSERT INTO yoao_assets_v1.native_pyramid_retirement_targets "
            "(output_id, asset_id, generation, gate_revision, location_id, "
            "config_revision, binding_generation, endpoint_url, bucket, "
            "key_prefix, prefix, storage_identity, policy, gc_grace_hours, "
            "url_expiry_seconds) "
            "VALUES (CAST(:output_id AS uuid), CAST(:asset_id AS uuid), 1, 1, "
            "CAST(:location_id AS uuid), 1, 1, :endpoint, :bucket, '', "
            ":prefix, :identity, CAST(:policy AS jsonb), 0, 1)"
        ),
        {
            "output_id": output_id,
            "asset_id": asset_id,
            "location_id": LOCATION,
            "endpoint": ENDPOINT,
            "bucket": BUCKET,
            "prefix": ref["prefix"],
            "identity": ref["storage_identity"],
            "policy": POLICY,
        },
    )
    await session.execute(
        text(
            "INSERT INTO yoao_assets_v1.native_pyramid_retirements "
            "(output_id, not_before) "
            "VALUES (CAST(:output_id AS uuid), "
            "clock_timestamp() - interval '1 hour')"
        ),
        {"output_id": output_id},
    )
    return output_id, lease_token


async def _claim(session, worker_id):
    return _reply(
        await session.scalar(
            text(
                "SELECT yoao_assets_v1.claim_native_pyramid_retirement("
                "CAST(:worker_id AS uuid), 120)"
            ),
            {"worker_id": worker_id},
        )
    )


async def _prepare(session, output_id, lease_token, page_size=100):
    return _reply(
        await session.scalar(
            text(
                "SELECT yoao_assets_v1.prepare_native_pyramid_retirement_page("
                "CAST(:output_id AS uuid), CAST(:lease_token AS uuid), :page_size)"
            ),
            {
                "output_id": output_id,
                "lease_token": lease_token,
                "page_size": page_size,
            },
        )
    )


async def _complete(session, output_id, lease_token, page_id, outcome, operation_ids):
    return _reply(
        await session.scalar(
            text(
                "SELECT yoao_assets_v1.complete_native_pyramid_retirement_page("
                "CAST(:output_id AS uuid), CAST(:lease_token AS uuid), "
                "CAST(:page_id AS uuid), :outcome, CAST(:operation_ids AS jsonb))"
            ),
            {
                "output_id": output_id,
                "lease_token": lease_token,
                "page_id": page_id,
                "outcome": outcome,
                "operation_ids": json.dumps(
                    [str(operation_id) for operation_id in operation_ids]
                ),
            },
        )
    )


async def test_saved_target_claim_and_page_fences_are_exact(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            output_id, _ = await _seed_obligation(session)

            claimed = await _claim(session, uuid.uuid4())
            assert claimed["ok"] is True
            claim = claimed["data"]["claim"]
            assert claim is not None
            assert claim["output_id"] == str(output_id)
            assert claim["storage_ref"]["bucket"] == BUCKET
            assert (
                claim["storage_ref"]["prefix"]
                == claim["storage_ref"]["key_prefix"]
                + f"image-pyramids/objects/{output_id}/"
            )
            assert claim["after_operation_id"] is None
            lease_token = uuid.UUID(claim["lease_token"])

            prepared = await _prepare(session, output_id, lease_token)
            assert prepared["ok"] is True
            page = prepared["data"]
            assert page["items"] == []
            assert page["exhausted"] is True
            assert page["after_operation_id"] is None
            page_id = uuid.UUID(page["page_id"])

            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_operations "
                        "WHERE id = CAST(:page_id AS uuid)"
                    ),
                    {"page_id": page_id},
                )
                == 1
            )

            foreign_page = await _complete(
                session, output_id, lease_token, uuid.uuid4(), "prefix_absent", []
            )
            assert foreign_page["ok"] is False
            assert foreign_page["error"]["code"] == "invalid_request"

            stale_token = await _complete(
                session, output_id, uuid.uuid4(), page_id, "prefix_absent", []
            )
            assert stale_token["ok"] is False
            assert stale_token["error"]["code"] == "lease_lost"

            wrong_outcome = await _complete(
                session,
                output_id,
                lease_token,
                page_id,
                "members_absent",
                [uuid.uuid4()],
            )
            assert wrong_outcome["ok"] is False
            assert wrong_outcome["error"]["code"] == "invalid_request"

            with_open_ids = await _complete(
                session,
                output_id,
                lease_token,
                page_id,
                "prefix_absent",
                [uuid.uuid4()],
            )
            assert with_open_ids["ok"] is False
            assert with_open_ids["error"]["code"] == "invalid_request"

            closed = await _complete(
                session, output_id, lease_token, page_id, "prefix_absent", []
            )
            assert closed["ok"] is True
            assert closed["data"] == {
                "output_id": str(output_id),
                "after_operation_id": None,
                "completed": True,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT state FROM public.image_pyramid_outputs "
                        "WHERE id = CAST(:output_id AS uuid)"
                    ),
                    {"output_id": output_id},
                )
                == "deleted"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_operations "
                        "WHERE id = CAST(:page_id AS uuid)"
                    ),
                    {"page_id": page_id},
                )
                == 0
            )
            assert await session.scalar(
                text(
                    "SELECT closed_at IS NOT NULL "
                    "FROM yoao_assets_v1.native_pyramid_retirements "
                    "WHERE output_id = CAST(:output_id AS uuid)"
                ),
                {"output_id": output_id},
            )

            replayed = await _complete(
                session, output_id, lease_token, page_id, "prefix_absent", []
            )
            assert replayed["data"] == closed["data"]
        finally:
            await session.rollback()


async def test_expired_page_releases_only_its_permit_and_is_then_recovered(
    test_engine,
):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            output_id, _ = await _seed_obligation(session)
            first = await _claim(session, uuid.uuid4())
            first_claim = first["data"]["claim"]
            first_token = uuid.UUID(first_claim["lease_token"])
            first_page = (await _prepare(session, output_id, first_token))["data"]
            first_page_id = uuid.UUID(first_page["page_id"])

            await session.execute(
                text(
                    "UPDATE yoao_assets_v1.native_pyramid_retirements "
                    "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE output_id = CAST(:output_id AS uuid)"
                ),
                {"output_id": output_id},
            )

            stale = await _complete(
                session, output_id, first_token, first_page_id, "prefix_absent", []
            )
            assert stale["ok"] is False
            assert stale["error"]["code"] == "lease_lost"

            released = await _complete(
                session,
                output_id,
                first_token,
                first_page_id,
                "closed_without_ack",
                [],
            )
            assert released["ok"] is True
            assert released["data"] == {
                "output_id": str(output_id),
                "after_operation_id": None,
                "completed": False,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_operations "
                        "WHERE id = CAST(:page_id AS uuid)"
                    ),
                    {"page_id": first_page_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT state FROM public.image_pyramid_outputs "
                        "WHERE id = CAST(:output_id AS uuid)"
                    ),
                    {"output_id": output_id},
                )
                == "retiring"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT closed_at FROM "
                        "yoao_assets_v1.native_pyramid_retirements "
                        "WHERE output_id = CAST(:output_id AS uuid)"
                    ),
                    {"output_id": output_id},
                )
                is None
            )

            replayed = await _complete(
                session,
                output_id,
                first_token,
                first_page_id,
                "closed_without_ack",
                [],
            )
            assert replayed["data"] == released["data"]

            recovered = await _claim(session, uuid.uuid4())
            recovered_claim = recovered["data"]["claim"]
            assert recovered_claim is not None
            recovered_token = uuid.UUID(recovered_claim["lease_token"])
            assert recovered_token != first_token
            assert recovered_claim["after_operation_id"] is None
            recovered_page = (await _prepare(session, output_id, recovered_token))[
                "data"
            ]
            assert recovered_page["exhausted"] is True
            assert recovered_page["page_id"] != first_page["page_id"]

            closed = await _complete(
                session,
                output_id,
                recovered_token,
                uuid.UUID(recovered_page["page_id"]),
                "prefix_absent",
                [],
            )
            assert closed["ok"] is True
            assert closed["data"]["completed"] is True
        finally:
            await session.rollback()


async def test_ordinary_allocation_registers_and_closes_its_saved_output(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        width=1,
        height=1,
    )
    await ensure_legacy_bindings(db_session)
    source = await _source(db_session, item_id)
    policy = ImagePolicy(
        optional_pixels=1,
        required_pixels=1,
        auto_generate_pyramids=True,
        gc_grace_hours=0,
        url_expiry_seconds=1,
    ).model_dump(mode="json")
    job_id, _ = await _enqueue(
        db_session, source, _spec("pyramid", source, policy=policy)
    )
    claim = await _claim_image(db_session, job_id=job_id, kind="pyramid")
    allocation = _data(
        _reply(
            await db_session.scalar(
                _ALLOCATE,
                {
                    "job_id": job_id,
                    "job_generation": claim["generation"],
                    "lease_token": claim["lease_token"],
                    "asset_id": uuid.UUID(claim["pyramid_asset_id"]),
                    "generation": claim["pyramid_generation"],
                },
            )
        )
    )
    output_id = uuid.UUID(allocation["output_id"])

    registered = (
        (
            await db_session.execute(
                text(
                    "SELECT target.endpoint_url, target.bucket, target.prefix, "
                    "target.key_prefix, target.policy, retirement.closed_at "
                    "FROM yoao_assets_v1.native_pyramid_retirement_targets AS target "
                    "JOIN yoao_assets_v1.native_pyramid_retirements AS retirement "
                    "  ON retirement.output_id = target.output_id "
                    "WHERE target.output_id = CAST(:output_id AS uuid)"
                ),
                {"output_id": output_id},
            )
        )
        .mappings()
        .one()
    )
    assert registered["prefix"] == allocation["storage_ref"]["prefix"]
    assert registered["prefix"] == (
        registered["key_prefix"] + f"image-pyramids/objects/{output_id}/"
    )
    registered_policy = _reply(registered["policy"])
    assert registered_policy["gc_grace_hours"] == 0
    assert registered_policy["url_expiry_seconds"] == 1
    assert registered["closed_at"] is None

    canceled = _data(
        _reply(
            await db_session.scalar(
                _CANCEL,
                {
                    "operation_id": uuid.uuid4(),
                    "actor_id": actor_id,
                    "job_id": job_id,
                },
            )
        )
    )
    assert canceled["canceled"] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT status FROM public.image_pyramid_generations "
                "WHERE asset_id = CAST(:asset_id AS uuid) "
                "AND generation = :generation"
            ),
            {
                "asset_id": uuid.UUID(claim["pyramid_asset_id"]),
                "generation": claim["pyramid_generation"],
            },
        )
        == "failed"
    )
    await db_session.execute(
        text(
            "UPDATE yoao_assets_v1.native_pyramid_retirements "
            "SET not_before = clock_timestamp() - interval '1 second' "
            "WHERE output_id = CAST(:output_id AS uuid)"
        ),
        {"output_id": output_id},
    )

    claimed = await _claim(db_session, uuid.uuid4())
    retirement = claimed["data"]["claim"]
    assert retirement is not None
    assert retirement["output_id"] == str(output_id)
    assert retirement["storage_ref"]["prefix"] == allocation["storage_ref"]["prefix"]
    lease_token = uuid.UUID(retirement["lease_token"])

    page = (await _prepare(db_session, output_id, lease_token))["data"]
    assert page["items"] == []
    assert page["exhausted"] is True
    closed = await _complete(
        db_session,
        output_id,
        lease_token,
        uuid.UUID(page["page_id"]),
        "prefix_absent",
        [],
    )
    assert closed["ok"] is True
    assert closed["data"]["completed"] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT state FROM public.image_pyramid_outputs "
                "WHERE id = CAST(:output_id AS uuid)"
            ),
            {"output_id": output_id},
        )
        == "deleted"
    )
    assert await db_session.scalar(
        text(
            "SELECT closed_at IS NOT NULL "
            "FROM yoao_assets_v1.native_pyramid_retirements "
            "WHERE output_id = CAST(:output_id AS uuid)"
        ),
        {"output_id": output_id},
    )
