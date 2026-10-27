"""Closed saved dataset addresses and physical rather than revision identity."""

from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from app.services import dataset_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.dataset_storage_target import managed_prefix
from app.services.managed_s3_target import Target


def target(*, legacy=False, prefix=""):
    return Target(
        LEGACY_LOCATION_ID
        if legacy
        else uuid.UUID("56df6328-fcd3-4a0e-8b91-d77e2c76448d"),
        1 if legacy else 3,
        None if legacy else 4,
        storage._runtime_endpoint() if legacy else "https://objects.example.test",
        "saved-datasets",
        "" if legacy else managed_prefix(prefix),
        purpose="datasets",
    )


@pytest.mark.parametrize("legacy,generation", [(True, 1), (True, 3), (False, 2)])
def test_saved_reference_is_closed_and_keeps_exact_key(legacy, generation):
    destination = target(legacy=legacy)
    key = destination.key_prefix + "__aap_managed_datasets__/file.bin"
    reference = storage.make_ref(destination, generation, key)
    saved = storage.resolve_ref(reference)
    assert saved.object_key == key and saved.binding_generation == generation
    reference["bucket"] = "tampered"
    assert saved.bucket == "saved-datasets"
    assert saved.ref["schema"] == 1 and saved.ref["purpose"] == "datasets"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("binding_generation", 0),
        ("config_revision", True),
        ("purpose", "annotations"),
        ("storage_identity", "f" * 64),
        ("key_prefix", "elsewhere/"),
        ("object_key", "elsewhere/file"),
        ("secret", "forbidden"),
    ],
)
def test_invalid_reference_cannot_redirect(field, value):
    destination = target()
    reference = storage.make_ref(destination, 2, destination.key_prefix + "file")
    reference[field] = value
    with pytest.raises(storage.DatasetStorageError):
        storage.resolve_ref(reference)


def test_full_key_and_root_have_character_and_byte_budgets():
    namespace = "_aap_managed/datasets/"
    assert len(managed_prefix("a" * (891 - len(namespace) - 1))) == 891
    with pytest.raises(storage.DatasetStorageError):
        managed_prefix("a" * (892 - len(namespace) - 1))
    assert storage.valid_key("a" * 1000)
    assert not storage.valid_key("a" * 1001)
    assert storage.valid_key("界" * 341)
    assert not storage.valid_key("界" * 342)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_current_credentials_do_not_reinterpret_saved_bucket_or_revision(
    monkeypatch, legacy
):
    destination = target(legacy=legacy)
    reference = storage.make_ref(
        destination, 1 if legacy else 2, destination.key_prefix + "file"
    )
    current = replace(
        destination,
        config_revision=1 if legacy else 8,
        connection_revision=None if legacy else 9,
        bucket="runtime-default" if legacy else destination.bucket,
    )

    class DB:
        async def get(self, *args, **kwargs):
            return SimpleNamespace(id=destination.location_id)

    async def current_target(*args, **kwargs):
        return current

    monkeypatch.setattr(
        "app.services.dataset_storage_target.target_for", current_target
    )
    actual = await storage.target_for_ref(DB(), reference)
    assert actual.bucket == destination.bucket
    assert actual.config_revision == current.config_revision
    if not legacy:
        current = replace(current, endpoint_url="https://changed.example.test")
        with pytest.raises(storage.DatasetStorageError):
            await storage.target_for_ref(DB(), reference)
