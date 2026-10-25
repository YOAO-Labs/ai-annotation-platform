"""A run's physical owner and attempt boundary stay fixed across retries."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.config import settings
from app.services import training_dataset_storage as datasets
from app.services import training_run_storage as service
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError


@pytest.fixture
def run():
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return SimpleNamespace(
        id=run_id,
        project_id=project_id,
        storage_ref={
            "schema": 1,
            "purpose": "training-run",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": "original-run-bucket",
            "prefix": service.prefix_for(project_id, run_id),
            "storage_identity": managed_storage_identity(
                endpoint, "original-run-bucket"
            ),
            "origin": "new",
            "evidence": None,
        },
    )


def binding_db(bucket):
    return SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                id=LEGACY_LOCATION_ID,
                kind="s3",
                config_revision=1,
                enabled=True,
                config={
                    "endpoint": settings.minio_endpoint,
                    "use_ssl": settings.minio_use_ssl,
                },
            )
        ),
        scalar=AsyncMock(
            return_value=SimpleNamespace(
                location_id=LEGACY_LOCATION_ID,
                generation=1,
                bucket=bucket,
            )
        ),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )


def test_saved_owner_and_nested_evidence_are_independent_of_current_bucket(
    run, monkeypatch
):
    run.storage_ref.update(
        origin="verified-retained", evidence={"attempts": [{"sha256": "a" * 64}]}
    )
    original = deepcopy(run.storage_ref)
    monkeypatch.setattr(storage_service, "training_bucket", "another-current-bucket")
    owner = service.resolve(run)
    copied = owner.ref
    copied["evidence"]["attempts"][0]["sha256"] = "rewritten"
    run.storage_ref["evidence"]["attempts"].clear()
    assert owner.ref == original
    assert owner.bucket == "original-run-bucket"
    attempt = uuid.uuid4()
    key = owner.key(f"attempts/{attempt}/artifacts/weights.pt")
    assert service.expect_attempt(owner, attempt, key) == key
    assert service.expect_attempt(owner, str(attempt), key) == key


def test_run_and_dataset_resolvers_cannot_accept_each_others_role(run):
    with pytest.raises(TrainingDatasetError):
        datasets.resolve(run)
    run.storage_ref["purpose"] = "training-dataset"
    run.storage_ref["prefix"] = datasets.prefix_for(run.project_id, run.id)
    with pytest.raises(TrainingDatasetError):
        service.resolve(run)
    assert datasets.resolve(run).prefix == run.storage_ref["prefix"]


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "run",
        "purpose",
        "namespace",
        "missing",
        "unknown",
        "identity",
        "endpoint",
        "evidence",
    ],
)
def test_rejects_foreign_or_unproven_run_locator(run, change):
    if change == "project":
        run.project_id = uuid.uuid4()
    elif change == "run":
        run.id = uuid.uuid4()
    elif change == "purpose":
        run.storage_ref["purpose"] = "training"
    elif change == "namespace":
        run.storage_ref["prefix"] = datasets.prefix_for(run.project_id, run.id)
    elif change == "missing":
        del run.storage_ref["storage_identity"]
    elif change == "unknown":
        run.storage_ref["credentials"] = "private"
    elif change == "identity":
        run.storage_ref["storage_identity"] = "0" * 64
    elif change == "endpoint":
        run.storage_ref["endpoint_url"] = "http://private@other.invalid"
    else:
        run.storage_ref.update(
            origin="verified-retained", evidence={"bad": float("nan")}
        )
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(run)
    assert caught.value.status_code == 409
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "relative",
    [
        "weights.pt",
        "attempts/{other}/weights.pt",
        "attempts/{attempt}suffix/weights.pt",
        "attempts/{attempt}/",
        "attempts/{attempt}/../weights.pt",
        "attempts/{attempt}//weights.pt",
        "attempts/{attempt}/weights\\pt",
        "attempts/{attempt}/weights\x00pt",
        "__aap_reference__/private",
    ],
)
def test_attempt_guard_rejects_siblings_unsafe_and_non_attempt_objects(run, relative):
    owner, attempt = service.resolve(run), uuid.uuid4()
    key = owner.prefix + relative.format(attempt=attempt, other=uuid.uuid4())
    with pytest.raises(TrainingDatasetError) as caught:
        service.expect_attempt(owner, attempt, key)
    assert caught.value.code == "training_storage_invalid_key"
    assert caught.value.status_code == 409


@pytest.mark.parametrize(
    "attempt_id",
    [
        None,
        1,
        [],
        "",
        "not-a-uuid",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_attempt_guard_rejects_noncanonical_attempt_ids(run, attempt_id):
    owner = service.resolve(run)
    key = owner.key("attempts/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/weights.pt")
    with pytest.raises(TrainingDatasetError):
        service.expect_attempt(owner, attempt_id, key)


def test_attempt_guard_rejects_forged_or_dataset_owner(run):
    ref = deepcopy(run.storage_ref)
    ref.update(
        purpose="training-dataset", prefix=datasets.prefix_for(run.project_id, run.id)
    )
    attempt = uuid.uuid4()
    owners = [
        None,
        datasets.resolve_ref(ref),
        service.Owner("foreign", run.storage_ref["prefix"], run.storage_ref),
    ]
    for owner in owners:
        with pytest.raises(TrainingDatasetError):
            service.expect_attempt(
                owner,
                attempt,
                run.storage_ref["prefix"] + f"attempts/{attempt}/weights.pt",
            )


async def test_explicit_allocate_flushes_once_never_commits_and_existing_pin_bypasses_binding(
    run, monkeypatch, db_session
):
    run.storage_ref = None
    db = db_session
    db.flush = AsyncMock(wraps=db.flush)
    db.commit = AsyncMock()
    bootstrap = AsyncMock(wraps=datasets.ensure_legacy_bindings)
    monkeypatch.setattr(datasets, "ensure_legacy_bindings", bootstrap)
    ref = await service.allocate(db, run)
    assert ref == run.storage_ref and ref["purpose"] == "training-run"
    assert ref["origin"] == "new" and ref["evidence"] is None
    bootstrap.assert_awaited_once_with(db)
    db.flush.assert_awaited_once_with()
    db.commit.assert_not_awaited()
    monkeypatch.setattr(storage_service, "training_bucket", "later-current-bucket")
    assert await service.allocate(SimpleNamespace(), run) == ref


async def test_unpinned_read_refuses_without_implicit_allocation(run):
    run.storage_ref = None
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(run)
    assert caught.value.code == "training_storage_unpinned"
    assert run.storage_ref is None


async def test_candidate_uses_deployment_bucket_without_binding_bootstrap_or_writes(
    run, monkeypatch
):
    run.storage_ref = None
    db = binding_db("persisted-training-bucket")
    bootstrap = AsyncMock(
        side_effect=AssertionError("read-only candidate bootstrapped")
    )
    monkeypatch.setattr(datasets, "ensure_legacy_bindings", bootstrap)
    monkeypatch.setattr(storage_service, "training_bucket", "different-current-bucket")
    ref = await service.adoption_candidate(db, run)
    assert (
        ref["bucket"] == "different-current-bucket" and ref["purpose"] == "training-run"
    )
    assert ref["origin"] == "verified-retained" and ref["evidence"] is None
    assert run.storage_ref is None
    bootstrap.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "missing-location",
        "disabled",
        "config",
        "revision",
        "endpoint",
    ],
)
async def test_candidate_refuses_unproven_platform_identity(run, monkeypatch, change):
    run.storage_ref = None
    db = binding_db("persisted-bucket")
    if change == "missing-location":
        db.get.return_value = None
    elif change == "missing-binding":
        db.scalar.return_value = None
    elif change == "disabled":
        db.get.return_value.enabled = False
    elif change == "config":
        db.get.return_value.config = {"endpoint": "another.invalid", "use_ssl": False}
    elif change == "revision":
        db.get.return_value.config_revision = 2
    elif change == "generation":
        db.scalar.return_value.generation = 2
    elif change == "location":
        db.scalar.return_value.location_id = uuid.uuid4()
    else:
        monkeypatch.setattr(
            storage_service,
            "client",
            SimpleNamespace(
                meta=SimpleNamespace(endpoint_url="http://another.invalid")
            ),
        )
    with pytest.raises(TrainingDatasetError) as caught:
        await service.adoption_candidate(db, run)
    assert caught.value.status_code == 409
    assert run.storage_ref is None
    db.flush.assert_not_awaited()


@pytest.mark.parametrize("change", ["bootstrap", "bucket"])
async def test_failed_allocation_does_not_write_a_locator(
    run, monkeypatch, change, db_session
):
    run.storage_ref = None
    db = db_session
    await datasets.ensure_legacy_bindings(db)
    db.flush = AsyncMock(wraps=db.flush)
    db.commit = AsyncMock()
    bootstrap = AsyncMock(
        side_effect=ValueError("private") if change == "bootstrap" else None,
        wraps=datasets.ensure_legacy_bindings,
    )
    monkeypatch.setattr(datasets, "ensure_legacy_bindings", bootstrap)
    monkeypatch.setattr(storage_service, "training_bucket", "another-bucket")
    with pytest.raises(TrainingDatasetError) as caught:
        await service.allocate(db, run)
    assert caught.value.code == "training_storage_binding_unproven"
    assert run.storage_ref is None
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
