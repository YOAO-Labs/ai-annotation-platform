"""Saved cache roots are closed facts and independent of credential revisions."""

from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from app.services import media_cache_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_s3_target import Target
from app.services.managed_original_retirement import managed_storage_identity


def reference(*, legacy=False, root="", family="thumbnails", generation=None):
    logical = f"{family}/objects/{uuid.uuid4()}/"
    endpoint = storage._runtime_endpoint() if legacy else "https://objects.example.test"
    bucket = "saved-cache"
    return {
        "schema": 2,
        "purpose": "media-cache",
        "location_id": str(LEGACY_LOCATION_ID if legacy else uuid.uuid4()),
        "config_revision": 1 if legacy else 3,
        "binding_generation": generation or (1 if legacy else 2),
        "endpoint_url": endpoint,
        "bucket": bucket,
        "key_prefix": "" if legacy else storage.managed_prefix(root),
        "prefix": ("" if legacy else storage.managed_prefix(root)) + logical,
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }, logical


@pytest.mark.parametrize("legacy,generation", [(True, 1), (True, 3), (False, 2)])
def test_saved_schema_two_preserves_exact_physical_root(legacy, generation):
    ref, logical = reference(legacy=legacy, generation=generation, root="公司/ 空间")
    owner = storage.resolve_ref(ref, expected_prefix=logical)
    key = owner.key("thumbnail.webp")
    assert key == ref["prefix"] + "thumbnail.webp"
    assert owner.logical_prefix == logical
    assert owner.contains(key) is None
    ref["bucket"] = "changed"
    assert owner.bucket == "saved-cache" and owner.ref["bucket"] == "saved-cache"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1),
        ("purpose", "datasets"),
        ("binding_generation", 0),
        ("config_revision", True),
        ("key_prefix", "elsewhere/"),
        ("prefix", "elsewhere/"),
        ("secret", "forbidden"),
        ("storage_identity", "f" * 64),
    ],
)
def test_invalid_ref_cannot_redirect_or_adopt_schema_one(field, value):
    ref, _ = reference()
    ref[field] = value
    with pytest.raises(storage.MediaCacheStorageError):
        storage.resolve_ref(ref)


@pytest.mark.parametrize(
    "relative", ["", "../a", "a//b", "/a", "a\\b", "a\x00", "a" * 1001, "界" * 342]
)
def test_full_key_is_bounded_and_relative(relative):
    ref, _ = reference()
    with pytest.raises(storage.MediaCacheStorageError):
        storage.resolve_ref(ref).key(relative)


def test_unicode_managed_root_budget_reserves_legacy_thumbnail_path():
    namespace = storage.MANAGED_NAMESPACE
    assert len(storage.managed_prefix("a" * (436 - len(namespace) - 1)).encode()) == 436
    with pytest.raises(storage.MediaCacheStorageError):
        storage.managed_prefix("a" * (437 - len(namespace) - 1))
    assert len(storage.managed_prefix("界" * 136).encode()) <= 436
    with pytest.raises(storage.MediaCacheStorageError):
        storage.managed_prefix("界" * 137)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_current_credentials_preserve_saved_physical_address(monkeypatch, legacy):
    ref, _ = reference(legacy=legacy)
    saved = storage.resolve_ref(ref)
    current = Target(
        saved.location_id,
        1 if legacy else 8,
        None if legacy else 9,
        ref["endpoint_url"],
        "runtime-default" if legacy else saved.bucket,
        saved.key_prefix,
        purpose="media-cache",
    )

    class DB:
        async def get(self, *args, **kwargs):
            return SimpleNamespace(id=saved.location_id)

    async def target(*args, **kwargs):
        return current

    monkeypatch.setattr(storage, "target_for", target)
    actual = await storage.target_for_ref(DB(), ref)
    assert (
        actual.bucket == saved.bucket
        and actual.config_revision == current.config_revision
    )
    current = replace(current, endpoint_url="https://changed.example.test")
    with pytest.raises(storage.MediaCacheStorageError):
        await storage.target_for_ref(DB(), ref)


async def test_allocate_legacy_uses_shared_dispatcher_without_committing_caller(
    db_session,
):
    logical = f"thumbnails/objects/{uuid.uuid4()}/"
    ref = await storage.allocate(db_session, logical)
    assert ref["schema"] == 2 and ref["binding_generation"] == 1
    assert ref["bucket"] == storage.storage_service.media_cache_bucket
    assert db_session.in_transaction()
    assert storage.ref_check("'thumbnails/objects/' || id::text || '/'")
