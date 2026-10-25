"""Small public-contract and durable-schema checks without database provisioning."""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from app.db.models.managed_original_reclaim import (
    ManagedOriginalReclaimAttempt,
    ManagedOriginalReclaimClaim,
)
from app.schemas.managed_original_reclaim import (
    ManagedOriginalReclaimReview,
    ManagedOriginalReclaimApply,
)


def test_review_is_explicit_and_rejects_unknown_inputs():
    for reason in ("short", "        ", "review\nreason"):
        with pytest.raises(ValidationError):
            ManagedOriginalReclaimReview(reason=reason)
    with pytest.raises(ValidationError):
        ManagedOriginalReclaimReview(reason="explicit reason", bucket="not public")
    value = ManagedOriginalReclaimApply(
        reason="explicit reason", impact_token="signed", retry_of_id=uuid.uuid4()
    )
    assert value.retry_of_id is not None


def test_claim_is_permanent_group_identity_and_attempt_pins_version():
    dialect = postgresql.dialect()
    claim = str(
        CreateTable(ManagedOriginalReclaimClaim.__table__).compile(dialect=dialect)
    )
    attempt = str(
        CreateTable(ManagedOriginalReclaimAttempt.__table__).compile(dialect=dialect)
    )
    assert "UNIQUE (bucket, object_key)" in claim
    assert "ON DELETE RESTRICT" in claim
    assert "version_id<>'null'" in attempt
    assert "physical_settled" in attempt
    assert "ON DELETE SET NULL" in attempt
