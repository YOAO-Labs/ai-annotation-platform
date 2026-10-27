"""Pure ownership and allocation tests; no database fixtures or physical I/O."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.config import settings
from app.db.models.dataset_asset import StorageBinding
from app.services import avatar_storage as owners
from app.services.avatar_image import MAX_AVATAR_FILE_BYTES
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

TOKEN = "0123456789abcdef0123456789abcdef"


def endpoint():
    return f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )


def reference(*, bucket="avatars-original", token=TOKEN):
    return {
        "schema": 1,
        "purpose": "avatars",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint(),
        "bucket": bucket,
        "object_key": f"{token}.webp",
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }


def record(**changes):
    return SimpleNamespace(
        **{
            "token": TOKEN,
            "storage_ref": reference(),
            "sha256": "a" * 64,
            "size_bytes": 101,
            **changes,
        }
    )


@pytest.fixture
def storage(monkeypatch):
    fake = SimpleNamespace(
        avatars_bucket="avatars-current",
        client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint())),
    )
    monkeypatch.setattr(owners, "storage_service", fake)
    return fake


def test_resolve_saved_bucket_independently_of_current_bucket(storage):
    row = record()
    saved = owners.resolve(row)
    assert saved.bucket == "avatars-original"
    assert saved.object_key == f"{TOKEN}.webp"
    storage.avatars_bucket = "avatars-new-allocation"
    assert owners.resolve(row).ref == saved.ref


def test_resolved_ref_and_returned_ref_are_independent_copies(storage):
    ref = reference()
    saved = owners.resolve_ref(ref)
    ref["bucket"] = "changed-caller"
    exported = saved.ref
    exported["object_key"] = "changed-export"
    assert saved.ref == reference()


@pytest.mark.parametrize("value", [None, {}, [], "text", True])
def test_missing_or_wrong_envelope_is_not_adopted(storage, value):
    with pytest.raises(owners.AvatarStorageError) as caught:
        owners.resolve_ref(value)
    assert caught.value.status_code == 409
    assert caught.value.code == (
        "avatar_storage_unpinned"
        if value is None
        else "avatar_storage_identity_changed"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.uuid4())),
        ("location_id", uuid.uuid4()),
        ("config_revision", True),
        ("config_revision", 1.0),
        ("binding_generation", 2),
        ("binding_generation", True),
        ("bucket", ""),
        ("bucket", "space bucket"),
        ("bucket", "foreign/key"),
        ("bucket", "foreign\\key"),
        ("bucket", "foreign?key"),
        ("bucket", "foreign#key"),
        ("bucket", "foreign@key"),
        ("bucket", "中文"),
        ("object_key", "../avatar.webp"),
        ("object_key", f"{TOKEN}.webp/extra"),
        ("object_key", f"{TOKEN.upper()}.webp"),
        ("object_key", f"{TOKEN}.png"),
        ("storage_identity", "a" * 64),
        ("storage_identity", None),
        ("endpoint_url", "http://other.invalid"),
        ("endpoint_url", "http://user:secret@storage.invalid"),
        ("endpoint_url", "http://storage.invalid/path"),
        ("endpoint_url", "http://storage.invalid?signature=secret"),
        ("endpoint_url", "http://storage.invalid/"),
        ("endpoint_url", "http://storage.invalid:99999"),
        ("endpoint_url", "http://storage.invalid\\path"),
    ],
)
def test_rejects_invalid_physical_locator_before_io(storage, field, value):
    ref = reference()
    ref[field] = value
    with pytest.raises(owners.AvatarStorageError) as caught:
        owners.resolve_ref(ref)
    assert caught.value.code == "avatar_storage_identity_changed"
    assert "secret" not in str(caught.value)


def test_envelope_rejects_unknown_credential_field(storage):
    with pytest.raises(owners.AvatarStorageError):
        owners.resolve_ref({**reference(), "secret": "credential"})


def test_saved_key_must_match_exact_upload_token(storage):
    with pytest.raises(owners.AvatarStorageError):
        owners.resolve_ref(reference(), expected_key=f"{'b' * 32}.webp")
    with pytest.raises(owners.AvatarStorageError):
        owners.resolve(record(token="b" * 32))


def test_actual_client_endpoint_is_checked(storage):
    storage.client.meta.endpoint_url = "http://another-endpoint.invalid"
    with pytest.raises(owners.AvatarStorageError) as caught:
        owners.resolve(record())
    assert caught.value.code == "avatar_storage_identity_changed"


@pytest.mark.parametrize(
    "token", [None, 123, "", "A" * 32, "a" * 31, "../avatar", "a" * 33]
)
def test_object_key_requires_canonical_token(token):
    with pytest.raises(owners.AvatarStorageError) as caught:
        owners.object_key_for(token)
    assert caught.value.code == "avatar_storage_invalid_key"


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": ""},
        {"sha256": "A" * 64},
        {"sha256": 1},
        {"size_bytes": True},
        {"size_bytes": 1.0},
        {"size_bytes": 0},
        {"size_bytes": MAX_AVATAR_FILE_BYTES + 1},
    ],
)
def test_content_receipt_requires_strict_sha_and_bounded_size(storage, changes):
    with pytest.raises(owners.AvatarStorageError) as caught:
        owners.resolve(record(**changes))
    assert caught.value.code == "avatar_storage_invalid_receipt"


def allocation_db(storage):
    location = SimpleNamespace(
        id=LEGACY_LOCATION_ID,
        kind="s3",
        config_revision=1,
        enabled=True,
        config={"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    binding = SimpleNamespace(
        location_id=LEGACY_LOCATION_ID, generation=1, bucket=storage.avatars_bucket
    )

    async def selected(statement):
        entity = statement.column_descriptions[0]["entity"]
        return binding if entity is StorageBinding else location

    db = SimpleNamespace(
        get=AsyncMock(return_value=location), scalar=AsyncMock(side_effect=selected)
    )
    return db, location, binding


async def test_new_allocation_uses_verified_persisted_binding_only(
    storage, monkeypatch
):
    db, _, _ = allocation_db(storage)
    ensure = AsyncMock()
    monkeypatch.setattr(owners, "ensure_legacy_bindings", ensure)
    original = reference(bucket=storage.avatars_bucket)
    assert await owners.allocate(db, TOKEN) == original
    ensure.assert_awaited_once_with(db)
    db.get.assert_awaited_once()
    statement = db.scalar.await_args.args[0]
    assert statement.compile().params == {"purpose_1": "avatars"}


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("location", "id", uuid.uuid4()),
        ("location", "kind", "filesystem"),
        ("location", "enabled", False),
        ("location", "config_revision", 2),
        ("location", "config", {"endpoint": "other.invalid", "use_ssl": False}),
        ("binding", "location_id", uuid.uuid4()),
        ("binding", "generation", 0),
        ("binding", "bucket", "historical-is-not-new-write-target"),
    ],
)
async def test_new_allocation_rejects_changed_binding(
    storage, monkeypatch, target, field, value
):
    db, location, binding = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    setattr(location if target == "location" else binding, field, deepcopy(value))
    with pytest.raises(owners.AvatarStorageError) as caught:
        await owners.allocate(db, TOKEN)
    assert caught.value.code == "avatar_storage_binding_unproven"


@pytest.mark.parametrize("missing", ["location", "binding"])
async def test_new_allocation_rejects_missing_binding(storage, monkeypatch, missing):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    mock = db.get if missing == "location" else db.scalar
    mock.side_effect = None
    mock.return_value = None
    with pytest.raises(owners.AvatarStorageError) as caught:
        await owners.allocate(db, TOKEN)
    assert caught.value.code == "avatar_storage_binding_unproven"


async def test_new_allocation_does_not_hide_bootstrap_conflict(storage, monkeypatch):
    db, _, _ = allocation_db(storage)
    monkeypatch.setattr(
        owners, "ensure_legacy_bindings", AsyncMock(side_effect=ValueError("secret"))
    )
    with pytest.raises(owners.AvatarStorageError) as caught:
        await owners.allocate(db, TOKEN)
    assert caught.value.code == "avatar_storage_binding_unproven"
    assert "secret" not in str(caught.value)
    db.get.assert_not_awaited()


async def test_switch_back_to_legacy_keeps_new_binding_generation(storage, monkeypatch):
    db, _, binding = allocation_db(storage)
    monkeypatch.setattr(owners, "ensure_legacy_bindings", AsyncMock())
    binding.generation = 3
    result = await owners.allocate(db, TOKEN)
    assert result == {
        **reference(bucket=storage.avatars_bucket),
        "schema": 2,
        "binding_generation": 3,
    }


def external_reference(**changes):
    endpoint_url = "https://avatar-storage.example.test"
    bucket = "registered-avatars"
    return {
        **reference(),
        "schema": 2,
        "location_id": str(uuid.uuid4()),
        "config_revision": 4,
        "binding_generation": 2,
        "endpoint_url": endpoint_url,
        "bucket": bucket,
        "object_key": f"team/图片/_aap_managed/avatars/{TOKEN}.webp",
        "storage_identity": managed_storage_identity(endpoint_url, bucket),
        **changes,
    }


def test_schema2_saved_owner_keeps_registered_endpoint_and_full_prefix(storage):
    ref = external_reference()
    saved = owners.resolve_ref(ref, expected_key=f"{TOKEN}.webp")
    assert saved.endpoint_url == ref["endpoint_url"]
    assert saved.object_key == ref["object_key"]
    storage.client.meta.endpoint_url = "https://unrelated-current.example.test"
    assert owners.resolve_ref(ref).ref == ref


@pytest.mark.parametrize(
    "key",
    [
        f"{TOKEN}.webp",
        f"/_aap_managed/avatars/{TOKEN}.webp",
        f"team//_aap_managed/avatars/{TOKEN}.webp",
        f"../_aap_managed/avatars/{TOKEN}.webp",
        f"team/./_aap_managed/avatars/{TOKEN}.webp",
        f"team\\x/_aap_managed/avatars/{TOKEN}.webp",
        f"team\x00/_aap_managed/avatars/{TOKEN}.webp",
        f"team/_aap_managed/avatars/{'b' * 32}.webp",
        f"{'a' * 1000}/_aap_managed/avatars/{TOKEN}.webp",
    ],
)
def test_schema2_rejects_noncanonical_key_and_wrong_token(storage, key):
    with pytest.raises(owners.AvatarStorageError):
        owners.resolve_ref(
            external_reference(object_key=key), expected_key=f"{TOKEN}.webp"
        )


@pytest.mark.parametrize("version", ["", True, 1, "x" * 1025, "version\nprivate"])
def test_provider_version_receipt_is_bounded(storage, version):
    with pytest.raises(owners.AvatarStorageError):
        owners.resolve(record(object_version_id=version))
