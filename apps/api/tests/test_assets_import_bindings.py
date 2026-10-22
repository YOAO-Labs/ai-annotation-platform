from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.import_contract import (
    ImportAcceptance,
    ImportContext,
    ImportLimits,
    ImportState,
)
from app.schemas.dataset import DatasetImportFromConnectionRequest
from app.services.assets_import import DatasetImportCapabilities


def _success(data: dict) -> dict:
    return {"schema": "aap-dataset-import/v1", "ok": True, "data": data}


def _failure(code: str) -> dict:
    return {"schema": "aap-dataset-import/v1", "ok": False, "error": {"code": code}}


def _db(payload: dict) -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = payload
    return db


def _context(actor_id, dataset_id, connection_id) -> dict:
    return {
        "actor_id": str(actor_id),
        "dataset_id": str(dataset_id),
        "dataset_display_id": "D-1",
        "dataset_name": "",
        "connection_id": str(connection_id),
        "connection_name": "S3",
        "connection_kind": "s3",
        "connection_config": {
            "endpoint": "https://s3.example.test",
            "bucket": "incoming",
        },
    }


def test_import_body_bounds_match_native_state_contract():
    with pytest.raises(ValidationError):
        DatasetImportFromConnectionRequest(
            connection_id=uuid.uuid4(), source_path="x" * 2049
        )
    with pytest.raises(ValidationError):
        DatasetImportFromConnectionRequest(
            connection_id=uuid.uuid4(), include_globs=["x" * 256]
        )
    with pytest.raises(ValidationError):
        DatasetImportFromConnectionRequest(
            connection_id=uuid.uuid4(), include_globs=["bad\npattern"]
        )


@pytest.mark.asyncio
async def test_import_route_reuses_explicit_idempotency_key(monkeypatch):
    from app.api.v1 import datasets as dataset_api

    actor_id, dataset_id, connection_id, operation_id = (uuid.uuid4() for _ in range(4))
    context = ImportContext.model_validate(
        _context(actor_id, dataset_id, connection_id)
    )
    prepared = AsyncMock(return_value=context)
    accepted = AsyncMock(
        return_value=ImportAcceptance(
            job_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            connection_id=connection_id,
        )
    )
    monkeypatch.setattr(DatasetImportCapabilities, "prepare", prepared)
    monkeypatch.setattr(DatasetImportCapabilities, "accept", accepted)

    async def allow(_db, _target):
        return None

    async def snapshot(_db, *, registration_default):
        assert registration_default is True
        return {
            "dataset_import_max_files": 100,
            "dataset_import_max_total_bytes": 10_000,
            "version": "v1-test",
        }

    monkeypatch.setattr(
        "app.services.connector_guard.assert_connection_target_allowed", allow
    )
    monkeypatch.setattr(
        dataset_api.SystemSettingsService,
        "get_import_limits_snapshot",
        staticmethod(snapshot),
    )

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/v1/datasets/{dataset_id}/import-from-connection",
            "raw_path": f"/api/v1/datasets/{dataset_id}/import-from-connection".encode(),
            "query_string": b"",
            "headers": [(b"host", b"test.local")],
            "server": ("test.local", 80),
            "client": ("127.0.0.1", 12345),
        }
    )
    response = Response()
    db = AsyncMock(spec=AsyncSession)
    user = type("Actor", (), {"id": actor_id})()
    body = DatasetImportFromConnectionRequest(
        connection_id=connection_id,
        source_path="incoming",
        recursive=True,
        include_globs=[" *.jpg ", ""],
    )

    for _ in range(2):
        result = await dataset_api.import_from_connection(
            dataset_id=dataset_id,
            payload=body,
            request=request,
            response=response,
            db=db,
            current_user=user,
            idempotency_key=operation_id,
            registration_default=True,
        )
        assert result.job_id == operation_id
        assert response.headers["Idempotency-Key"] == str(operation_id)

    assert prepared.await_count == 2
    for call_args in prepared.await_args_list:
        assert call_args.kwargs == {
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "connection_id": connection_id,
        }
    assert accepted.await_count == 2
    for call_args in accepted.await_args_list:
        assert call_args.kwargs["operation_id"] == operation_id
        assert call_args.kwargs["request"] == {
            "source_path": "incoming",
            "recursive": True,
            "include_globs": ["*.jpg"],
        }
        assert call_args.kwargs["limits"].version == "v1-test"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_prepare_binds_scoped_actor_dataset_and_connection():
    actor_id, dataset_id, connection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _db(_success(_context(actor_id, dataset_id, connection_id)))

    context = await DatasetImportCapabilities(db).prepare(
        actor_id=actor_id, dataset_id=dataset_id, connection_id=connection_id
    )

    assert isinstance(context, ImportContext)
    assert (context.actor_id, context.dataset_id, context.connection_id) == (
        actor_id,
        dataset_id,
        connection_id,
    )
    statement, parameters = db.scalar.await_args.args
    assert "aap_assets_v1.prepare_dataset_import" in str(statement)
    assert parameters == {
        "actor_id": actor_id,
        "dataset_id": dataset_id,
        "connection_id": connection_id,
    }


