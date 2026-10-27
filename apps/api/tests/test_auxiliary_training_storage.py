"""Auxiliary output owners never borrow another kind, namespace or current bucket."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.config import settings
from app.services import active_learning_storage as active
from app.services import sample_evaluation_storage as evaluations
from app.services import training_dataset_storage as datasets
from app.services import training_preview_storage as previews
from app.services import training_run_storage as runs
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError

KINDS = [
    (previews, "training-preview", "previews"),
    (evaluations, "sample-evaluation", "sample-evaluations"),
    (active, "active-learning", "active-learning"),
]


@pytest.fixture(params=KINDS, ids=[kind[1] for kind in KINDS])
def case(request):
    return make_case(*request.param)


def make_case(service, purpose, namespace):
    project_id, identifier = uuid.uuid4(), uuid.uuid4()
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    ref = {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "auxiliary-original-bucket",
        "prefix": f"projects/{project_id}/{namespace}/{identifier}/",
        "storage_identity": managed_storage_identity(
            endpoint, "auxiliary-original-bucket"
        ),
        "origin": "verified-retained",
        "evidence": {"objects": [{"sha256": "a" * 64}]},
    }
    return service, SimpleNamespace(
        id=identifier, project_id=project_id, storage_ref=ref
    )


@pytest.fixture
def preview_case():
    return make_case(*KINDS[0])


@pytest.fixture
def evaluation_case():
    return make_case(*KINDS[1])


@pytest.fixture
def active_case():
    return make_case(*KINDS[2])


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
                location_id=LEGACY_LOCATION_ID, generation=1, bucket=bucket
            )
        ),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )


def artifact(service, owner, *, prefix=None):
    prefix = owner.prefix if prefix is None else prefix
    if service is previews:
        key = prefix + "preview-0.png"
        return service.expect_artifact(owner, "preview-0.png", key)
    if service is evaluations:
        key = prefix + "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/" + "a" * 64 + ".json"
        return service.expect_report(
            owner, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", key, "a" * 64
        )
    return service.expect_media(owner, 0, prefix + "media/0.png")


def test_saved_owner_ignores_current_bucket_and_returns_isolated_evidence(
    case, monkeypatch
):
    service, row = case
    original = deepcopy(row.storage_ref)
    monkeypatch.setattr(storage_service, "training_bucket", "new-current-bucket")
    owner = service.resolve(row)
    copied = owner.ref
    copied["evidence"]["objects"][0]["sha256"] = "changed"
    row.storage_ref["evidence"]["objects"].clear()
    assert owner.ref == original
    assert owner.bucket == original["bucket"]
    assert artifact(service, owner).startswith(owner.prefix)


def test_all_five_owner_kinds_are_mutually_exclusive(case):
    service, row = case
    for other in [previews, evaluations, active, datasets, runs]:
        if other is not service:
            with pytest.raises(TrainingDatasetError):
                other.resolve(row)


@pytest.mark.parametrize(
    "change",
    [
        "project",
        "id",
        "prefix",
        "purpose",
        "extra",
        "identity",
        "endpoint",
        "schema-bool",
        "nan",
    ],
)
def test_rejects_foreign_ambiguous_and_non_json_locators(case, change):
    service, row = case
    if change == "project":
        row.project_id = uuid.uuid4()
    elif change == "id":
        row.id = uuid.uuid4()
    elif change == "prefix":
        row.storage_ref["prefix"] = row.storage_ref["prefix"].upper()
    elif change == "purpose":
        row.storage_ref["purpose"] = "training-run"
    elif change == "extra":
        row.storage_ref["credential"] = "secret"
    elif change == "identity":
        row.storage_ref["storage_identity"] = "0" * 64
    elif change == "endpoint":
        row.storage_ref["endpoint_url"] = "http://secret@different.invalid"
    elif change == "schema-bool":
        row.storage_ref["schema"] = True
    else:
        row.storage_ref["evidence"] = {"invalid": float("nan")}
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(row)
    assert caught.value.status_code == 409
    assert "secret" not in str(caught.value)


def test_unpinned_resolve_does_not_discover_or_allocate(case):
    service, row = case
    row.storage_ref = None
    with pytest.raises(TrainingDatasetError) as caught:
        service.resolve(row)
    assert caught.value.code == "training_storage_unpinned"
    assert row.storage_ref is None


async def test_creation_allocation_flushes_once_and_saved_pin_skips_current_binding(
    case, monkeypatch, db_session
):
    service, row = case
    row.storage_ref = None
    db = db_session
    db.flush = AsyncMock(wraps=db.flush)
    db.commit = AsyncMock()
    bootstrap = AsyncMock(wraps=datasets.ensure_legacy_bindings)
    monkeypatch.setattr(datasets, "ensure_legacy_bindings", bootstrap)
    ref = await service.allocate(db, row)
    assert ref["origin"] == "new" and ref["evidence"] is None
    assert ref["prefix"] == service.prefix_for(row.project_id, row.id)
    bootstrap.assert_awaited_once_with(db)
    db.flush.assert_awaited_once_with()
    db.commit.assert_not_awaited()
    monkeypatch.setattr(storage_service, "training_bucket", "later-current-bucket")
    assert await service.allocate(SimpleNamespace(), row) == ref


async def test_candidate_uses_deployment_bucket_without_binding_bootstrap_or_writes(
    case, monkeypatch
):
    service, row = case
    row.storage_ref = None
    db = binding_db("retained-candidate-bucket")
    bootstrap = AsyncMock(side_effect=AssertionError("implicit bootstrap"))
    monkeypatch.setattr(datasets, "ensure_legacy_bindings", bootstrap)
    monkeypatch.setattr(storage_service, "training_bucket", "different-current-bucket")
    ref = await service.adoption_candidate(db, row)
    assert ref["bucket"] == "different-current-bucket"
    assert ref["origin"] == "verified-retained" and ref["evidence"] is None
    assert row.storage_ref is None
    bootstrap.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("change", ["disabled", "revision", "endpoint"])
async def test_unproven_binding_has_no_allocation_or_io(case, monkeypatch, change):
    service, row = case
    row.storage_ref = None
    db = binding_db("retained-candidate-bucket")
    if change == "missing":
        db.scalar.return_value = None
    elif change == "disabled":
        db.get.return_value.enabled = False
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
            SimpleNamespace(meta=SimpleNamespace(endpoint_url="http://other.invalid")),
        )
    with pytest.raises(TrainingDatasetError):
        await service.adoption_candidate(db, row)
    assert row.storage_ref is None
    db.flush.assert_not_awaited()


def test_artifact_helpers_revalidate_forged_owner_and_purpose(case):
    service, row = case
    real = service.resolve(row)
    for owner in [None, service.Owner("foreign-bucket", real.prefix, real.ref)]:
        with pytest.raises(TrainingDatasetError):
            artifact(service, owner, prefix=real.prefix)
    for other in [previews, evaluations, active]:
        if other is not service:
            with pytest.raises(TrainingDatasetError):
                artifact(other, real)


@pytest.mark.parametrize("name", ["report.json", "preview-0.png", "preview-3.png"])
def test_preview_accepts_only_canonical_output_names(preview_case, name):
    service, row = preview_case
    owner = service.resolve(row)
    assert service.expect_artifact(owner, name, owner.key(name)) == owner.key(name)


@pytest.mark.parametrize(
    "name",
    [
        None,
        [],
        "preview-4.png",
        "preview-00.png",
        "preview--1.png",
        "report.JSON",
        "../report.json",
        "report.json/child",
    ],
)
def test_preview_rejects_noncanonical_or_oversized_output_index(preview_case, name):
    service, row = preview_case
    owner = service.resolve(row)
    with pytest.raises(TrainingDatasetError):
        service.expect_artifact(owner, name, owner.prefix + str(name))


@pytest.mark.parametrize("ordinal", [True, False, -1, 32, 1.0, "0", None])
def test_active_media_ordinal_is_bounded_integer(active_case, ordinal):
    service, row = active_case
    owner = service.resolve(row)
    with pytest.raises(TrainingDatasetError):
        service.expect_media(owner, ordinal, owner.prefix + f"media/{ordinal}.png")


@pytest.mark.parametrize(
    "attempt",
    [
        None,
        True,
        0,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}",
    ],
)
def test_evaluation_rejects_noncanonical_attempt_identifiers(evaluation_case, attempt):
    service, row = evaluation_case
    owner = service.resolve(row)
    with pytest.raises(TrainingDatasetError):
        service.expect_report(
            owner, attempt, owner.prefix + f"{attempt}/" + "a" * 64 + ".json", "a" * 64
        )


@pytest.mark.parametrize("digest", [None, True, "a" * 63, "A" * 64, "a" * 65, "g" * 64])
def test_evaluation_rejects_noncanonical_digest(evaluation_case, digest):
    service, row = evaluation_case
    owner = service.resolve(row)
    attempt = uuid.uuid4()
    with pytest.raises(TrainingDatasetError):
        service.expect_report(
            owner, attempt, owner.prefix + f"{attempt}/{digest}.json", digest
        )
