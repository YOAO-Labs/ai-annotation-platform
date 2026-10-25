from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.data_management.actions import (
    assert_idempotent_request_matches,
    find_idempotent_job,
    lock_idempotency_key,
    request_digest,
)
from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData


@pytest.mark.asyncio
async def test_lock_idempotency_key_delegates_scoped_arguments(monkeypatch):
    db = object()
    project_id, actor_id = uuid.uuid4(), uuid.uuid4()
    calls = []

    async def lock_action_idempotency(self, **kwargs):
        calls.append((self.db, kwargs))

    monkeypatch.setattr(
        JobsDomainQueryPostgresData,
        "lock_action_idempotency",
        lock_action_idempotency,
        raising=False,
    )

    await lock_idempotency_key(
        db,
        action="preannotate",
        project_id=project_id,
        actor_id=actor_id,
        key=" retry-1 ",
    )

    assert calls == [
        (
            db,
            {
                "action": "preannotate",
                "project_id": project_id,
                "actor_id": actor_id,
                "key": " retry-1 ",
            },
        )
    ]


@pytest.mark.asyncio
async def test_find_idempotent_job_delegates_and_returns_job(monkeypatch):
    db = object()
    project_id, actor_id = uuid.uuid4(), uuid.uuid4()
    job = object()
    calls = []

    async def find_action_idempotent_job(self, **kwargs):
        calls.append((self.db, kwargs))
        return job

    monkeypatch.setattr(
        JobsDomainQueryPostgresData,
        "find_action_idempotent_job",
        find_action_idempotent_job,
        raising=False,
    )

    assert (
        await find_idempotent_job(
            db,
            kind="batch_predict",
            project_id=project_id,
            actor_id=actor_id,
            key="retry-1",
        )
        is job
    )
    assert calls == [
        (
            db,
            {
                "kind": "batch_predict",
                "project_id": project_id,
                "actor_id": actor_id,
                "key": "retry-1",
            },
        )
    ]


def test_request_digest_is_stable_for_equivalent_mappings():
    assert request_digest({"left": 1, "right": [2]}) == request_digest(
        {"right": [2], "left": 1}
    )
    assert request_digest({"left": 1}) != request_digest({"left": 2})


def test_idempotent_request_rejects_a_different_digest_with_conflict():
    job_id = uuid.uuid4()
    job = SimpleNamespace(
        id=job_id,
        payload={"data_manager_request_digest": "previous"},
    )

    with pytest.raises(HTTPException) as error:
        assert_idempotent_request_matches(job, digest="current")

    assert error.value.status_code == 409
    assert error.value.detail == {
        "code": "idempotency_conflict",
        "message": "Idempotency-Key was already used with another request",
        "job_id": str(job_id),
    }
