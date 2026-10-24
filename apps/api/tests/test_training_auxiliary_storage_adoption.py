"""Retained auxiliary copies require independent receipts and stable full reads."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from unittest.mock import Mock
import uuid

from PIL import Image
import pytest

from app.services import (
    training_auxiliary_storage_adoption as adoption,
    training_preview_storage,
    sample_evaluation_storage,
    active_learning_storage,
    sample_evaluations,
    active_learning,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.test_training_storage_adoption import Store


@pytest.fixture(params=sorted(adoption.PURPOSES))
def retained(request, monkeypatch):
    purpose = request.param
    project, identifier, attempt_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owners = {
        "training-preview": training_preview_storage,
        "sample-evaluation": sample_evaluation_storage,
        "active-learning": active_learning_storage,
    }[purpose]
    endpoint = storage_service.client.meta.endpoint_url.rstrip("/")
    owner = owners.resolve_ref(
        {
            "schema": 1,
            "purpose": purpose,
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": "saved-auxiliary",
            "prefix": owners.prefix_for(project, identifier),
            "storage_identity": managed_storage_identity(endpoint, "saved-auxiliary"),
            "origin": "verified-retained",
            "evidence": None,
        }
    )
    store = Store(owner.bucket)
    image = io.BytesIO()
    Image.new("RGB", (16, 12), "orange").save(image, format="PNG")
    png = image.getvalue()
    sha = hashlib.sha256(png).hexdigest()
    state = {
        "purpose": purpose,
        "record": {
            "id": str(identifier),
            "project_id": str(project),
            "status": "cancelled",
            "spec_digest": "a" * 64,
            "generation": 1,
            "current_attempt_id": str(attempt_id),
            "put_expires_at": None,
        },
        "attempts": [
            {
                "id": str(attempt_id),
                "status": "cancelled",
                "generation": 1,
                "claim_id": str(uuid.uuid4()),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "report": None,
            }
        ],
        "members": [],
    }
    if purpose == "training-preview":
        key = owner.key("preview-0.png")
        receipt = {"name": "preview-0.png", "sha256": sha, "size_bytes": len(png)}
        state["record"].update(
            input_digest="b" * 64,
            spec={"count": 1},
            upload_inventory={"preview-0.png": receipt},
            artifacts={"preview-0.png": {**receipt, "key": key}},
        )
        state["attempts"] = []
        metadata = {
            "preview-id": str(identifier),
            "input-digest": "b" * 64,
            "sha256": sha,
        }
        data = png
    elif purpose == "active-learning":
        key = owner.key("media/0.png")
        sample = {
            "ordinal": 0,
            "width": 16,
            "height": 12,
            "sha256": "c" * 64,
            "derived_sha256": sha,
        }
        state["record"]["spec"] = {"samples": [sample]}
        state["members"] = [
            {
                "ordinal": 0,
                "status": "pending",
                "request": None,
                "evidence": None,
                "media": {"key": key, "sha256": sha},
            }
        ]
        monkeypatch.setattr(
            active_learning, "validate_spec", lambda record: record.spec
        )
        metadata = {
            "round-id": str(identifier),
            "ordinal": "0",
            "source-sha256": "c" * 64,
            "sha256": sha,
        }
        data = png
    else:
        spec = {
            "schema": 1,
            "benchmark_digest": "b" * 64,
            "comparison_digest": "c" * 64,
            "model_ref": {"sha256": "d" * 64},
            "evaluator": {},
            "samples": [{}],
        }
        state["record"]["spec"] = spec
        report = {
            "schema": 1,
            "evaluation_id": str(identifier),
            "attempt_id": str(attempt_id),
            "spec_digest": "a" * 64,
            "benchmark_digest": spec["benchmark_digest"],
            "comparison_digest": spec["comparison_digest"],
            "model_ref": spec["model_ref"],
            "evaluator": {},
            "status": "cancelled",
            "complete": False,
            "total_samples": 1,
            "succeeded_samples": 0,
            "failed_samples": 0,
            "pending_samples": 1,
            "aggregate": None,
            "samples": [
                {
                    "ordinal": 0,
                    "task_id": str(uuid.uuid4()),
                    "sha256": "e" * 64,
                    "label_sha256": "f" * 64,
                    "width": 16,
                    "height": 12,
                    "status": "pending",
                    "truth": [],
                    "request_id": str(uuid.uuid4()),
                }
            ],
        }
        data = json.dumps(report).encode()
        sha = hashlib.sha256(data).hexdigest()
        key = owner.key(f"{attempt_id}/{sha}.json")
        state["attempts"][0]["report"] = {
            "key": key,
            "sha256": sha,
            "size_bytes": len(data),
        }
        monkeypatch.setattr(
            sample_evaluations, "validate_spec", lambda record: record.spec
        )
        monkeypatch.setattr(
            sample_evaluations, "verify_report", Mock(return_value=report)
        )
        metadata = sample_evaluations.report_metadata(
            adoption._record(state["record"]), adoption._record(state["attempts"][0])
        )
    store.objects[key], store.metadata[key] = data, metadata
    return store, owner, state


def test_all_retained_bytes_are_read_from_saved_bucket_without_writes(
    retained, monkeypatch
):
    store, owner, state = retained
    before = deepcopy(store.objects)
    monkeypatch.setattr(storage_service, "training_bucket", "different-current")
    evidence = adoption.verify(store, owner, state)
    assert evidence["purpose"] == state["purpose"]
    assert evidence["scope"] == "current-retained-copy"
    assert evidence["object_count"] == 1
    assert evidence["read_bytes"] == sum(map(len, before.values()))
    assert store.objects == before and set(store.reads) == set(before)


@pytest.mark.parametrize(
    "change", ["missing", "bytes", "metadata", "unknown", "active"]
)
def test_missing_unexplained_or_changed_copy_never_produces_pin_evidence(
    retained, change
):
    store, owner, state = retained
    key = next(iter(store.objects))
    if change == "missing":
        store.objects.clear()
    elif change == "bytes":
        store.objects[key] += b"changed"
    elif change == "metadata":
        store.metadata[key] = {}
    elif change == "unknown":
        store.objects[owner.key("unclaimed.bin")] = b"unexplained"
    else:
        state["record"]["status"] = "lost"
    before = deepcopy(store.objects)
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)
    assert store.objects == before


def test_budget_change_after_read_and_uncertain_close_all_refuse(retained):
    store, owner, state = retained
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state, max_bytes=1)
    store.close_failure = True
    with pytest.raises(TrainingDatasetError) as caught:
        adoption.verify(store, owner, state)
    assert caught.value.storage_operation_unsettled
    store.close_failure = False
    store.after_read = lambda key: store.objects.__setitem__(
        key, store.objects[key] + b"changed after close"
    )
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)


def test_preview_put_window_blocks_before_inventory(retained):
    store, owner, state = retained
    if state["purpose"] != "training-preview":
        pytest.skip("Preview PUT window")
    state["record"]["put_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat()
    with pytest.raises(TrainingDatasetError) as caught:
        adoption.verify(store, owner, state)
    assert caught.value.code == "legacy_storage_version_active" and not store.reads


@pytest.mark.parametrize(
    "change", [None, "current", "generation", "claim", "completion"]
)
def test_only_exact_retired_lost_attempt_is_compatible(retained, change):
    store, owner, state = retained
    if state["purpose"] == "training-preview":
        pytest.skip("Inference attempt fence")
    state["record"]["generation"] = state["attempts"][0]["generation"] = 2
    lost = {
        "id": str(uuid.uuid4()),
        "status": "lost",
        "generation": 1,
        "claim_id": None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "report": None,
    }
    state["attempts"].append(lost)
    if change == "current":
        state["record"]["current_attempt_id"] = lost["id"]
    elif change == "generation":
        lost["generation"] = 2
    elif change == "claim":
        lost["claim_id"] = str(uuid.uuid4())
    elif change == "completion":
        lost["completed_at"] = None
    if change is None:
        assert adoption.verify(store, owner, state)["object_count"] == 1
    else:
        with pytest.raises(TrainingDatasetError):
            adoption.verify(store, owner, state)


def test_learning_unpublished_frozen_png_is_accepted_but_no_source_repair_occurs(
    retained, monkeypatch
):
    store, owner, state = retained
    if state["purpose"] != "active-learning":
        pytest.skip("Learning retained PNG")
    monkeypatch.setattr(
        active_learning.pool,
        "read_input",
        Mock(side_effect=AssertionError("No sources during adoption")),
    )
    state["members"][0]["media"] = None
    assert adoption.verify(store, owner, state)["object_count"] == 1
    store.objects.clear()
    assert adoption.verify(store, owner, state)["object_count"] == 0
    state["members"][0]["request"] = {"persisted": True}
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)


def test_preview_cannot_adopt_receipted_png_outside_its_frozen_count(retained):
    store, owner, state = retained
    if state["purpose"] != "training-preview":
        pytest.skip("Preview count namespace")
    old_key = owner.key("preview-0.png")
    key = owner.key("preview-3.png")
    receipt = {
        **state["record"]["upload_inventory"]["preview-0.png"],
        "name": "preview-3.png",
    }
    state["record"]["upload_inventory"]["preview-3.png"] = receipt
    store.objects[key], store.metadata[key] = (
        store.objects[old_key],
        store.metadata[old_key],
    )
    with pytest.raises(TrainingDatasetError) as caught:
        adoption.verify(store, owner, state)
    assert caught.value.code == "legacy_storage_unknown_object"
