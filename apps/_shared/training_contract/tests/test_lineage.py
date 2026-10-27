"""Safe immutable provenance, honest historical gaps and explicit contracts."""

from copy import deepcopy
import uuid

import pytest

from training_contract.dataset import DatasetContractError
from training_contract.lineage import (
    SOURCE_LINEAGE_CONTRACT,
    source_lineage_evidence,
    validate_source_lineage,
)


def lineage():
    return {
        "source_id": str(uuid.UUID(int=1)),
        "location_id": str(uuid.UUID(int=2)),
        "path_digest": "a" * 64,
        "scan_run_id": str(uuid.UUID(int=3)),
        "accepted_revision_id": str(uuid.UUID(int=4)),
        "revision_number": 2,
        "previous_revision_id": str(uuid.UUID(int=5)),
        "accepted_at": "2026-10-24T02:00:00+00:00",
        "scan_completed_at": "2026-10-24T01:00:00Z",
        "content_verified_at": "2026-10-24T01:30:00+00:00",
        "source_config_revision": 7,
        "location_config_revision": 3,
    }


def record(**updates):
    return {
        "source_lineage_contract": dict(SOURCE_LINEAGE_CONTRACT),
        "samples": [{"asset_reference": {"schema": 1}, "source_lineage": lineage()}],
        **updates,
    }


def test_recorded_evidence_is_detached_and_old_records_remain_unknown():
    value = record()
    original = deepcopy(value)
    validate_source_lineage(value)
    result = source_lineage_evidence(value, value["samples"][0])
    assert result == {"status": "recorded", "reasons": [], "evidence": lineage()}
    result["evidence"]["source_config_revision"] = 99
    assert value == original
    historical = {"samples": [{"asset_reference": {"schema": 1}}]}
    validate_source_lineage(historical)
    assert source_lineage_evidence(historical, historical["samples"][0]) == {
        "status": "unrecorded",
        "reasons": ["historical_contract_missing"],
        "evidence": None,
    }
    assert "source_lineage_contract" not in historical


def test_mixed_managed_members_do_not_acquire_invented_reference_history():
    value = record()
    value["samples"].append({"task_id": "managed"})
    validate_source_lineage(value)
    assert source_lineage_evidence(value, value["samples"][1]) == {
        "status": "unrecorded",
        "reasons": ["not_reference"],
        "evidence": None,
    }


def test_partial_persistent_facts_are_preserved_without_inventing_time():
    value = record()
    evidence = value["samples"][0]["source_lineage"]
    for key in (
        "accepted_revision_id",
        "revision_number",
        "accepted_at",
        "scan_completed_at",
        "content_verified_at",
    ):
        evidence[key] = None
    validate_source_lineage(value)
    assert source_lineage_evidence(value, value["samples"][0]) == {
        "status": "partial",
        "reasons": [
            "accepted_revision_unknown",
            "scan_time_unknown",
            "verification_time_unknown",
        ],
        "evidence": evidence,
    }


@pytest.mark.parametrize(
    "contract",
    [
        None,
        {},
        {"schema": True, "scope": "accepted-reference"},
        {"schema": 2, "scope": "accepted-reference"},
        {**SOURCE_LINEAGE_CONTRACT, "path": "/host/private"},
    ],
)
def test_unknown_contract_cannot_be_silently_ignored(contract):
    value = record(source_lineage_contract=contract)
    with pytest.raises(DatasetContractError) as error:
        validate_source_lineage(value)
    assert error.value.code == "unknown_source_lineage_contract"


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_id", 1),
        ("location_id", "invalid"),
        ("scan_run_id", "{" + str(uuid.UUID(int=3)) + "}"),
        ("accepted_revision_id", "bad"),
        ("previous_revision_id", "bad"),
        ("path_digest", "A" * 64),
        ("path_digest", "/private/secret.png"),
        ("source_config_revision", True),
        ("source_config_revision", 0),
        ("location_config_revision", "1"),
        ("revision_number", False),
        ("revision_number", -1),
        ("accepted_at", "2026-10-24T00:00:00"),
        ("content_verified_at", "2026-99-24T00:00:00Z"),
        ("scan_completed_at", 1792790000),
    ],
)
def test_invalid_or_coerced_evidence_is_rejected(key, value):
    frozen = record()
    frozen["samples"][0]["source_lineage"][key] = value
    with pytest.raises(DatasetContractError) as error:
        validate_source_lineage(frozen)
    assert error.value.code == "invalid_source_lineage"


@pytest.mark.parametrize(
    "change", ["missing", "private", "orphan", "managed", "absent"]
)
def test_incomplete_unsafe_and_undeclared_payloads_are_rejected(change):
    value = record()
    member = value["samples"][0]
    if change == "missing":
        del member["source_lineage"]["source_id"]
    elif change == "private":
        member["source_lineage"]["source_path"] = "/host/private.png"
    elif change == "orphan":
        del value["source_lineage_contract"]
    elif change == "managed":
        del member["asset_reference"]
    else:
        del member["source_lineage"]
    with pytest.raises(DatasetContractError) as error:
        validate_source_lineage(value)
    assert error.value.code == "invalid_source_lineage"


def test_single_member_interpretation_does_not_walk_other_members():
    value = record()
    member = value["samples"][0]
    value["samples"] = object()
    assert source_lineage_evidence(value, member)["status"] == "recorded"
