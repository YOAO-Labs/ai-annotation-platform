"""Registered retirement evidence routes only the existing bounded read probe."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.services import managed_original_retirement as retirement
from app.services import managed_original_verification as verification
from app.services.managed_original_probe import ProbeResult
from app.workers import managed_original_verification as worker
from tests.test_dataset_original_reads import saved_db as saved_db, saved_item


async def retired_b(db):
    item, asset, replica, _task, location, connection = await saved_item(db)
    fields = {
        "dataset_id": item.dataset_id,
        "dataset_item_id": item.id,
        "dataset_lifecycle_revision": 1,
        "asset_id": asset.id,
        "creation_id": None,
        "location_id": location.id,
        "bucket": replica.bucket,
        "object_key": replica.object_key,
        "role": "managed_original",
        "storage_ref": replica.storage_ref,
        "location_config_revision": 99,
        "binding_generation": None,
        "asset_verification_state": "verified",
        "checksum_algorithm": "sha256",
        "checksum_value": asset.checksum_value,
        "size_bytes": asset.size_bytes,
        "reference_state": "present",
        "etag": replica.etag,
        "verified_at": replica.verified_at,
    }
    values = await retirement._saved_assertion(db, fields)
    anchor = ManagedOriginalRetirement(**values)
    db.add(anchor)
    await db.commit()
    return anchor, location, connection


async def test_saved_retirement_uses_recorded_address_without_current_binding(saved_db):
    anchor, _location, _connection = await retired_b(saved_db)
    assert anchor.identity_state == "verified_binding"
    assert anchor.location_config_revision == anchor.binding_generation == 2
    assert anchor.storage_identity == retirement.managed_storage_identity(
        "https://objects.example.test",
        anchor.bucket,
    )
    context = await verification.observe(saved_db, anchor.id)
    assert context["configuration"]["saved_ref"]["object_key"] == anchor.object_key
    blockers, revision = await verification.eligibility(saved_db, context)
    assert blockers == [] and revision == 1


async def test_retirement_fingerprint_ignores_credential_rotation_and_transport_is_current(
    saved_db,
):
    db = saved_db
    anchor, location, connection = await retired_b(db)
    before = await verification.observe(db, anchor.id)
    connection.secret_enc = b"encrypted-rotated"
    connection.revision += 1
    location.config_revision += 1
    await db.commit()
    after = await verification.observe(db, anchor.id)
    assert before["fingerprint"] == after["fingerprint"]
    row = SimpleNamespace(anchor=after["anchor"], configuration=after["configuration"])
    options = await verification.probe_options(db, row)
    target = options["client_context"].func.__self__
    assert target._access.secret_enc == b"encrypted-rotated"
    assert target.config_revision == 3
    assert after["configuration"]["saved_ref"]["config_revision"] == 2


@pytest.mark.parametrize("change", ["identity", "prefix", "disabled"])
async def test_retirement_candidate_must_prove_saved_hash_and_controlled_root(
    saved_db,
    change,
):
    db = saved_db
    anchor, location, connection = await retired_b(db)
    if change == "identity":
        anchor.storage_identity = "f" * 64
    elif change == "prefix":
        connection.config = {**connection.config, "base_prefix": "elsewhere"}
    else:
        location.enabled = False
    await db.commit()
    context = await verification.observe(db, anchor.id)
    blockers, _revision = await verification.eligibility(db, context)
    assert "platform_configuration_changed" in blockers
    assert context["configuration"]["saved_ref"] is None


async def test_existing_worker_routes_b_location_and_probe_sidecar(monkeypatch):
    location_id, job_id = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        maintenance_revision=4,
        anchor={
            "location_id": str(location_id),
            "bucket": "saved-bucket",
            "object_key": "saved/key",
            "storage_identity": "a" * 64,
        },
        configuration={"saved_ref": {"endpoint_url": "https://objects.example.test"}},
        expected_sha256="b" * 64,
        expected_size_bytes=7,
    )

    def context():
        return None

    monkeypatch.setattr(verification, "claim", AsyncMock(return_value=row))
    monkeypatch.setattr(
        verification,
        "probe_options",
        AsyncMock(
            return_value={"client_context": context},
        ),
    )
    result = ProbeResult("matched", None, row.expected_sha256, 7)
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(worker.storage_access, "run", run)
    monkeypatch.setattr(verification, "publish", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "notify_job_terminal", AsyncMock())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    assert await worker.execute(db, job_id, "saved-task")
    args, kwargs = run.call_args
    assert args[1] == location_id
    assert args[4].endpoint_url == "https://objects.example.test"
    assert args[4].bucket == "saved-bucket" and args[4].object_key == "saved/key"
    assert kwargs["client_context"] is context and kwargs["expected_revision"] == 4
