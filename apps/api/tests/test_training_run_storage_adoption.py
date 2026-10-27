"""Legacy experiment adoption verifies durable receipts and every retained key."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from training_contract.observability import REDACTION_REVISION

from app.services import training_run_storage_adoption as adoption
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.test_training_storage_adoption import Store


@pytest.fixture
def retained():
    prefix = f"projects/{uuid.uuid4()}/runs/{uuid.uuid4()}/"

    def key(relative):
        assert all(part not in {"", ".", ".."} for part in relative.split("/"))
        return prefix + relative

    def contains(value):
        assert value.startswith(prefix) and ".." not in value

    owner = SimpleNamespace(
        bucket="saved-run", prefix=prefix, key=key, contains=contains
    )
    store = Store(owner.bucket)
    attempts = []
    for index in range(2):
        identifier = str(uuid.uuid4())
        inventory, published = {}, {}
        for name, role, data in [
            (
                "best.pt",
                "weights",
                b"Opaque weights, never deserialize: " + bytes([index]),
            ),
            ("checkpoint.pt", "checkpoint", b"checkpoint" + bytes([index])),
            ("metrics.json", "metrics", b'{"summary":{}}'),
            ("train.log", "log", b"first\nAuthorization: Bearer hidden-value\nlast\n"),
        ]:
            spec = {
                "name": name,
                "role": role,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "epoch": None,
            }
            path = key(f"attempts/{identifier}/{name}")
            inventory[name] = deepcopy(spec)
            published[name] = {**spec, "key": path}
            store.objects[path] = data
            store.metadata[path] = {"attempt-id": identifier, "sha256": spec["sha256"]}
        attempts.append(
            {
                "id": identifier,
                "status": "succeeded",
                "upload_inventory": inventory,
                "artifacts": published,
            }
        )
    state = {"status": "succeeded", "attempts": attempts}
    return store, owner, state


def add_derived(retained):
    store, owner, state = retained
    attempt = state["attempts"][0]
    source = attempt["artifacts"]["train.log"]
    raw = adoption._redact(store.objects[source["key"]])
    sha = hashlib.sha256(raw).hexdigest()
    key = owner.key(f"attempts/{attempt['id']}/redacted-logs/train.log.{sha}.log")
    store.objects[key] = raw
    store.metadata[key] = {
        "sha256": sha,
        "source-sha256": source["sha256"],
        "attempt-id": attempt["id"],
        "redaction-revision": str(REDACTION_REVISION),
    }
    return key


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_all_stopped_attempts_use_independent_receipts_without_writes(retained, status):
    store, owner, state = retained
    state["status"] = status
    before = deepcopy(store.objects)
    evidence = adoption.verify(store, owner, state)
    assert (
        evidence["scope"] == "current-retained-copy" and evidence["attempt_count"] == 2
    )
    assert evidence["object_count"] == len(before) and set(store.reads) == set(before)
    assert store.objects == before


def test_current_derived_log_requires_reproduction_from_verified_source(retained):
    store, owner, state = retained
    key = add_derived(retained)
    evidence = adoption.verify(store, owner, state)
    assert evidence["object_count"] == 9 and key in store.reads


@pytest.mark.parametrize("change", ["bytes", "source", "attempt", "revision"])
def test_rehashed_or_unexplained_derived_log_cannot_be_adopted(retained, change):
    store, owner, state = retained
    key = add_derived(retained)
    if change == "bytes":
        raw = b"forged\n"
        sha = hashlib.sha256(raw).hexdigest()
        new_key = key.rsplit(".", 2)[0] + "." + sha + ".log"
        store.objects[new_key] = raw
        del store.objects[key]
        store.metadata[new_key] = {**store.metadata.pop(key), "sha256": sha}
    else:
        field = {
            "source": "source-sha256",
            "attempt": "attempt-id",
            "revision": "redaction-revision",
        }[change]
        store.metadata[key][field] = "unverified"
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "bytes",
        "metadata",
        "unknown",
        "inventory",
        "key",
        "active-run",
        "active-attempt",
    ],
)
def test_unavailable_or_changed_receipts_fail_closed(retained, change):
    store, owner, state = retained
    spec = state["attempts"][0]["artifacts"]["best.pt"]
    if change == "missing":
        store.objects.pop(spec["key"])
    elif change == "bytes":
        store.objects[spec["key"]] += b"different"
    elif change == "metadata":
        store.metadata[spec["key"]]["attempt-id"] = "other"
    elif change == "unknown":
        store.objects[owner.key("orphan.bin")] = b"unclaimed"
    elif change == "inventory":
        state["attempts"][0]["upload_inventory"]["best.pt"]["sha256"] = "0" * 64
    elif change == "key":
        spec["key"] = owner.key("attempts/another/best.pt")
    elif change == "active-run":
        state["status"] = "lost"
    else:
        state["attempts"][0]["status"] = "uploading"
    before = deepcopy(store.objects)
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)
    assert store.objects == before


@pytest.mark.parametrize("present", [False, True])
def test_partial_uploads_in_failed_attempt_are_optional_but_verified_when_present(
    retained, present
):
    store, owner, state = retained
    attempt = state["attempts"][0]
    attempt["status"] = state["status"] = "failed"
    spec = attempt["artifacts"].pop("best.pt")
    if not present:
        store.objects.pop(spec["key"])
    assert adoption.verify(store, owner, state)["object_count"] == len(store.objects)
    if present:
        store.objects[spec["key"]] = b"incomplete"
        with pytest.raises(TrainingDatasetError):
            adoption.verify(store, owner, state)


def test_successful_attempt_cannot_hide_unpublished_missing_upload(retained):
    store, owner, state = retained
    spec = state["attempts"][0]["artifacts"].pop("best.pt")
    store.objects.pop(spec["key"])
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)


def test_empty_stopped_history_can_pin_without_inventing_objects(retained):
    store, owner, state = retained
    state = {"status": "cancelled", "attempts": []}
    store.objects.clear()
    assert adoption.verify(store, owner, state)["object_count"] == 0 and not store.reads


def test_read_budget_and_unknown_close_never_produce_adoption_evidence(retained):
    store, owner, state = retained
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state, max_bytes=1)
    store.close_failure = True
    with pytest.raises(TrainingDatasetError) as error:
        adoption.verify(store, owner, state)
    assert error.value.storage_operation_unsettled is True


def test_overwritten_object_after_read_is_not_a_stable_retained_copy(retained):
    store, owner, state = retained

    def changed(key):
        store.objects[key] += b"changed after body close"

    store.after_read = changed
    with pytest.raises(TrainingDatasetError):
        adoption.verify(store, owner, state)
