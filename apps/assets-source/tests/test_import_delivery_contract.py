from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aap_assets_source.import_delivery_contract import (
    DatasetImportDeliveryFailure,
    decode_dataset_import_delivery_claim,
    decode_dataset_import_delivery_completion,
)


def _delivery():
    return {
        "delivery_id": str(uuid4()),
        "job_id": str(uuid4()),
        "kind": "thumbnail",
        "target_id": str(uuid4()),
        "lease_token": str(uuid4()),
        "lease_expires_at": datetime.now(timezone.utc).isoformat(),
    }


def test_claim_decodes_empty_and_strict_delivery_envelopes():
    assert (
        decode_dataset_import_delivery_claim(
            {"schema": "aap-dataset-import-delivery/v1", "ok": True, "data": None}
        )
        is None
    )

    delivery = _delivery()
    assert decode_dataset_import_delivery_claim(
        {"schema": "aap-dataset-import-delivery/v1", "ok": True, "data": delivery}
    ).target_id == UUID(delivery["target_id"])

    with pytest.raises(ValidationError):
        decode_dataset_import_delivery_claim(
            {
                "schema": "aap-dataset-import-delivery/v1",
                "ok": True,
                "data": {**delivery, "credentials": "must not cross this boundary"},
            }
        )


def test_claim_rejects_naive_lease_and_unknown_failure_code():
    delivery = {**_delivery(), "lease_expires_at": "2030-01-01T00:00:00"}
    with pytest.raises(ValidationError):
        decode_dataset_import_delivery_claim(
            {"schema": "aap-dataset-import-delivery/v1", "ok": True, "data": delivery}
        )

    with pytest.raises(ValidationError):
        decode_dataset_import_delivery_claim(
            {
                "schema": "aap-dataset-import-delivery/v1",
                "ok": False,
                "error": {"code": "unexpected"},
            }
        )


def test_completion_is_bound_to_the_claimed_lease():
    delivery_id = uuid4()
    lease_token = uuid4()
    payload = {
        "schema": "aap-dataset-import-delivery/v1",
        "ok": True,
        "data": {
            "delivery_id": str(delivery_id),
            "lease_token": str(lease_token),
            "delivered": True,
        },
    }
    assert decode_dataset_import_delivery_completion(
        payload, delivery_id=delivery_id, lease_token=lease_token
    ).delivered

    with pytest.raises(ValueError, match="does not match the active lease"):
        decode_dataset_import_delivery_completion(
            payload, delivery_id=uuid4(), lease_token=lease_token
        )


def test_lease_lost_is_a_retryable_delivery_failure():
    with pytest.raises(DatasetImportDeliveryFailure, match="lease_lost"):
        decode_dataset_import_delivery_completion(
            {
                "schema": "aap-dataset-import-delivery/v1",
                "ok": False,
                "error": {"code": "lease_lost"},
            },
            delivery_id=uuid4(),
            lease_token=uuid4(),
        )
