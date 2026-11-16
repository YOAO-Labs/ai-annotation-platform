"""Focused durable-edge tests for Native saved targets, written closure and fixture routing.

These use the repository's disposable ``test_engine`` database and roll back, so
they are not part of any pure unit suite.  They never execute during a code
change; the integration owner runs them once on a verified test database.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import image_pyramid_objects as objects
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_assets_catalog_native import _seed_dataset
from tests.test_assets_images_execution import (
    _ALLOCATE,
    _BEGIN_MEMBER,
    _CANCEL,
    _SUBMIT_MEMBER,
    _WRITE_MEMBER,
    _claim as _claim_image,
    _data,
    _enqueue,
    _seed_actor,
    _seed_image,
    _source,
    _spec,
)
from tests.test_native_pyramid_retirement import (
    BUCKET,
    ENDPOINT,
    LOCATION,
    _claim,
    _complete,
    _prepare,
    _reply,
)
from yoao_assets_images.policy import ImagePolicy

pytestmark = pytest.mark.asyncio

_CLAIMABLE = text(
    "SELECT aap_assets_v1._native_pyramid_retirement_claimable("
    "CAST(:output_id AS uuid))"
)
_ABANDON = text(
    "SELECT aap_assets_v1.abandon_native_pyramid_member("
    "CAST(:operation_id AS uuid), :state)"
)
_VERIFY_MEMBER = text(
    "SELECT aap_assets_v1.verify_native_pyramid_member(CAST(:operation_id AS uuid))"
)
_CAPTURE_WRITE = text(
    "SELECT aap_assets_v1.capture_native_pyramid_write_io("
    "CAST(:operation_id AS uuid), CAST(:job_id AS uuid), "
    "CAST(:job_generation AS bigint), CAST(:lease_token AS uuid), :mode)"
)
_CAPTURE_RETIREMENT = text(
    "SELECT aap_assets_v1.capture_native_pyramid_retirement_io("
    "CAST(:output_id AS uuid), CAST(:lease_token AS uuid), CAST(:page_id AS uuid))"
)
_DESTINATION = text(
    "SELECT aap_assets_v1._native_pyramid_target_destination("
    "CAST(:location_id AS uuid), :endpoint_url, :bucket, :storage_identity)"
)
_RESOLVE_FIXTURE_CACHE = text(
    "SELECT aap_assets_v1.resolve_fixture_native_cache(:key)"
)


def _identity(endpoint: str, bucket: str) -> str:
    return hashlib.sha256(json.dumps([endpoint, bucket]).encode("utf-8")).hexdigest()


def _ref(output_id, location_id, endpoint, bucket, *, key_prefix="", config=1, binding=1):
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(location_id),
        "config_revision": config,
        "binding_generation": binding,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "key_prefix": key_prefix,
        "prefix": key_prefix + f"image-pyramids/objects/{output_id}/",
        "storage_identity": _identity(endpoint, bucket),
    }


async def _maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _allocate(session, item_id, actor_id, *, url_expiry_seconds=1):
    source = await _source(session, item_id)
    policy = ImagePolicy(
        optional_pixels=1,
        required_pixels=1,
        auto_generate_pyramids=True,
        gc_grace_hours=0,
        url_expiry_seconds=url_expiry_seconds,
    ).model_dump(mode="json")
    job_id, _ = await _enqueue(session, source, _spec("pyramid", source, policy=policy))
    claim = await _claim_image(session, job_id=job_id, kind="pyramid")
    allocation = _data(
        _reply(
            await session.scalar(
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
    return {
        "job_id": job_id,
        "job_generation": claim["generation"],
        "lease_token": claim["lease_token"],
        "actor_id": actor_id,
        "output_id": uuid.UUID(allocation["output_id"]),
        "storage_ref": allocation["storage_ref"],
    }


async def _reserve(session, output_id, relative_key):
    operation_id = uuid.uuid4()
    reservation = _data(
        _reply(
            await session.scalar(
                _BEGIN_MEMBER,
                {
                    "operation_id": operation_id,
                    "output_id": output_id,
                    "relative_key": relative_key,
                    "sha256": hashlib.sha256(relative_key.encode()).hexdigest(),
                    "size_bytes": 10,
                },
            )
        )
    )
    assert reservation["state"] == "prepared"
    submitted = _data(
        _reply(
            await session.scalar(_SUBMIT_MEMBER, {"operation_id": operation_id})
        )
    )
    assert submitted["state"] == "submitted"
    return operation_id


async def _write(session, operation_id, etag, version):
    return _reply(
        await session.scalar(
            _WRITE_MEMBER,
            {"operation_id": operation_id, "etag": etag, "version_id": version},
        )
    )


async def _abandon(session, operation_id, state):
    return _reply(
        await session.scalar(_ABANDON, {"operation_id": operation_id, "state": state})
    )


async def _claimable(session, output_id):
    return await session.scalar(_CLAIMABLE, {"output_id": output_id})


async def _permits(session, output_id):
    return await session.scalar(
        text(
            "SELECT count(*) FROM public.storage_operations "
            "WHERE purpose = :purpose"
        ),
        {"purpose": "image_pyramid_" + output_id.hex},
    )


async def _cancel(session, allocation):
    return _data(
        _reply(
            await session.scalar(
                _CANCEL,
                {
                    "operation_id": uuid.uuid4(),
                    "actor_id": allocation["actor_id"],
                    "job_id": allocation["job_id"],
                },
            )
        )
    )


async def _make_retirement_due(session, output_id):
    await session.execute(
        text(
            "UPDATE aap_assets_v1.native_pyramid_retirements "
            "SET not_before = clock_timestamp() - interval '1 minute' "
            "WHERE output_id = CAST(:output_id AS uuid)"
        ),
        {"output_id": output_id},
    )
    await session.execute(
        text(
            "UPDATE public.image_pyramid_generations "
            "SET last_accessed_at = clock_timestamp() - interval '1 hour' "
            "WHERE output_id = CAST(:output_id AS uuid)"
        ),
        {"output_id": output_id},
    )


async def test_written_closure_settles_its_operation_and_permits_retirement(test_engine):
    maker = await _maker(test_engine)
    async with maker() as session:
        await session.begin()
        try:
            actor_id = await _seed_actor(session)
            dataset_id = await _seed_dataset(session, actor_id=actor_id)
            item_id = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            await ensure_legacy_bindings(session)
            allocation = await _allocate(session, item_id, actor_id)
            output_id = allocation["output_id"]
            manifest = await _reserve(session, output_id, "manifest.json")
            overview = await _reserve(session, output_id, "overview.webp")

            written = _data(await _write(session, manifest, '"etag-manifest"', "null"))
            assert written["state"] == "written"
            assert await _permits(session, output_id) == 2

            # A live attempt admits verification while the permit is open.
            verified_capture = _reply(
                await session.scalar(
                    _CAPTURE_WRITE,
                    {
                        "operation_id": manifest,
                        "job_id": allocation["job_id"],
                        "job_generation": allocation["job_generation"],
                        "lease_token": allocation["lease_token"],
                        "mode": "verify",
                    },
                )
            )
            assert verified_capture["ok"] is True
            assert verified_capture["data"]["mode"] == "verify"
            assert verified_capture["data"]["members"][0]["etag"] == '"etag-manifest"'
            assert verified_capture["data"]["members"][0]["version_id"] == "null"

            # Written-member physical closure keeps the receipt and closes
            # only that member's own admitted operation.
            closed = _data(await _abandon(session, manifest, "written"))
            assert closed["state"] == "written"
            assert await _permits(session, output_id) == 1
            replayed = _data(await _abandon(session, manifest, "written"))
            assert replayed == closed
            assert await _permits(session, output_id) == 1
            receipt = (
                (
                    await session.execute(
                        text(
                            "SELECT state, etag, version_id "
                            "FROM aap_assets_v1.image_pyramid_native_members "
                            "WHERE operation_id = CAST(:operation_id AS uuid)"
                        ),
                        {"operation_id": manifest},
                    )
                )
                .mappings()
                .one()
            )
            assert dict(receipt) == {
                "state": "written",
                "etag": '"etag-manifest"',
                "version_id": "null",
            }

            # A closed written member cannot regain a verification capability.
            refused_capture = _reply(
                await session.scalar(
                    _CAPTURE_WRITE,
                    {
                        "operation_id": manifest,
                        "job_id": allocation["job_id"],
                        "job_generation": allocation["job_generation"],
                        "lease_token": allocation["lease_token"],
                        "mode": "verify",
                    },
                )
            )
            assert refused_capture["ok"] is False
            assert refused_capture["error"]["code"] == "invalid_request"
            refused_verify = _reply(
                await session.scalar(_VERIFY_MEMBER, {"operation_id": manifest})
            )
            assert refused_verify["ok"] is False
            assert refused_verify["error"]["code"] == "invalid_request"

            await _cancel(session, allocation)

            # A known, fully drained PUT result still registers after the job
            # lease and source authority are gone: this records an admitted
            # physical fact and never authorizes new I/O.
            late = _data(await _write(session, overview, '"etag-overview"', None))
            assert late["state"] == "written"
            versions = (
                (
                    await session.execute(
                        text(
                            "SELECT operation_id, version_id, version_id IS NULL "
                            "AS omitted FROM aap_assets_v1.image_pyramid_native_members "
                            "WHERE output_id = CAST(:output_id AS uuid)"
                        ),
                        {"output_id": output_id},
                    )
                )
                .mappings()
                .all()
            )
            by_operation = {row["operation_id"]: row for row in versions}
            # The literal S3 version is a durable string, distinct from NULL.
            assert by_operation[manifest]["version_id"] == "null"
            assert by_operation[manifest]["omitted"] is False
            assert by_operation[overview]["version_id"] is None
            assert by_operation[overview]["omitted"] is True

            await _make_retirement_due(session, output_id)

            # An open written operation still blocks retirement.
            assert await _claimable(session, output_id) is False
            blocked = await _claim(session, uuid.uuid4())
            assert blocked["ok"] is True
            assert blocked["data"]["claim"] is None

            settled = _data(await _abandon(session, overview, "written"))
            assert settled["state"] == "written"
            assert await _permits(session, output_id) == 0
            assert await _claimable(session, output_id) is True

            claimed = await _claim(session, uuid.uuid4())
            assert claimed["ok"] is True
            retirement = claimed["data"]["claim"]
            assert retirement is not None
            lease_token = uuid.UUID(retirement["lease_token"])

            page = (await _prepare(session, output_id, lease_token))["data"]
            assert page["exhausted"] is False
            items = {uuid.UUID(item["operation_id"]): item for item in page["items"]}
            assert set(items) == {manifest, overview}
            assert items[manifest]["etag"] == '"etag-manifest"'
            assert items[manifest]["version_id"] == "null"
            assert items[overview]["version_id"] is None

            captured = _reply(
                await session.scalar(
                    _CAPTURE_RETIREMENT,
                    {
                        "output_id": output_id,
                        "lease_token": lease_token,
                        "page_id": uuid.UUID(page["page_id"]),
                    },
                )
            )
            assert captured["ok"] is True
            assert captured["data"]["mode"] == "retirement_members"
            captured_items = {
                uuid.UUID(item["operation_id"]): item
                for item in captured["data"]["members"]
            }
            assert captured_items[manifest]["version_id"] == "null"
            assert captured_items[overview]["version_id"] is None

            completed = await _complete(
                session,
                output_id,
                lease_token,
                uuid.UUID(page["page_id"]),
                "members_absent",
                [uuid.UUID(item["operation_id"]) for item in page["items"]],
            )
            assert completed["ok"] is True
            assert completed["data"]["completed"] is False

            terminal = (await _prepare(session, output_id, lease_token))["data"]
            assert terminal["exhausted"] is True
            assert terminal["items"] == []
            closed = await _complete(
                session,
                output_id,
                lease_token,
                uuid.UUID(terminal["page_id"]),
                "prefix_absent",
                [],
            )
            assert closed["ok"] is True
            assert closed["data"]["completed"] is True
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
        finally:
            await session.rollback()


async def test_unknown_and_unwritten_members_never_close_their_operation(test_engine):
    maker = await _maker(test_engine)
    async with maker() as session:
        await session.begin()
        try:
            actor_id = await _seed_actor(session)
            dataset_id = await _seed_dataset(session, actor_id=actor_id)
            item_id = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            await ensure_legacy_bindings(session)
            allocation = await _allocate(session, item_id, actor_id)
            output_id = allocation["output_id"]

            unknown = await _reserve(session, output_id, "manifest.json")
            abandoned = _data(await _abandon(session, unknown, "unknown"))
            assert abandoned["state"] == "unknown"
            assert await _permits(session, output_id) == 1
            for outcome in ("written", "not_written"):
                replay = _data(await _abandon(session, unknown, outcome))
                assert replay["state"] == "unknown"
            assert await _permits(session, output_id) == 1

            prepared = uuid.uuid4()
            reservation = _data(
                _reply(
                    await session.scalar(
                        _BEGIN_MEMBER,
                        {
                            "operation_id": prepared,
                            "output_id": output_id,
                            "relative_key": "overview.webp",
                            "sha256": hashlib.sha256(b"overview").hexdigest(),
                            "size_bytes": 11,
                        },
                    )
                )
            )
            assert reservation["state"] == "prepared"
            refused = _reply(
                await session.scalar(
                    _ABANDON, {"operation_id": prepared, "state": "written"}
                )
            )
            assert refused["ok"] is False
            assert refused["error"]["code"] == "invalid_request"
            assert await _permits(session, output_id) == 2
            cleared = _data(await _abandon(session, prepared, "not_written"))
            assert cleared["state"] == "not_written"
            assert await _permits(session, output_id) == 1

            await _cancel(session, allocation)
            await _make_retirement_due(session, output_id)
            assert await _claimable(session, output_id) is False
            blocked = await _claim(session, uuid.uuid4())
            assert blocked["ok"] is True
            assert blocked["data"]["claim"] is None
        finally:
            await session.rollback()


async def _connection(session, endpoint, bucket, base_prefix=""):
    connection_id, location_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO public.storage_connections "
            "(id, name, revision, kind, config, secret_enc, scope, project_id, "
            " created_by) VALUES (CAST(:id AS uuid), :name, 1, 's3', "
            " CAST(:config AS jsonb), :secret, 'global', NULL, NULL)"
        ),
        {
            "id": connection_id,
            "name": f"native-target-{connection_id}",
            "config": json.dumps(
                {"endpoint": endpoint, "bucket": bucket, "base_prefix": base_prefix}
            ),
            "secret": b"\x01",
        },
    )
    await session.execute(
        text(
            "INSERT INTO public.storage_locations "
            "(id, name, kind, config_revision, config, connection_id, enabled) "
            "VALUES (CAST(:id AS uuid), :name, 's3', 1, '{}'::jsonb, "
            " CAST(:connection AS uuid), true)"
        ),
        {
            "id": location_id,
            "name": f"native-target-location-{location_id}",
            "connection": connection_id,
        },
    )
    return connection_id, location_id


async def _switch_media_cache_binding(session, location_id, bucket, generation):
    await session.execute(
        text(
            "UPDATE public.storage_bindings SET location_id = CAST(:location AS uuid), "
            "bucket = :bucket, generation = :generation WHERE purpose = 'media-cache'"
        ),
        {"location": location_id, "bucket": bucket, "generation": generation},
    )


async def test_saved_targets_require_the_registered_connection_bucket(test_engine):
    maker = await _maker(test_engine)
    async with maker() as session:
        await session.begin()
        try:
            actor_id = await _seed_actor(session)
            dataset_id = await _seed_dataset(session, actor_id=actor_id)
            first_item = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            second_item = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            await ensure_legacy_bindings(session)
            connection_a, location_a = await _connection(
                session, "https://cache-a.example", "registered-a", base_prefix="tenant"
            )
            await _switch_media_cache_binding(session, location_a, "registered-a", 2)

            first = await _allocate(session, first_item, actor_id)
            assert first["storage_ref"]["location_id"] == str(location_a)
            assert first["storage_ref"]["endpoint_url"] == "https://cache-a.example"
            assert first["storage_ref"]["bucket"] == "registered-a"
            assert first["storage_ref"]["key_prefix"] == "tenant/_aap_managed/media-cache/"

            destination = _reply(
                await session.scalar(
                    _DESTINATION,
                    {
                        "location_id": location_a,
                        "endpoint_url": first["storage_ref"]["endpoint_url"],
                        "bucket": first["storage_ref"]["bucket"],
                        "storage_identity": first["storage_ref"]["storage_identity"],
                    },
                )
            )
            assert destination is not None
            assert destination["destination"]["kind"] == "s3"
            assert destination["destination"]["config"]["bucket"] == "registered-a"
            assert destination["destination"]["secret_enc"]

            wrong_bucket = await session.scalar(
                _DESTINATION,
                {
                    "location_id": location_a,
                    "endpoint_url": first["storage_ref"]["endpoint_url"],
                    "bucket": "other-bucket",
                    "storage_identity": first["storage_ref"]["storage_identity"],
                },
            )
            assert wrong_bucket is None

            # A mutated connection bucket is a refusal, never a redirect: the
            # saved allocation keeps pointing at its own registered target.
            await session.execute(
                text(
                    "UPDATE public.storage_connections SET config = CAST(:config AS jsonb) "
                    "WHERE id = CAST(:connection AS uuid)"
                ),
                {
                    "connection": connection_a,
                    "config": json.dumps(
                        {
                            "endpoint": "https://cache-a.example",
                            "bucket": "mutated-a",
                            "base_prefix": "tenant",
                        }
                    ),
                },
            )
            mutated = await session.scalar(
                _DESTINATION,
                {
                    "location_id": location_a,
                    "endpoint_url": first["storage_ref"]["endpoint_url"],
                    "bucket": first["storage_ref"]["bucket"],
                    "storage_identity": first["storage_ref"]["storage_identity"],
                },
            )
            assert mutated is None

            # The same mismatch refuses to mint a fresh saved target.
            source = await _source(session, second_item)
            policy = ImagePolicy(
                optional_pixels=1,
                required_pixels=1,
                auto_generate_pyramids=True,
            ).model_dump(mode="json")
            job_id, _ = await _enqueue(
                session, source, _spec("pyramid", source, policy=policy)
            )
            claim = await _claim_image(session, job_id=job_id, kind="pyramid")
            refused = _reply(
                await session.scalar(
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
            assert refused["ok"] is False
            assert refused["error"]["code"] == "storage_maintenance"

            # A non-string connection bucket is also refused.
            await session.execute(
                text(
                    "UPDATE public.storage_connections SET config = CAST(:config AS jsonb) "
                    "WHERE id = CAST(:connection AS uuid)"
                ),
                {
                    "connection": connection_a,
                    "config": json.dumps(
                        {
                            "endpoint": "https://cache-a.example",
                            "bucket": 7,
                            "base_prefix": "tenant",
                        }
                    ),
                },
            )
            assert (
                await session.scalar(
                    _DESTINATION,
                    {
                        "location_id": location_a,
                        "endpoint_url": first["storage_ref"]["endpoint_url"],
                        "bucket": first["storage_ref"]["bucket"],
                        "storage_identity": first["storage_ref"]["storage_identity"],
                    },
                )
                is None
            )
        finally:
            await session.rollback()


async def test_binding_switch_keeps_each_saved_target_at_its_own_location(test_engine):
    maker = await _maker(test_engine)
    async with maker() as session:
        await session.begin()
        try:
            actor_id = await _seed_actor(session)
            dataset_id = await _seed_dataset(session, actor_id=actor_id)
            first_item = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            second_item = await _seed_image(
                session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
            )
            await ensure_legacy_bindings(session)
            connection_a, location_a = await _connection(
                session, "https://cache-a.example", "registered-a", base_prefix="tenant"
            )
            await _switch_media_cache_binding(session, location_a, "registered-a", 2)
            first = await _allocate(session, first_item, actor_id)
            assert first["storage_ref"]["location_id"] == str(location_a)

            # The purpose binding switches A->B; the old saved target stays at A.
            connection_b, location_b = await _connection(
                session, "https://cache-b.example", "registered-b"
            )
            await _switch_media_cache_binding(session, location_b, "registered-b", 3)
            second = await _allocate(session, second_item, actor_id)
            assert second["storage_ref"]["location_id"] == str(location_b)
            assert second["storage_ref"]["endpoint_url"] == "https://cache-b.example"
            assert second["storage_ref"]["bucket"] == "registered-b"
            assert second["storage_ref"]["key_prefix"] == "_aap_managed/media-cache/"

            for saved in (first, second):
                resolved = _reply(
                    await session.scalar(
                        _DESTINATION,
                        {
                            "location_id": uuid.UUID(saved["storage_ref"]["location_id"]),
                            "endpoint_url": saved["storage_ref"]["endpoint_url"],
                            "bucket": saved["storage_ref"]["bucket"],
                            "storage_identity": saved["storage_ref"]["storage_identity"],
                        },
                    )
                )
                assert resolved is not None

            saved_targets = (
                (
                    await session.execute(
                        text(
                            "SELECT output_id, location_id FROM "
                            "aap_assets_v1.native_pyramid_retirement_targets "
                            "WHERE output_id IN (CAST(:first AS uuid), CAST(:second AS uuid))"
                        ),
                        {
                            "first": first["output_id"],
                            "second": second["output_id"],
                        },
                    )
                )
                .mappings()
                .all()
            )
            by_output = {row["output_id"]: row["location_id"] for row in saved_targets}
            assert by_output[first["output_id"]] == location_a
            assert by_output[second["output_id"]] == location_b
        finally:
            await session.rollback()


async def _fixture_run(session, *, parent=None):
    run_id = uuid.uuid4()
    if parent is None:
        await session.execute(
            text(
                "INSERT INTO yoao_fixture_v1.fixture_runs (id, family) "
                "VALUES (CAST(:id AS uuid), 'screenshot')"
            ),
            {"id": run_id},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO yoao_fixture_v1.fixture_runs "
                "(id, parent_run_id, scope_key, family) "
                "VALUES (CAST(:id AS uuid), CAST(:parent AS uuid), 'scope', 'screenshot')"
            ),
            {"id": run_id, "parent": parent},
        )
    return run_id


async def _register_resolver(session, suite_id, *, run_id, proxy_origin):
    await session.execute(
        text(
            "INSERT INTO aap_assets_v1.fixture_native_resolver_principals "
            "(suite_id, principal_oid, principal_name, state) "
            "VALUES (CAST(:suite AS uuid), "
            " (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user), "
            " current_user, 'active')"
        ),
        {"suite": suite_id},
    )
    await session.execute(
        text(
            "INSERT INTO aap_assets_v1.fixture_native_read_principals "
            "(run_id, principal_oid, principal_name, proxy_origin, state) "
            "VALUES (CAST(:run AS uuid), "
            " (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user), "
            " current_user, :origin, 'active')"
        ),
        {"run": run_id, "origin": proxy_origin},
    )


async def test_fixture_resolver_serves_only_verified_pyramid_members(test_engine):
    maker = await _maker(test_engine)
    async with maker() as session:
        await session.begin()
        try:
            suite = await _fixture_run(session)
            run = await _fixture_run(session, parent=suite)
            proxy_origin = f"http://fixture-proxy-{uuid.uuid4().hex}.example"
            await _register_resolver(
                session, suite, run_id=run, proxy_origin=proxy_origin
            )

            dataset_id, item_id = uuid.uuid4(), uuid.uuid4()
            asset_id, job_id, lease_token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            output_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO public.image_pyramid_assets "
                    "(id, dataset_item_id, profile_version) "
                    "VALUES (CAST(:id AS uuid), CAST(:item AS uuid), 'pyramid-v1')"
                ),
                {"id": asset_id, "item": item_id},
            )
            await session.execute(
                text(
                    "INSERT INTO aap_assets_v1.fixture_native_media_jobs "
                    "(family, job_id, run_id, dataset_id, item_id, job_kind, "
                    " owner_kind, owner_id) "
                    "VALUES ('image', CAST(:job AS uuid), CAST(:run AS uuid), "
                    " CAST(:dataset AS uuid), CAST(:item AS uuid), 'pyramid', "
                    " 'dataset_item', CAST(:item AS uuid))"
                ),
                {
                    "job": job_id,
                    "run": run,
                    "dataset": dataset_id,
                    "item": item_id,
                },
            )
            ref = _ref(output_id, LOCATION, ENDPOINT, BUCKET)
            await session.execute(
                text(
                    "INSERT INTO public.image_pyramid_outputs "
                    "(id, asset_id, generation, lease_token, storage_ref, state) "
                    "VALUES (CAST(:id AS uuid), CAST(:asset AS uuid), 1, "
                    " CAST(:lease AS uuid), CAST(:ref AS jsonb), 'building')"
                ),
                {
                    "id": output_id,
                    "asset": asset_id,
                    "lease": lease_token,
                    "ref": json.dumps(ref),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO aap_assets_v1.image_pyramid_native_attempts "
                    "(output_id, job_id, job_generation, lease_token, asset_id, "
                    " generation, source_generation, source_snapshot, "
                    " profile_version, gate_revision) "
                    "VALUES (CAST(:output AS uuid), CAST(:job AS uuid), 1, "
                    " CAST(:lease AS uuid), CAST(:asset AS uuid), 1, 1, :snapshot, "
                    " 'pyramid-v1', 1)"
                ),
                {
                    "output": output_id,
                    "job": job_id,
                    "lease": lease_token,
                    "asset": asset_id,
                    "snapshot": "a" * 64,
                },
            )

            manifest_sha, overview_sha, tile_sha = "b" * 64, "c" * 64, "d" * 64
            inventory = {
                "schema": 1,
                "objects": {
                    ref["prefix"] + "manifest.json": {
                        "sha256": manifest_sha,
                        "size_bytes": 10,
                    },
                    ref["prefix"] + "overview.webp": {
                        "sha256": overview_sha,
                        "size_bytes": 11,
                    },
                },
            }
            await session.execute(
                text(
                    "UPDATE public.image_pyramid_outputs "
                    "SET state = 'ready', inventory = CAST(:inventory AS jsonb) "
                    "WHERE id = CAST(:output AS uuid)"
                ),
                {"output": output_id, "inventory": json.dumps(inventory)},
            )
            await session.execute(
                text(
                    "INSERT INTO aap_assets_v1.fixture_media_births "
                    "(owner_kind, owner_id, generation, run_id, dataset_id, item_id) "
                    "VALUES ('pyramid_output', CAST(:output AS uuid), 0, "
                    " CAST(:run AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid))"
                ),
                {
                    "output": output_id,
                    "run": run,
                    "dataset": dataset_id,
                    "item": item_id,
                },
            )

            members = {
                "manifest.json": (manifest_sha, 10, "verified", "written"),
                "overview.webp": (overview_sha, 11, "verified", "written"),
                "tiles/0/0/0.webp": (tile_sha, 12, "written", "written"),
            }
            operations = {}
            for relative_key, (sha, size, member_state, version_state) in members.items():
                operation_id = uuid.uuid4()
                operations[relative_key] = operation_id
                object_key = ref["prefix"] + relative_key
                await session.execute(
                    text(
                        "INSERT INTO aap_assets_v1.image_pyramid_native_members "
                        "(operation_id, output_id, relative_key, sha256, size_bytes, "
                        " state, operation_permit, etag, version_id) "
                        "VALUES (CAST(:operation AS uuid), CAST(:output AS uuid), "
                        " :relative_key, :sha256, :size_bytes, :state, "
                        " CAST(:permit AS uuid), :etag, :version_id)"
                    ),
                    {
                        "operation": operation_id,
                        "output": output_id,
                        "relative_key": relative_key,
                        "sha256": sha,
                        "size_bytes": size,
                        "state": member_state,
                        "permit": uuid.uuid4(),
                        "etag": f'"etag-{operation_id.hex[:8]}"',
                        "version_id": "null",
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO aap_assets_v1.fixture_media_versions "
                        "(id, run_id, dataset_id, item_id, task_id, execution_kind, "
                        " execution_id, execution_token, owner_kind, owner_id, "
                        " object_key, key_digest, sha256, size_bytes, state, etag, "
                        " version_id) "
                        "VALUES (CAST(:id AS uuid), CAST(:run AS uuid), "
                        " CAST(:dataset AS uuid), CAST(:item AS uuid), NULL, 'pyramid', "
                        " CAST(:job AS uuid), CAST(:lease AS uuid), 'pyramid_output', "
                        " CAST(:output AS uuid), :object_key, "
                        " pg_catalog.sha256(pg_catalog.convert_to(:object_key, 'UTF8')), "
                        " :sha256, :size_bytes, :state, :etag, :version_id)"
                    ),
                    {
                        "id": operation_id,
                        "run": run,
                        "dataset": dataset_id,
                        "item": item_id,
                        "job": job_id,
                        "lease": lease_token,
                        "output": output_id,
                        "object_key": object_key,
                        "sha256": sha,
                        "size_bytes": size,
                        "state": version_state,
                        "etag": f'"etag-{operation_id.hex[:8]}"',
                        "version_id": "null",
                    },
                )

            resolved = _reply(
                await session.scalar(
                    _RESOLVE_FIXTURE_CACHE,
                    {"key": ref["prefix"] + "manifest.json"},
                )
            )
            assert resolved == {
                "run_id": str(run),
                "write_id": str(operations["manifest.json"]),
                "proxy_origin": proxy_origin,
            }

            unknown = await session.scalar(
                _RESOLVE_FIXTURE_CACHE,
                {"key": ref["prefix"] + "tiles/9/9/9.webp"},
            )
            assert unknown is None

            # A known fixture pyramid key whose receipt is not verified is a
            # meaningful refusal, never an ordinary fallback or a silent NULL.
            with pytest.raises(DBAPIError):
                await session.scalar(
                    _RESOLVE_FIXTURE_CACHE,
                    {"key": ref["prefix"] + "tiles/0/0/0.webp"},
                )
        finally:
            await session.rollback()


async def _native_generation(db_session, *, allocate):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    item_id = await _seed_image(
        db_session, actor_id=actor_id, dataset_id=dataset_id, width=1, height=1
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
    asset_id = uuid.UUID(claim["pyramid_asset_id"])
    generation = claim["pyramid_generation"]
    allocation = None
    if allocate:
        allocation = _data(
            _reply(
                await db_session.scalar(
                    _ALLOCATE,
                    {
                        "job_id": job_id,
                        "job_generation": claim["generation"],
                        "lease_token": claim["lease_token"],
                        "asset_id": asset_id,
                        "generation": generation,
                    },
                )
            )
        )
    return job_id, asset_id, generation, claim, allocation


async def _forbid_legacy_physical_work(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("legacy pyramid GC performed physical work")

    monkeypatch.setattr(objects.physical, "delete", forbidden)
    monkeypatch.setattr(objects.storage_access, "run", forbidden)
    return calls


async def test_legacy_reconcile_never_fails_admitted_native_generation(
    db_session, monkeypatch
):
    job_id, asset_id, generation, claim, _ = await _native_generation(
        db_session, allocate=False
    )
    # No saved output exists yet: admission is the generation's own work link.
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_assets_v1.image_pyramid_native_attempts "
                "WHERE asset_id = CAST(:asset_id AS uuid)"
            ),
            {"asset_id": asset_id},
        )
        == 0
    )
    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_generations "
            "SET lease_expires_at = clock_timestamp() - interval '1 minute', "
            "    updated_at = clock_timestamp() - interval '10 days' "
            "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
        ),
        {"asset_id": asset_id, "generation": generation},
    )
    calls = await _forbid_legacy_physical_work(monkeypatch)

    assert await objects.reconcile(db_session, None) == 0
    assert calls == []
    state = (
        await db_session.execute(
            text(
                "SELECT status, lease_token FROM public.image_pyramid_generations "
                "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
            ),
            {"asset_id": asset_id, "generation": generation},
        )
    ).one()
    assert state.status == "building"
    assert state.lease_token == uuid.UUID(claim["lease_token"])

    # Admission survives the business job anchor; a vanished job is still not
    # permission for the legacy collector to mutate the generation.
    await db_session.execute(
        text("DELETE FROM public.asset_image_jobs WHERE job_id = CAST(:job_id AS uuid)"),
        {"job_id": job_id},
    )
    assert await db_session.scalar(
        text(
            "SELECT count(*) FROM public.asset_image_jobs "
            "WHERE job_id = CAST(:job_id AS uuid)"
        ),
        {"job_id": job_id},
    ) == 0
    assert (
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.legacy_pyramid_gc_eligible("
                "'pyramid_generation', CAST(:asset_id AS uuid), :generation)"
            ),
            {"asset_id": asset_id, "generation": generation},
        )
        is False
    )
    assert await objects.reconcile(db_session, None) == 0
    assert calls == []
    assert (
        await db_session.scalar(
            text(
                "SELECT status FROM public.image_pyramid_generations "
                "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
            ),
            {"asset_id": asset_id, "generation": generation},
        )
        == "building"
    )


async def test_legacy_reconcile_never_retires_owned_native_saved_output(
    db_session, monkeypatch
):
    job_id, asset_id, generation, _, allocation = await _native_generation(
        db_session, allocate=True
    )
    output_id = uuid.UUID(allocation["output_id"])
    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_outputs "
            "SET updated_at = clock_timestamp() - interval '10 days' "
            "WHERE id = CAST(:output_id AS uuid)"
        ),
        {"output_id": output_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_assets "
            "SET active_generation = NULL, building_generation = NULL "
            "WHERE id = CAST(:asset_id AS uuid)"
        ),
        {"asset_id": asset_id},
    )
    await db_session.execute(
        text(
            "UPDATE public.image_pyramid_generations "
            "SET status = 'failed', "
            "    updated_at = clock_timestamp() - interval '10 days' "
            "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
        ),
        {"asset_id": asset_id, "generation": generation},
    )
    calls = await _forbid_legacy_physical_work(monkeypatch)

    # The stale-generation pass must not delete the admitted Native row.
    assert await objects.reconcile(db_session, None) == 0
    assert calls == []
    assert (
        await db_session.scalar(
            text(
                "SELECT status FROM public.image_pyramid_generations "
                "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
            ),
            {"asset_id": asset_id, "generation": generation},
        )
        == "failed"
    )
    # The legacy collector deletes a stale generation first; the saved output's
    # immutable attempt must still be excluded after generation and job vanish.
    await db_session.execute(
        text(
            "DELETE FROM public.image_pyramid_generations "
            "WHERE asset_id = CAST(:asset_id AS uuid) AND generation = :generation"
        ),
        {"asset_id": asset_id, "generation": generation},
    )
    await db_session.execute(
        text("DELETE FROM public.asset_image_jobs WHERE job_id = CAST(:job_id AS uuid)"),
        {"job_id": job_id},
    )

    assert await objects.reconcile(db_session, None) == 0
    assert calls == []
    assert (
        await db_session.scalar(
            text(
                "SELECT state FROM public.image_pyramid_outputs "
                "WHERE id = CAST(:output_id AS uuid)"
            ),
            {"output_id": output_id},
        )
        == "building"
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_assets_v1.image_pyramid_native_attempts "
                "WHERE output_id = CAST(:output_id AS uuid)"
            ),
            {"output_id": output_id},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_assets_v1.native_pyramid_retirement_targets "
                "WHERE output_id = CAST(:output_id AS uuid)"
            ),
            {"output_id": output_id},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT aap_assets_v1.legacy_pyramid_gc_eligible("
                "'pyramid_output', CAST(:output_id AS uuid), 0)"
            ),
            {"output_id": output_id},
        )
        is False
    )

    # Retained independent Native retirement is still possible after the
    # legacy pass refused the same physical owner.
    await _make_retirement_due(db_session, output_id)
    claimed = await _claim(db_session, uuid.uuid4())
    retirement = claimed["data"]["claim"]
    assert retirement is not None
    assert retirement["output_id"] == str(output_id)
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
