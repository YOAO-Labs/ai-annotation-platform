import json
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import DBAPIError

from aap_labeling.annotation_prediction_contract import (
    ANNOTATION_PREDICTION_PROTOCOL,
    ApplySecondaryAttributes,
)
from app.services.labeling_annotation_prediction import annotation_prediction_command


@pytest.mark.asyncio
async def test_prediction_annotation_binding_reads_the_driver_error_not_its_wrapper():
    driver = Exception("native driver error")
    driver.message = "aap_annotation_prediction_error"
    driver.detail = json.dumps(
        {"status_code": 409, "detail": {"reason": "annotation_target_changed"}}
    )
    driver.sqlstate = "P0001"
    adapter = Exception(
        "<class 'asyncpg.exceptions.RaiseError'>: aap_annotation_prediction_error"
    )
    adapter.sqlstate = "P0001"
    adapter.__cause__ = driver

    class Database:
        autoflush = False

        async def scalar(self, *_args):
            raise DBAPIError("owner command", {}, adapter)

    request = ApplySecondaryAttributes(
        schema=ANNOTATION_PREDICTION_PROTOCOL,
        operation="apply_secondary_attributes",
        annotation_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        expected_batch_id=None,
        expected_status="in_progress",
        expected_review_round_id=None,
        expected_version=1,
        attributes={"color": "red"},
        attributes_meta={},
    )
    with pytest.raises(HTTPException) as rejected:
        await annotation_prediction_command(Database(), request)
    assert (rejected.value.status_code, rejected.value.detail) == (
        409,
        {"reason": "annotation_target_changed"},
    )
