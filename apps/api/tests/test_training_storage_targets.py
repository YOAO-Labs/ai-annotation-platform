"""Saved training addresses remain physical while logical ownership stays exact."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from app.services import training_dataset_storage as storage, training_storage_io as io
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_s3_target import Target
from app.services.training_dataset_bridge import TrainingDatasetError


def target(*, legacy=False, root="公司/ 空间"):
    return Target(
        LEGACY_LOCATION_ID if legacy else uuid.uuid4(),
        1 if legacy else 3,
        None if legacy else 7,
        storage._runtime_endpoint() if legacy else "https://objects.example.test",
        "saved-training",
        "" if legacy else storage.managed_prefix(root),
        purpose="training",
    )


@pytest.mark.parametrize("purpose", storage._NAMESPACES)
@pytest.mark.parametrize("legacy,generation", [(False, 2), (True, 1), (True, 3)])
def test_family_schema_two_keeps_physical_expected_prefix_and_logical_owner(
    purpose, legacy, generation
):
    record = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    logical = storage._prefix_for(record.project_id, record.id, purpose=purpose)
    destination = target(legacy=legacy)
    record.storage_ref = storage.make_ref(
        destination, generation, logical, purpose=purpose
    )
    owner = storage._resolve(record, purpose=purpose)
    assert owner.logical_prefix == logical
    assert owner.prefix == destination.key_prefix + logical
    assert owner.location_id == destination.location_id
    assert len(owner.ref) == 12 and owner.ref["evidence"] is None
    assert (
        storage._resolve_ref(
            owner.ref, purpose=purpose, expected_prefix=owner.prefix
        ).ref
        == owner.ref
    )
    if not legacy:
        with pytest.raises(TrainingDatasetError):
            storage._resolve_ref(owner.ref, purpose=purpose, expected_prefix=logical)
    record.id = uuid.uuid4()
    with pytest.raises(TrainingDatasetError):
        storage._resolve(record, purpose=purpose)


def test_root_preserves_existing_five_hundred_character_and_unicode_byte_limits():
    base_length = 266 - len(storage.MANAGED_NAMESPACE) - 1
    base = "😀" * 147 + "a" * (base_length - 147)
    root = storage.managed_prefix(base)
    assert len(root) == 266 and len(root.encode()) == 707
    destination = target(root=base)
    logical = storage._prefix_for(uuid.uuid4(), uuid.uuid4(), purpose="training-run")
    owner = storage._resolve_ref(
        storage.make_ref(destination, 2, logical, purpose="training-run"),
        purpose="training-run",
    )
    attempt = str(uuid.uuid4())
    assert len(owner.key(f"attempts/{attempt}/" + "w" * 100)) == 500
    assert (
        len(
            owner.key(
                f"attempts/{attempt}/redacted-logs/"
                + "l" * 100
                + "."
                + "a" * 64
                + ".log"
            ).encode()
        )
        == 1024
    )
    for invalid in (
        base + "a",
        base.replace("a", "界", 1),
        "../bad",
        "a//b",
        "a\\b",
        "\ud800",
    ):
        with pytest.raises(TrainingDatasetError):
            storage.managed_prefix(invalid)


@pytest.mark.parametrize(
    "change",
    [
        {"secret": "forbidden"},
        {"schema": True},
        {"binding_generation": True},
        {"binding_generation": 1},
        {"key_prefix": "outside/"},
        {"prefix": "outside/"},
        {"location_id": "NOT-UUID"},
        {"origin": "verified-retained"},
        {"evidence": {}},
    ],
)
def test_new_ref_is_closed_and_cannot_redirect(change):
    ref = storage.make_ref(target(), 2, storage.prefix_for(uuid.uuid4(), uuid.uuid4()))
    with pytest.raises(TrainingDatasetError):
        storage.resolve_ref(ref | change)


async def test_target_uses_current_credentials_and_rejects_changed_physical_identity(
    monkeypatch,
):
    destination = target()
    ref = storage.make_ref(
        destination, 2, storage.prefix_for(uuid.uuid4(), uuid.uuid4())
    )
    current = replace(destination, config_revision=8, connection_revision=9)

    class DB:
        async def get(self, *args, **kwargs):
            return SimpleNamespace(id=destination.location_id)

    async def current_target(*args, **kwargs):
        return current

    monkeypatch.setattr(storage, "target_for", current_target)
    assert (await storage.target_for_ref(DB(), ref)).connection_revision == 9
    current = replace(current, key_prefix=storage.managed_prefix("changed"))
    with pytest.raises(TrainingDatasetError):
        await storage.target_for_ref(DB(), ref)


def test_external_signing_preserves_b_origin_without_mutating_global_a(monkeypatch):
    original = io.storage_service.client
    calls = []
    client = SimpleNamespace(
        generate_presigned_url=lambda operation, **kwargs: (
            calls.append(kwargs) or "https://objects.example.test/signed"
        )
    )

    @contextmanager
    def admitted_client():
        yield client

    destination = SimpleNamespace(
        location_id=uuid.uuid4(), bucket="target-b", client=admitted_client
    )
    monkeypatch.setattr(
        io.storage_service,
        "generate_download_url",
        lambda *args, **kwargs: pytest.fail("global helper was used"),
    )
    with io.service(destination) as bound:
        assert (
            bound.generate_download_url("owned/file", audience="browser", align=False)
            == "https://objects.example.test/signed"
        )
        assert bound.client is client and bound.training_bucket == "target-b"
    assert io.storage_service.client is original
    assert calls[0]["Params"] == {"Bucket": "target-b", "Key": "owned/file"}


async def test_run_passes_explicit_target_and_transaction_hook_without_committing(
    monkeypatch,
):
    destination = target()
    owner = storage.resolve_ref(
        storage.make_ref(destination, 2, storage.prefix_for(uuid.uuid4(), uuid.uuid4()))
    )
    callback, acquired = object(), object()
    seen = []

    async def registered(*args, **kwargs):
        seen.append((args, kwargs))
        return "done"

    monkeypatch.setattr(io.storage_access, "run", registered)
    assert (
        await io.run(
            object(),
            owner,
            "training_read",
            callback,
            3,
            target=destination,
            on_acquired=acquired,
        )
        == "done"
    )
    assert seen[0][0][1:6] == (
        owner.location_id,
        "training_read",
        io.physical,
        destination,
        callback,
    )
    assert seen[0][1]["on_acquired"] is acquired
