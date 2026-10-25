"""Saved output addresses, current transport and exact browser capability wiring."""

from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import image_pyramid_objects as images
from app.services import image_pyramid_storage as image_storage
from app.services import media_cache_storage, media_cache_urls
from app.services import video_derivative_storage as video_storage
from app.services.content_snapshot import ContentError
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_image_pyramid_storage import Provider as ImageProvider
from tests.test_video_derivative_storage import Provider as VideoProvider
from tests.test_dataset_original_reads import saved_db as saved_db


def reference(storage, identifier):
    root = "公司/_aap_managed/media-cache/"
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(uuid.UUID(int=2032)),
        "config_revision": 2,
        "binding_generation": 2,
        "endpoint_url": "https://saved-cache.example.test",
        "bucket": "saved-cache-b",
        "key_prefix": root,
        "prefix": root + storage.prefix_for(identifier),
        "storage_identity": managed_storage_identity(
            "https://saved-cache.example.test", "saved-cache-b"
        ),
    }


@pytest.mark.parametrize("kind", ["image-pyramid", "video-derivative"])
def test_b_outputs_verify_sign_and_delete_only_the_saved_unicode_prefix(
    tmp_path, monkeypatch, kind
):
    storage, provider = (
        (image_storage, ImageProvider())
        if kind == "image-pyramid"
        else (video_storage, VideoProvider())
    )
    identifier = uuid.uuid4()
    ref = reference(storage, identifier)
    provider.meta.endpoint_url = ref["endpoint_url"]
    monkeypatch.setattr(
        storage,
        "storage_service",
        SimpleNamespace(
            client=SimpleNamespace(meta=SimpleNamespace(endpoint_url="http://wrong-a")),
            media_cache_bucket="wrong-current-bucket",
        ),
    )
    row = SimpleNamespace(id=identifier, storage_ref=ref, inventory=None, result=None)
    owner = storage.resolve(row)
    transport = SimpleNamespace(
        client=provider,
        generate_download_url=lambda *a, **k: pytest.fail("B used legacy URL signing"),
    )
    path = tmp_path / "derived.webp"
    path.write_bytes(b"small synthetic derived bytes")
    if kind == "image-pyramid":
        result = storage.upload_and_verify(
            transport,
            row,
            [(path, 0, 0, 0)],
            path,
            json.dumps({"schema": "aap-image-pyramid/v1"}).encode(),
            float("inf"),
        )
        row.inventory = result[3]
        key = owner.key("overview.webp")
        storage.verify_object(transport, row, key)
    else:
        receipt = storage.upload_file(transport, row, path, "frame.webp", "image/webp")
        row.result = {"schema": 1, "frame": receipt}
        metadata = storage.write_result(transport, row, row.result)
        key = owner.key("frame.webp")
        row.inventory = {
            "schema": 1,
            "objects": {key: receipt, owner.key("metadata.json"): metadata},
        }
        storage.verify_inventory(transport, row, row.inventory)
    frozen = deepcopy((ref, row.inventory))
    calls = len(provider.calls)
    url = storage.signed_url(transport, row, key, expires=23)
    assert len(provider.calls) == calls
    parsed = urlsplit(url)
    assert parsed.path.startswith(f"/api/v1/media-cache/{kind}/{identifier}/")
    assert not parsed.netloc and "saved-cache.example" not in url
    capability = media_cache_urls.decode(
        parse_qs(parsed.query)["token"][0],
        kind=kind,
        output_id=identifier,
        relative=key[len(owner.prefix) :],
    )
    assert capability.reference == media_cache_urls.reference_digest(ref)
    assert capability.sha256 == row.inventory["objects"][key]["sha256"]
    assert (ref, row.inventory) == frozen
    with pytest.raises(ValueError):
        owner.key("../another-output")
    other = ref["key_prefix"] + storage.prefix_for(uuid.uuid4()) + "unrelated.webp"
    provider.objects[(owner.bucket, other)] = b"other owner"
    if kind == "image-pyramid":
        storage.delete(transport, row)
    else:
        storage.delete_output(transport, row)
    assert provider.objects == {(owner.bucket, other): b"other owner"}
    assert all(
        params["Bucket"] == owner.bucket
        for operation, params in provider.calls
        if "Bucket" in params
    )
    assert all(body.closed for body in provider.bodies)


