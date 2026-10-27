"""Durable per-submission facts at the actual media writer boundaries."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import io
import os
import re
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import media_cache_accounting as media
from app.services import image_pyramid_storage as pyramid
from app.services import original_cache as cache
from app.services import storage_access
from app.services import storage_usage_accounting as accounting
from app.services import thumbnail_outputs as thumbnail
from app.services import video_derivative_storage as video
from app.services.managed_s3_target import Target
from app.services.storage import StorageService
from tests.test_image_pyramid_storage import reference as pyramid_ref
from tests.test_original_cache import cache_world as cache_world, fill, record
from tests.test_storage_usage_accounting import (
    accounting_factory as accounting_factory,
    observe,
)
from tests.test_video_derivative_storage import Provider, reference as video_ref

_REAL_CACHE_STORAGE = cache.make_storage


def receipt(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def recorder(factory, producer="video_derivative_outputs", owner=None, bucket="saved"):
    return media.Recorder(
        factory, producer, owner or uuid.uuid4(), Provider().meta.endpoint_url, bucket
    )


async def rows(factory):
    async with factory() as db:
        return list(await db.scalars(select(Mutation).order_by(Mutation.started_at)))


def native(rec, client, *, verify=True):
    data = b"encoded-image"
    response, attempt = media.put(
        rec,
        client,
        "owned/object",
        receipt(data),
        Bucket=rec.bucket,
        Key="owned/object",
        Body=data,
        ContentLength=len(data),
        IfNoneMatch="*",
    )
    if verify:
        value = client.get_object(
            Bucket=rec.bucket,
            Key="owned/object",
            IfMatch=response["ETag"],
            **(
                {"VersionId": response["VersionId"]}
                if response.get("VersionId") not in {None, "null"}
                else {}
            ),
        )
        with storage_access.closing_registered(value["Body"]) as body:
            assert body.read() == data and body.read(1) == b""
        attempt.verified(client, value.get("VersionId"))
    client.close()


@pytest.mark.parametrize(
    "producer",
    [
        "thumbnail_outputs",
        "original_cache",
        "image_pyramid_outputs",
        "video_derivative_outputs",
    ],
)
async def test_exact_intent_commits_before_put_and_native_close_before_publish(
    accounting_factory, producer
):
    rec, client = recorder(accounting_factory, producer), Provider()
    put = client.put_object
    loop = asyncio.get_running_loop()

    async def pending():
        saved = await rows(accounting_factory)
        assert len(saved) == 1
        assert saved[0].state == "pending" and saved[0].object_key == "owned/object"
        assert saved[0].producer == producer and saved[0].owner_id == rec.owner_id

    def checked(**params):
        asyncio.run_coroutine_threadsafe(pending(), loop).result(5)
        return put(**params)

    client.put_object = checked
    await asyncio.to_thread(native, rec, client)
    assert client.closed and all(body.closed for body in client.bodies)
    await rec.finish(settled=True)
    (row,) = await rows(accounting_factory)
    assert row.state == "confirmed" and row.physical_outcome == "written"
    assert row.current_delta_bytes == len(b"encoded-image")
    assert row.evidence["full_readback"] and row.evidence["current_head_matches"]


async def test_lost_response_then_412_preserves_independent_unknown_window(
    accounting_factory,
):
    owner, client = uuid.uuid4(), Provider()
    first = recorder(accounting_factory, owner=owner)
    put = client.put_object

    def lost(**params):
        put(**params)
        raise OSError("response lost")

    client.put_object = lost
    with pytest.raises(OSError):
        await asyncio.to_thread(native, first, client)
    await first.finish(settled=True)
    client.put_object = put
    later = recorder(accounting_factory, owner=owner)
    with pytest.raises(Exception):
        await asyncio.to_thread(native, later, client)
    await later.finish(settled=True)
    one, two = await rows(accounting_factory)
    assert one.state == "unknown" and one.current_delta_bytes is None
    assert two.state == "confirmed" and two.physical_outcome == "not_written"
    assert two.current_delta_bytes == 0
    assert one.evidence["logical_intent_id"] == two.evidence["logical_intent_id"]
    assert one.evidence["attempt_nonce"] != two.evidence["attempt_nonce"]


@pytest.mark.parametrize("version", [None, "null"])
async def test_nonimmutable_version_retains_written_fact_without_delta(
    accounting_factory, version
):
    rec, client = recorder(accounting_factory), Provider()
    client.version = version
    await asyncio.to_thread(native, rec, client)
    await rec.finish(settled=True)
    (row,) = await rows(accounting_factory)
    assert row.state == "unknown" and row.physical_outcome == "written"
    assert (
        row.physical_size_bytes == len(b"encoded-image")
        and row.current_delta_bytes is None
    )


@pytest.mark.parametrize(
    "change",
    [
        "old_read",
        "replaced",
        "head_failure",
        "read_failure",
        "body_close_failure",
        "close_failure",
    ],
)
async def test_unproven_read_or_current_head_never_confirms(accounting_factory, change):
    rec, client = recorder(accounting_factory), Provider()
    if change == "old_read":
        original = client.get_object

        def old(**params):
            response = original(**params)
            response["VersionId"] = "old-version"
            return response

        client.get_object = old
    elif change == "replaced":
        original = client.head_object
        client.head_object = lambda **params: {
            **original(**params),
            "VersionId": "replacement",
        }
    elif change == "head_failure":

        def fail(**params):
            raise OSError("HEAD unavailable")

        client.head_object = fail
    elif change == "read_failure":

        class Broken(io.BytesIO):
            def read(self, *args):
                raise OSError("GET interrupted")

        client.body_factory = Broken
    elif change == "body_close_failure":

        class BrokenClose(io.BytesIO):
            def close(self):
                super().close()
                raise storage_access.mark_unsettled(OSError("body close failed"))

        client.body_factory = BrokenClose
    elif change == "close_failure":

        def fail():
            raise storage_access.mark_unsettled(OSError("close failed"))

        client.close = fail
    failure = None
    try:
        await asyncio.to_thread(native, rec, client)
    except OSError as exc:
        failure = exc
    await rec.finish(settled=not getattr(failure, "storage_operation_unsettled", False))
    (row,) = await rows(accounting_factory)
    assert row.state == "unknown" and row.current_delta_bytes is None
    assert row.physical_outcome == "written" and row.physical_size_bytes == len(
        b"encoded-image"
    )


async def test_publication_failure_keeps_business_success_and_pending_intent(
    accounting_factory, monkeypatch
):
    rec, client = recorder(accounting_factory), Provider()
    await asyncio.to_thread(native, rec, client)

    async def fail(*args, **kwargs):
        raise OSError("ledger publication unavailable")

    monkeypatch.setattr(accounting, "publish", fail)
    await rec.finish(settled=True)
    (row,) = await rows(accounting_factory)
    assert row.state == "pending" and row.current_delta_bytes is None
    assert client.objects[(rec.bucket, "owned/object")] == b"encoded-image"


async def test_finish_cancellation_drains_owned_publication_then_propagates(
    accounting_factory, monkeypatch
):
    rec, client = recorder(accounting_factory), Provider()
    await asyncio.to_thread(native, rec, client)
    started, release = asyncio.Event(), asyncio.Event()
    publish = accounting.publish

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        await publish(*args, **kwargs)

    monkeypatch.setattr(accounting, "publish", blocked)
    task = asyncio.create_task(rec.finish(settled=True))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    (row,) = await rows(accounting_factory)
    assert row.state == "confirmed"


async def test_video_partial_failure_keeps_already_closed_object_fact(
    accounting_factory, tmp_path
):
    rec, client = recorder(accounting_factory), Provider()
    row = SimpleNamespace(
        id=rec.owner_id, storage_ref=video_ref(rec.owner_id, rec.bucket)
    )
    path = tmp_path / "poster.webp"
    path.write_bytes(b"small-webp")

    def work():
        video.upload_file(
            SimpleNamespace(client=client),
            row,
            path,
            "poster.webp",
            "image/webp",
            accounting=rec,
        )

        def broken(**params):
            raise OSError("second PUT lost")

        client.put_object = broken
        try:
            video.write_result(
                SimpleNamespace(client=client), row, {"schema": 1}, accounting=rec
            )
        finally:
            client.close()

    with pytest.raises(video.VideoDerivativeStorageError):
        await asyncio.to_thread(work)
    await rec.finish(settled=True)
    one, two = await rows(accounting_factory)
    assert one.state == "confirmed" and one.current_delta_bytes == len(b"small-webp")
    assert two.state == "unknown" and two.current_delta_bytes is None


async def test_pyramid_partial_put_retains_written_without_inventing_readback(
    accounting_factory, tmp_path
):
    rec, client = recorder(accounting_factory, "image_pyramid_outputs"), Provider()
    row = SimpleNamespace(
        id=rec.owner_id, storage_ref=pyramid_ref(rec.owner_id, rec.bucket)
    )
    tile, overview = tmp_path / "tile.webp", tmp_path / "overview.webp"
    tile.write_bytes(b"tile")
    overview.write_bytes(b"overview")
    put = client.put_object
    calls = []

    def partial(**params):
        calls.append(params["Key"])
        if len(calls) == 2:
            raise OSError("second PUT lost")
        return put(**params)

    client.put_object = partial
    with pytest.raises(pyramid.ImagePyramidStorageError):
        await asyncio.to_thread(
            pyramid.upload_and_verify,
            SimpleNamespace(client=client),
            row,
            [(tile, 0, 0, 0)],
            overview,
            b"{}",
            float("inf"),
            accounting=rec,
        )
    client.close()
    await rec.finish(settled=True)
    one, two = await rows(accounting_factory)
    assert one.state == two.state == "unknown"
    assert one.physical_outcome == "written" and one.physical_size_bytes == 4
    assert two.physical_outcome == "unknown"


async def test_media_fact_survives_business_rollback_and_reconciles(accounting_factory):
    rec, client = recorder(accounting_factory), Provider()
    start = accounting.now() - timedelta(seconds=10)
    scope = accounting.scope_key(rec.endpoint_url, rec.bucket)
    async with accounting_factory() as db:
        await observe(db, scope, uuid.uuid4(), [], start, start + timedelta(seconds=1))
        await db.commit()
    await asyncio.to_thread(native, rec, client)
    await rec.finish(settled=True)
    async with accounting_factory() as business:
        await business.execute(select(Mutation))
        await business.rollback()
    end = accounting.now() + timedelta(seconds=1)
    async with accounting_factory() as db:
        result = await observe(
            db,
            scope,
            uuid.uuid4(),
            [{"key": "owned/object", "size": len(b"encoded-image")}],
            end,
            end + timedelta(seconds=1),
        )
        assert result.summary["confirmed_change_bytes"] == len(b"encoded-image")
        assert result.summary["unexplained_change_bytes"] == 0


def test_owned_single_attempt_client_preserves_default_shared_client(monkeypatch):
    from app.services import storage as storage_module

    shared, owned, options = object(), Provider(), []

    def factory(**kwargs):
        options.append(kwargs["client_config"])
        return SimpleNamespace(client=owned)

    monkeypatch.setattr(storage_module, "StorageService", factory)
    target = Target(
        uuid.uuid4(),
        1,
        None,
        owned.meta.endpoint_url,
        "saved",
        "",
        _legacy_client=shared,
    )
    with target.client() as value:
        assert value is shared
    with target.client(single_attempt=True, read_timeout=15) as value:
        assert value is owned and not owned.closed
        assert options[0].retries == {"total_max_attempts": 1, "mode": "standard"}
        assert options[0].read_timeout == 15
    assert owned.closed


def test_owned_constructor_failure_preserves_unsettled_transport(monkeypatch):
    from app.services import storage as storage_module

    def broken(**kwargs):
        raise OSError("transport construction failed")

    monkeypatch.setattr(storage_module, "StorageService", broken)
    target = Target(uuid.uuid4(), 1, None, Provider().meta.endpoint_url, "saved", "")
    with pytest.raises(OSError) as failure:
        with target.client(single_attempt=True):
            pytest.fail("the failed constructor cannot yield a transport")
    assert failure.value.storage_operation_unsettled


@pytest.fixture
def live_storage():
    if os.environ.get("AAP_WORKTREE_MODE") != "test":
        pytest.skip("Actual provider checks require a disposable worktree test mode")
    bucket = settings.minio_media_cache_bucket
    assert re.fullmatch(r"aap-wt-[0-9a-f]{16}-test-media-cache", bucket)
    storage = StorageService()
    storage.client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    try:
        yield storage
    finally:
        storage.client.close()


def delete_versions(storage, bucket, prefix):
    page = storage.client.list_object_versions(Bucket=bucket, Prefix=prefix)
    owned = [
        {"Key": obj["Key"], "VersionId": obj["VersionId"]}
        for obj in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
    ]
    if owned:
        storage.client.delete_objects(Bucket=bucket, Delete={"Objects": owned})


@pytest.mark.parametrize(
    "producer",
    ["thumbnail_outputs", "image_pyramid_outputs", "video_derivative_outputs"],
)
async def test_actual_minio_writer_seams_match_current_bytes(
    accounting_factory, live_storage, tmp_path, producer, monkeypatch
):
    bucket = settings.minio_media_cache_bucket
    rec = recorder(accounting_factory, producer, bucket=bucket)
    client = live_storage.client
    prefixes = {
        "thumbnail_outputs": thumbnail.prefix_for,
        "image_pyramid_outputs": pyramid.prefix_for,
        "video_derivative_outputs": video.prefix_for,
    }
    prefix = prefixes[producer](rec.owner_id)
    with io.BytesIO() as encoded:
        with Image.new("RGB", (1, 1), (10, 20, 30)) as image:
            image.save(encoded, "WEBP")
        data = encoded.getvalue()
    path = tmp_path / "output.webp"
    path.write_bytes(data)
    try:
        if producer == "thumbnail_outputs":
            ref = pyramid_ref(rec.owner_id, bucket)
            ref.update(schema=2, prefix=prefix, key_prefix="")

            @contextmanager
            def transport(**kwargs):
                yield client

            await asyncio.to_thread(
                thumbnail._write,
                SimpleNamespace(client=transport),
                ref,
                data,
                accounting=rec,
            )
        elif producer == "image_pyramid_outputs":
            ref = {**pyramid_ref(rec.owner_id, bucket), "schema": 2, "key_prefix": ""}
            output = SimpleNamespace(id=rec.owner_id, storage_ref=ref)
            from app.workers import image_pyramid as worker

            def generated(source_path, output_dir, **kwargs):
                # Prefabricated encoding isolates the real builder/upload seam
                # from native libvips qualification.
                root = output_dir / "tiles"
                (root / "0").mkdir(parents=True)
                (root / "0" / "0_0.webp").write_bytes(data)
                overview = output_dir / "overview.webp"
                overview.write_bytes(data)
                return root, overview, 1, 1, 1, 1

            monkeypatch.setattr(worker, "_generate_local", generated)
            with storage_access.closing_registered(path.open("rb")) as source:
                await asyncio.to_thread(
                    worker._build_generation,
                    storage=live_storage,
                    owner=SimpleNamespace(
                        source=SimpleNamespace(sha256=receipt(data)["sha256"])
                    ),
                    asset_id=uuid.uuid4(),
                    generation_number=1,
                    expected_source_size=len(data),
                    stage=SimpleNamespace(
                        file=source, sha256=receipt(data)["sha256"], size=len(data)
                    ),
                    output=output,
                    accounting=rec,
                )
        else:
            ref = {**video_ref(rec.owner_id, bucket), "schema": 2, "key_prefix": ""}
            output = SimpleNamespace(id=rec.owner_id, storage_ref=ref)
            await asyncio.to_thread(
                video.upload_file,
                live_storage,
                output,
                path,
                "poster.webp",
                "image/webp",
                accounting=rec,
            )
            await asyncio.to_thread(
                video.write_result, live_storage, output, {"schema": 1}, accounting=rec
            )
        client.close()
        await rec.finish(settled=True)
        saved = await rows(accounting_factory)
        assert (
            len(saved)
            == {
                "thumbnail_outputs": 1,
                "image_pyramid_outputs": 3,
                "video_derivative_outputs": 2,
            }[producer]
        )
        current = StorageService()
        try:
            visible = current.client.list_objects_v2(Bucket=bucket, Prefix=prefix).get(
                "Contents", []
            )
            assert {row.object_key for row in saved} == {obj["Key"] for obj in visible}
            assert sum(row.current_delta_bytes for row in saved) == sum(
                obj["Size"] for obj in visible
            )
            assert all(
                row.state == "confirmed"
                and row.evidence["returned_version"] not in {None, "null"}
                for row in saved
            )
        finally:
            current.client.close()
    finally:
        cleanup = StorageService()
        try:
            await asyncio.to_thread(delete_versions, cleanup, bucket, prefix)
        finally:
            cleanup.client.close()


async def test_actual_minio_original_cache_production_store_matches_saved_target(
    cache_world, live_storage, monkeypatch
):
    world = cache_world
    monkeypatch.setattr(cache, "make_storage", _REAL_CACHE_STORAGE)
    entry = None
    try:
        assert await fill(world) == "stored"
        entry = await record(world)
        async with world.factory() as db:
            saved = list(
                await db.scalars(
                    select(Mutation).where(
                        Mutation.owner_id == entry.id,
                        Mutation.producer == "original_cache",
                    )
                )
            )
        (row,) = saved
        head = await asyncio.to_thread(
            live_storage.client.head_object,
            Bucket=entry.bucket,
            Key=cache.object_key(entry),
        )
        assert row.object_key == cache.object_key(entry)
        assert row.state == "confirmed" and row.current_delta_bytes == head[
            "ContentLength"
        ] == len(world.data)
        assert row.evidence["returned_version"] == head["VersionId"]
    finally:
        if entry is not None:
            await asyncio.to_thread(
                delete_versions, live_storage, entry.bucket, cache.object_key(entry)
            )
