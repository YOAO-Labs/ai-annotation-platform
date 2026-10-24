"""Exact native admission keeps logical members distinct and historical contracts honest."""

from copy import deepcopy
import uuid

import pytest

from training_contract.input_membership import (
    compact_members,
    contract_for,
    declared_contract,
    member_records,
    observed_members,
    receipt_for,
    verify_receipt,
)


def manifest():
    return {
        "samples": [
            {
                "task_id": str(uuid.UUID(int=index + 1)),
                "split": split,
                "image_path": f"images/{split}/{index}.png",
                "label_path": f"labels/{split}/{index}.txt",
                "sha256": "a" * 64,
                "label_sha256": "b" * 64,
                "width": 48,
                "height": 32,
                "size_bytes": 120,
            }
            for index, split in enumerate(("train", "train", "val"))
        ]
    }


def config():
    return {
        "dataset_id": str(uuid.UUID(int=20)),
        "dataset_sha256": "c" * 64,
        "input_contract": contract_for(manifest()),
    }


def test_identical_content_is_not_a_single_logical_member_and_order_is_irrelevant():
    record = manifest()
    original = deepcopy(record)
    records = member_records(record)
    paths = [path for path in records if records[path]["split"] == "train"]
    accepted = observed_members(records, "train", paths[::-1])
    assert len(accepted) == 2
    assert accepted[0]["member_digest"] != accepted[1]["member_digest"]
    record["samples"].reverse()
    assert contract_for(record) == contract_for(original)
    members = compact_members(records)
    run, attempt = uuid.uuid4(), uuid.uuid4()
    proof = receipt_for(config(), run, attempt, members)
    proof["members"]["train"].reverse()
    assert verify_receipt(proof, config(), run, attempt)["counts"] == {
        "train": 2,
        "val": 1,
    }
    assert original == manifest()


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "wrong_split"])
def test_native_members_must_match_every_frozen_member(change):
    records = member_records(manifest())
    paths = [path for path in records if records[path]["split"] == "train"]
    if change == "missing":
        paths.pop()
    elif change == "extra":
        paths.append("images/train/extra.png")
    elif change == "duplicate":
        paths[1] = paths[0]
    else:
        paths[1] = "images/val/2.png"
    with pytest.raises(ValueError, match="differ"):
        observed_members(records, "train", paths)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "unknown"),
        ("image_path", "images/train/../0.png"),
        ("label_path", "labels/train/other.txt"),
        ("sha256", "unknown"),
        ("width", True),
        ("split", "test"),
    ],
)
def test_explicit_bad_manifest_is_not_reinterpreted_as_a_legacy_contract(field, value):
    record = manifest()
    record["samples"][0][field] = value
    with pytest.raises(ValueError):
        contract_for(record)


def test_historical_absence_and_explicit_invalid_declaration_are_distinct():
    assert contract_for({"schema": 1}) is None
    assert declared_contract({}) is None
    for value in (None, {}, {**config()["input_contract"], "schema": True}):
        with pytest.raises(ValueError):
            declared_contract({"input_contract": value})
    for value in (None, [], [manifest()["samples"][0]]):
        with pytest.raises(ValueError):
            contract_for({"samples": value})
    for value in (None, [], "samples"):
        with pytest.raises(ValueError):
            contract_for(value)


@pytest.mark.parametrize(
    "change", ["attempt", "config", "member", "duplicate", "split", "bool_count"]
)
def test_receipt_is_bound_to_attempt_config_and_actual_collection(change):
    frozen = config()
    run, attempt = uuid.uuid4(), uuid.uuid4()
    proof = receipt_for(
        frozen, run, attempt, compact_members(member_records(manifest()))
    )
    if change == "attempt":
        proof["attempt_id"] = str(uuid.uuid4())
    elif change == "config":
        frozen["changed"] = True
    elif change == "member":
        proof["members"]["train"][0]["member_digest"] = "f" * 64
    elif change == "duplicate":
        proof["members"]["train"][1] = deepcopy(proof["members"]["train"][0])
    elif change == "bool_count":
        proof["counts"]["val"] = True
    else:
        proof["members"]["train"][0], proof["members"]["val"][0] = (
            proof["members"]["val"][0],
            proof["members"]["train"][0],
        )
    with pytest.raises(ValueError):
        verify_receipt(proof, frozen, run, attempt)


@pytest.mark.parametrize(
    "field", ["task_id", "sha256", "label_sha256", "width", "size_bytes"]
)
def test_member_content_and_identity_change_the_expected_collection(field):
    record = manifest()
    before = contract_for(record)
    record["samples"][0][field] = (
        str(uuid.uuid4())
        if field == "task_id"
        else "e" * 64
        if field.endswith("sha256")
        else 99
    )
    assert contract_for(record) != before