@pytest.mark.parametrize("storage", [image_storage, video_storage])
async def test_transport_metadata_uses_owned_session_and_current_credentials(
    monkeypatch, storage
):
    row = SimpleNamespace(id=uuid.uuid4())
    row.storage_ref = reference(storage, row.id)
    session = SimpleNamespace()
    business_db = object()

    class Session:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(storage.storage_access, "engine", lambda db: "owned-engine")
    monkeypatch.setattr(storage, "AsyncSession", lambda **kwargs: Session())
    target = SimpleNamespace(secret="current-rotated", config_revision=8)
    capture = AsyncMock(return_value=target)
    monkeypatch.setattr(media_cache_storage, "target_for_ref", capture)
    assert await storage.transport_target(business_db, row) is target
    capture.assert_awaited_once_with(session, row.storage_ref, require_enabled=True)
    assert row.storage_ref["config_revision"] == 2


@pytest.mark.parametrize("storage", [image_storage, video_storage])
async def test_saved_b_transport_rotation_and_disabled_cleanup_use_real_metadata(
    saved_db, storage
):
    db = saved_db
    identifier = uuid.uuid4()
    ref = reference(storage, identifier)
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="output-target-" + uuid.uuid4().hex,
        kind="s3",
        scope="global",
        revision=2,
        config={
            "endpoint": ref["endpoint_url"],
            "bucket": ref["bucket"],
            "base_prefix": "公司",
            "use_ssl": True,
        },
        secret_enc=b"saved-output-credentials",
    )
    location = StorageLocation(
        id=uuid.UUID(ref["location_id"]),
        name="output-location-" + uuid.uuid4().hex,
        kind="s3",
        config={},
        config_revision=2,
        connection_id=connection.id,
        enabled=True,
    )
    db.add_all([connection, location])
    await db.commit()
    output = SimpleNamespace(id=identifier, storage_ref=ref)
    original = deepcopy(ref)
    first = await storage.transport_target(db, output)
    assert first._access.secret_enc == b"saved-output-credentials"
    connection.secret_enc = b"rotated-output-credentials"
    connection.revision = location.config_revision = 3
    await db.commit()
    current = await storage.transport_target(db, output)
    assert current._access.secret_enc == b"rotated-output-credentials"
    assert current.config_revision == current.connection_revision == 3
    assert output.storage_ref == original and original["config_revision"] == 2
    location.enabled = False
    await db.commit()
    with pytest.raises(ValueError):
        await storage.transport_target(db, output)
    cleanup = await storage.transport_target(db, output, require_enabled=False)
    assert (
        cleanup.endpoint_url == ref["endpoint_url"] and cleanup.bucket == ref["bucket"]
    )
    connection.config = {**connection.config, "base_prefix": "redirected"}
    await db.commit()
    with pytest.raises(ValueError):
        await storage.transport_target(db, output, require_enabled=False)


@pytest.mark.parametrize("storage", [image_storage, video_storage])
def test_target_adapter_closes_registered_context_without_legacy_construction(storage):
    events = []
    client = object()

    @contextmanager
    def context(*, read_timeout):
        events.append(("open", read_timeout))
        try:
            yield client
        finally:
            events.append(("close",))

    target = SimpleNamespace(client=context, bucket="saved-cache-b")
    with storage.open_storage(target=target) as adapter:
        assert adapter.client is client and adapter.media_cache_bucket == target.bucket
    assert events == [("open", 30), ("close",)]


@pytest.mark.parametrize("storage", [image_storage, video_storage])
def test_target_adapter_preserves_caller_content_failure_and_closes(storage):
    closed = []

    @contextmanager
    def client(**options):
        try:
            yield object()
        finally:
            closed.append(True)

    target = SimpleNamespace(client=client, bucket="saved-cache-b")
    failure = ContentError("saved_metadata_invalid")
    with pytest.raises(ContentError) as caught:
        with storage.open_storage(target=target):
            raise failure
    assert caught.value is failure and closed == [True]


async def test_image_transport_closes_late_open_and_preserves_exception(monkeypatch):
    opened, closed = [], []
    target = object()
    monkeypatch.setattr(
        image_storage, "transport_target", AsyncMock(return_value=target)
    )

    @contextmanager
    def context(**kwargs):
        assert kwargs == {"target": target}
        opened.append(True)
        try:
            yield "saved transport"
        finally:
            closed.append(True)

    monkeypatch.setattr(image_storage, "open_storage", context)
    with pytest.raises(RuntimeError, match="caller interrupted"):
        async with images.transport(object(), object()) as adapter:
            assert adapter == "saved transport"
            raise RuntimeError("caller interrupted")
    assert opened == closed == [True]