@pytest.mark.asyncio
async def test_accept_binds_operation_and_canonical_request_snapshot():
    operation_id, actor_id, dataset_id, connection_id = (uuid.uuid4() for _ in range(4))
    db = _db(
        _success(
            {
                "job_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "connection_id": str(connection_id),
            }
        )
    )
    request = {
        "source_path": "incoming/images",
        "recursive": False,
        "include_globs": ["*.jpg"],
    }
    limits = ImportLimits(
        dataset_import_max_files=100,
        dataset_import_max_total_bytes=1_000,
        version="v1-test",
    )
    audit_context = {"method": "POST", "path": "/datasets/D-1/import-from-connection"}

    accepted = await DatasetImportCapabilities(db).accept(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        connection_id=connection_id,
        request=request,
        limits=limits,
        audit_context=audit_context,
    )

    assert isinstance(accepted, ImportAcceptance)
    statement, parameters = db.scalar.await_args.args
    assert "aap_assets_v1.accept_dataset_import" in str(statement)
    assert parameters["operation_id"] == operation_id
    assert json.loads(parameters["request"]) == request
    assert json.loads(parameters["limits"]) == limits.model_dump(mode="json")
    assert json.loads(parameters["audit_context"]) == audit_context


@pytest.mark.asyncio
async def test_accept_rejects_mismatched_native_receipt_scope():
    operation_id, actor_id, dataset_id, connection_id = (uuid.uuid4() for _ in range(4))
    db = _db(
        _success(
            {
                "job_id": str(uuid.uuid4()),
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "connection_id": str(connection_id),
            }
        )
    )

    with pytest.raises(HTTPException) as failure:
        await DatasetImportCapabilities(db).accept(
            operation_id=operation_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            connection_id=connection_id,
            request={"source_path": "", "recursive": True, "include_globs": []},
            limits=ImportLimits(
                dataset_import_max_files=1,
                dataset_import_max_total_bytes=1,
                version="v1-test",
            ),
            audit_context={},
        )

    assert failure.value.status_code == 502
    assert failure.value.detail == "Dataset import returned an invalid response"


@pytest.mark.asyncio
async def test_cancel_accepts_original_owner_in_receipt_for_current_actor():
    current_actor_id, original_owner_id, job_id, dataset_id, connection_id = (
        uuid.uuid4() for _ in range(5)
    )
    db = _db(
        _success(
            {
                "job_id": str(job_id),
                "actor_id": str(original_owner_id),
                "dataset_id": str(dataset_id),
                "connection_id": str(connection_id),
                "source_path": "incoming",
                "recursive": True,
                "include_globs": [],
                "settings_snapshot": {
                    "dataset_import_max_files": 10,
                    "dataset_import_max_total_bytes": 100,
                    "version": "v1-test",
                },
                "stage": "cancelled",
                "cursor": 0,
                "total": None,
                "total_bytes": None,
                "added": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "linked_tasks": 0,
                "committed_bytes": 0,
                "lease_token": None,
                "lease_expires_at": None,
            }
        )
    )

    state = await DatasetImportCapabilities(db).cancel(
        actor_id=current_actor_id, job_id=job_id
    )

    assert isinstance(state, ImportState)
    assert state.stage == "cancelled"
    assert state.actor_id == original_owner_id
    statement, parameters = db.scalar.await_args.args
    assert "aap_assets_v1.cancel_dataset_import" in str(statement)
    assert parameters == {"actor_id": current_actor_id, "job_id": job_id}


@pytest.mark.asyncio
async def test_failure_mapping_and_database_errors_are_sanitized():
    actor_id, dataset_id, connection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with pytest.raises(HTTPException) as missing:
        await DatasetImportCapabilities(_db(_failure("connection_missing"))).prepare(
            actor_id=actor_id, dataset_id=dataset_id, connection_id=connection_id
        )
    assert missing.value.status_code == 404
    assert missing.value.detail == "连接器不存在"

    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = SQLAlchemyError("private SQL details")
    with pytest.raises(HTTPException) as unavailable:
        await DatasetImportCapabilities(db).prepare(
            actor_id=actor_id, dataset_id=dataset_id, connection_id=connection_id
        )
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == "Dataset import service is unavailable"
