"""A retained version never follows the current training-purpose bucket."""

from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services import training_dataset_storage as service
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.factory import create_project


@pytest.fixture
def version():
    project_id, version_id = uuid.uuid4(), uuid.uuid4()
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return SimpleNamespace(
        id=version_id,
        project_id=project_id,
        storage_ref={
            "schema": 1,
            "purpose": "training-dataset",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": "original-retained-bucket",
            "prefix": service.prefix_for(project_id, version_id),
            "storage_identity": managed_storage_identity(
                endpoint, "original-retained-bucket"
            ),
            "origin": "new",
            "evidence": None,
        },
    )


def test_resolve_keeps_original_bucket_when_current_purpose_changes(
    version, monkeypatch
):
    monkeypatch.setattr(storage_service, "training_bucket", "wrong-new-purpose-bucket")
    owner = service.resolve(version)
    assert owner.bucket == "original-retained-bucket"
    key = owner.key("attempts/a/dataset.zip")
    assert (
        key
        == f"projects/{version.project_id}/datasets/{version.id}/attempts/a/dataset.zip"
    )
    assert owner.contains(key)
    returned = owner.ref
    returned["bucket"] = "mutated"
    assert owner.ref["bucket"] == "original-retained-bucket"
    version.storage_ref["bucket"] = "mutated-again"
    assert owner.ref["bucket"] == "original-retained-bucket"


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/outside",
        "../outside",
        "a/../outside",
        "a/./b",
        "a//b",
        "a/",
        "a\\b",
        "a\x00b",
        "a\nb",
        "__aap_reference__/opaque",
    ],
)
def test_resolve_ref_rejects_unsafe_relative_keys(version, relative):
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(version).key(relative)
    assert caught.value.code == "training_storage_invalid_key"


@pytest.mark.parametrize("key", ["other/key", "", "__aap_reference__/opaque"])
def test_resolve_ref_rejects_cross_owner_keys(version, key):
    with pytest.raises(TrainingDatasetError):
        service.resolve(version).contains(key)


def test_resolve_ref_rejects_prefix_itself_and_other_version(version):
    owner = service.resolve(version)
    for key in [
        owner.prefix,
        service.prefix_for(version.project_id, uuid.uuid4()) + "media/a",
    ]:
        with pytest.raises(TrainingDatasetError):
            owner.contains(key)
    version.storage_ref["prefix"] = service.prefix_for(version.project_id, uuid.uuid4())
    with pytest.raises(TrainingDatasetError):
        service.resolve(version)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("purpose", "training"),
        ("location_id", str(uuid.uuid4())),
        ("config_revision", True),
        ("config_revision", 2),
        ("binding_generation", True),
        ("binding_generation", 2),
        ("bucket", "bad/bucket"),
        ("bucket", "bad\\bucket"),
        ("bucket", "白名单"),
        ("prefix", "projects/../datasets/bad/"),
        ("storage_identity", "0" * 64),
        ("origin", "historical-created"),
        ("origin", []),
        ("evidence", {"made-up": True}),
    ],
)
def test_resolve_ref_rejects_unknown_or_malformed_identity(version, field, value):
    version.storage_ref[field] = value
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(version)
    assert caught.value.status_code == 409


def test_resolve_ref_rejects_missing_or_extra_fields(version):
    original = deepcopy(version.storage_ref)
    for field in original:
        version.storage_ref = {k: v for k, v in original.items() if k != field}
        with pytest.raises(TrainingDatasetError):
            service.resolve(version)
    version.storage_ref = {**original, "secret_key": "private-credential"}
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(version)
    assert "private" not in str(caught.value)
    version.storage_ref = None
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(version)
    assert caught.value.code == "training_storage_unpinned"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://other.invalid:9000",
        "http://user:private@other.invalid",
        "http://host/path",
        "http://host?query=private",
        "http://host#private",
    ],
)
def test_resolve_ref_rejects_endpoint_mismatch_without_network(version, endpoint):
    version.storage_ref["endpoint_url"] = endpoint
    version.storage_ref["storage_identity"] = managed_storage_identity(
        endpoint, version.storage_ref["bucket"]
    )
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(version)
    assert "private" not in str(caught.value)


