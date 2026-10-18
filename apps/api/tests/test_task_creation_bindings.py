"""Consumer validation and retry behavior at the native creation boundary."""

from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4
import json

from celery.exceptions import Retry
from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from sqlalchemy.exc import DBAPIError

from app.application.task_creation_errors import raise_task_creation_http_failure
from app.services.task_creation_contract import (
    CreationChunk,
    CreationJobClaim,
    CreationJobCreated,
    CreationJobDiscovery,
    CreationJobFinished,
    CreationOperation,
    DatasetAssociationFacts,
    ProjectManagerFacts,
    TaskCreationFailure,
    decode_creation_reply,
)
from app.workers import create_tasks


def _operation():
    return {
        **{
            key: str(uuid4())
            for key in (
                "operation_id",
                "actor_id",
                "dataset_id",
                "project_id",
                "association_id",
            )
        },
        "job_id": None,
        "mode": "generic",
        "chunk_size": 2,
        "state": "running",
        "total": 3,
        "next_chunk": 1,
        "processed": 2,
        "created": 2,
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"schema": "aap-task-creation/v2"},
        {"ok": 1},
        {"error": {"code": "source_changed"}},
        {"extra": "private manifest"},
    ],
)
def test_creation_reply_rejects_unknown_or_ambiguous_envelopes(changed):
    reply = {"schema": "aap-task-creation/v1", "ok": True, "data": _operation()}
    with pytest.raises(ValueError):
        decode_creation_reply(
            {**reply, **changed}, "aap-task-creation/v1", CreationOperation
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"created": True},
        {"created": -1},
        {"created": 3},
        {"total": None},
        {"state": "completed"},
        {"state": "accepted"},
        {"chunk_size": 5001},
        {"manifest": []},
    ],
)
def test_operation_rejects_invalid_counts_and_private_fields(changed):
    with pytest.raises(ValidationError):
        CreationOperation.model_validate({**_operation(), **changed})


def test_chunk_rejects_a_cursor_that_would_skip_an_uncommitted_chunk():
    with pytest.raises(ValidationError, match="cursor"):
        CreationChunk.model_validate(
            {
                "operation_id": str(uuid4()),
                "project_id": str(uuid4()),
                "chunk_index": 0,
                "created": 2,
                "processed": 2,
                "cumulative_created": 2,
                "cumulative_processed": 2,
                "total": 3,
                "next_chunk": 2,
                "complete": False,
                "project_total": 2,
            }
        )


def test_discovery_rejects_missing_operation_scope():
    with pytest.raises(ValidationError):
        CreationJobDiscovery(job_id=uuid4(), operation_id=None, status="running")


def test_domain_failure_preserves_kind_facts_and_http_contract():
    with pytest.raises(TaskCreationFailure) as raised:
        decode_creation_reply(
            {
                "schema": "aap-dataset-association/v1",
                "ok": False,
                "error": {
                    "code": "media_kind_mismatch",
                    "details": {
                        "project_data_type": "image",
                        "dataset_data_type": "point_cloud",
                        "project_scene_mode": False,
                        "dataset_has_scenes": False,
                    },
                },
            },
            "aap-dataset-association/v1",
            DatasetAssociationFacts,
        )
    with pytest.raises(HTTPException) as http:
        raise_task_creation_http_failure(raised.value)
    assert http.value.status_code == 422
    assert http.value.detail == "data_type 不匹配(项目 image / 数据集 lidar)"


def test_disabled_actor_failure_keeps_bearer_authentication_header():
    with pytest.raises(HTTPException) as raised:
        raise_task_creation_http_failure(TaskCreationFailure("account_not_active"))
    assert raised.value.status_code == 401
    assert raised.value.headers == {"WWW-Authenticate": "Bearer"}


def test_python_bindings_match_frozen_public_response_fields():
    root = Path(__file__).parents[1] / "alembic/sql"
    authority = json.loads((root / "0192_creation_authority.schema.json").read_text())[
        "$defs"
    ]
    jobs = json.loads((root / "0193_task_creation_jobs.schema.json").read_text())[
        "$defs"
    ]
    labeling = json.loads((root / "0194_task_creation.schema.json").read_text())[
        "$defs"
    ]
    pairs = [
        (ProjectManagerFacts, authority["projectManagerSuccess"]["properties"]["data"]),
        (
            DatasetAssociationFacts,
            authority["datasetAssociationSuccess"]["properties"]["data"],
        ),
        (CreationOperation, labeling["operationSummary"]),
        (CreationChunk, labeling["taskCreationChunkData"]),
        *[
            (model, jobs[name]["allOf"][1]["properties"]["data"])
            for model, name in (
                (CreationJobCreated, "createSuccess"),
                (CreationJobDiscovery, "discoverySuccess"),
                (CreationJobClaim, "claimSuccess"),
                (CreationJobFinished, "finishSuccess"),
            )
        ],
    ]
    for model, frozen in pairs:
        actual = model.model_json_schema()
        assert set(actual["properties"]) == set(frozen["properties"]), model
        assert set(actual["required"]) == set(frozen["required"]), model
        assert actual["additionalProperties"] is frozen["additionalProperties"] is False


@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01", "57014", "08006"])
def test_worker_retries_transient_persistence_failures(sqlstate):
    class DriverFailure(Exception):
        pass

    original = DriverFailure()
    original.sqlstate = sqlstate
    error = DBAPIError("redacted", None, original)
    assert create_tasks._retryable(error)
    assert not create_tasks._retryable(TaskCreationFailure("association_changed"))


def test_contention_after_many_deliveries_requeues_instead_of_failing(monkeypatch):
    async def temporary_failure(**kwargs):
        raise create_tasks.RetryableTaskCreation("unavailable")

    monkeypatch.setattr(create_tasks, "_run_create_tasks", temporary_failure)
    retry = Mock(side_effect=Retry())
    task = create_tasks.run_create_tasks
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(id=str(uuid4()), retries=100, called_directly=False)
    try:
        with pytest.raises(Retry):
            task.run(str(uuid4()))
    finally:
        task.pop_request()
    assert retry.call_args.kwargs["countdown"] == 30
    assert retry.call_args.kwargs["max_retries"] is None
    assert task.acks_late is True and task.reject_on_worker_lost is True