def test_resolve_ref_checks_actual_client_origin_and_serializable_evidence(
    version, monkeypatch
):
    monkeypatch.setattr(
        storage_service,
        "client",
        SimpleNamespace(meta=SimpleNamespace(endpoint_url="http://unexpected.invalid")),
    )
    with pytest.raises(TrainingDatasetError):
        service.resolve(version)


def test_resolve_ref_keeps_verified_adoption_evidence(version):
    version.storage_ref.update(
        origin="verified-retained", evidence={"objects": 3, "sha256": "b" * 64}
    )
    assert service.resolve(version).ref["evidence"]["objects"] == 3
    version.storage_ref["evidence"] = {"bad": float("nan")}
    with pytest.raises(TrainingDatasetError):
        service.resolve(version)


async def database_version(db, owner):
    project = await create_project(db, owner_id=owner.id)
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        name="storage owner",
        snapshot={},
        snapshot_digest="b" * 64,
    )
    db.add(version)
    await db.flush()
    return version


async def test_database_allocate_persists_only_in_callers_transaction(
    db_session, super_admin, monkeypatch
):
    version = await database_version(db_session, super_admin[0])
    calls = []
    monkeypatch.setattr(db_session, "commit", lambda: calls.append("commit"))
    ref = await service.allocate(db_session, version)
    assert calls == [] and ref == version.storage_ref
    assert ref["origin"] == "new" and ref["evidence"] is None
    assert ref["bucket"] == storage_service.training_bucket
    original_bucket = ref["bucket"]
    monkeypatch.setattr(storage_service, "training_bucket", "new-destination")
    assert (await service.allocate(db_session, version))["bucket"] == original_bucket


async def test_database_adoption_candidate_is_read_only_and_uses_persisted_bucket(
    db_session, super_admin, monkeypatch
):
    await ensure_legacy_bindings(db_session)
    version = await database_version(db_session, super_admin[0])
    bucket = await db_session.scalar(
        select(StorageBinding.bucket).where(StorageBinding.purpose == "training")
    )
    monkeypatch.setattr(storage_service, "training_bucket", "wrong-current-bucket")
    candidate = await service.adoption_candidate(db_session, version)
    assert candidate["bucket"] == bucket and candidate["origin"] == "verified-retained"
    assert candidate["evidence"] is None and version.storage_ref is None


async def test_database_adoption_does_not_bootstrap_missing_binding(
    db_session, super_admin
):
    version = await database_version(db_session, super_admin[0])
    # The ordinary fixture transaction has not registered any purpose yet.
    original = await db_session.get(StorageBinding, "training")
    if original is not None:
        await db_session.delete(original)
        await db_session.flush()
    with pytest.raises(TrainingDatasetError) as caught:
        await service.adoption_candidate(db_session, version)
    assert caught.value.code == "training_storage_binding_unproven"
    assert await db_session.get(StorageBinding, "training") is None
    assert version.storage_ref is None


@pytest.mark.parametrize("change", ["disabled", "config", "generation", "location"])
async def test_database_candidate_rejects_unproven_platform_binding(
    db_session, super_admin, change
):
    await ensure_legacy_bindings(db_session)
    version = await database_version(db_session, super_admin[0])
    location = await db_session.get(StorageLocation, LEGACY_LOCATION_ID)
    binding = await db_session.get(StorageBinding, "training")
    if change == "disabled":
        location.enabled = False
    elif change == "config":
        location.config_revision = 2
    elif change == "generation":
        binding.generation = 2
    else:
        new_location = StorageLocation(
            id=uuid.uuid4(), name="other", kind="s3", config={}, enabled=True
        )
        db_session.add(new_location)
        await db_session.flush()
        binding.location_id = new_location.id
    await db_session.flush()
    with pytest.raises(TrainingDatasetError) as caught:
        await service.adoption_candidate(db_session, version)
    assert caught.value.code == "training_storage_binding_unproven"
    assert version.storage_ref is None
