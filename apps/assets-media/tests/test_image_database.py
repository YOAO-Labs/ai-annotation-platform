from __future__ import annotations

from contextlib import asynccontextmanager
import json
from uuid import uuid4

import pytest

from aap_assets_images.contract import (
    IMAGE_SCHEMA,
    OPERATIONS,
    AdvanceImageBackfillRequest,
    Artifact,
    ClaimImageMediaRequest,
    ClaimedImageWork,
    FailImageMediaRequest,
    FinishImageMediaRequest,
    HeartbeatImageMediaRequest,
    ImageFailure,
    ImagePyramidManifest,
    ImageTileArtifact,
    ObjectIdentity,
    PyramidResult,
    ReconcileImageMediaRequest,
    ReadImageSourceRequest,
)
from aap_assets_media.contract import OPERATIONS as VIDEO_OPERATIONS
from aap_assets_images_runtime.database import (
    AsyncpgImageDatabase,
    prepare_native_call,
    validate_native_result,
)
from aap_assets_images_runtime.health import check_capabilities


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.transactions.append("rollback" if exc_type else "commit")
        return False


class _Connection:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []
        self.transactions = []

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, statement, *parameters):
        self.calls.append((statement, parameters))
        return self.raw


class _Pool:
    def __init__(self, raw):
        self.connection = _Connection(raw)
        self.acquisitions = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        yield self.connection


class _HealthConnection:
    def __init__(self, *, identity_ok, signatures, rows):
        self.identity_ok = identity_ok
        self.signatures = signatures
        self.rows = rows
        self.transactions = []

    def transaction(self, *, readonly=False):
        assert readonly is True
        return _Transaction(self)

    async def execute(self, _statement):
        return None

    async def fetchval(self, statement, *parameters):
        if "to_regprocedure" in statement:
            return self.signatures.get(parameters[0])
        return self.identity_ok

    async def fetch(self, _statement):
        return self.rows


class _HealthPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self, timeout=None):
        assert timeout == 2
        yield self.connection


def _ok(data):
    return {"schema": IMAGE_SCHEMA, "ok": True, "data": data}


def _worker_requests():
    job_id, lease_token = uuid4(), uuid4()
    identity = ObjectIdentity(etag="source", byte_size=12)
    manifest = ImagePyramidManifest(
        **{
            "schema": "aap-image-pyramid/v1",
            "generation": 17,
            "sourceFingerprint": "sha256:" + "a" * 64,
            "normalizationVersion": "exif-autorotate-srgb-v1",
            "width": 1,
            "height": 1,
            "tileSize": 512,
            "overlap": 1,
            "format": "webp",
            "levels": [
                {
                    "level": 0,
                    "scaleFactor": 1,
                    "width": 1,
                    "height": 1,
                    "columns": 1,
                    "rows": 1,
                }
            ],
            "overview": {
                "width": 1,
                "height": 1,
                "contentDigest": "sha256:" + "b" * 64,
            },
        }
    )
    prefix = f"image-pyramids/{uuid4()}/g17/a{lease_token}/"
    pyramid = PyramidResult(
        source_identity=identity,
        source_sha256="a" * 64,
        manifest=manifest,
        manifest_artifact=Artifact(
            key=prefix + "manifest.json", byte_size=10, sha256="c" * 64
        ),
        overview_artifact=Artifact(
            key=prefix + "overview.webp", byte_size=11, sha256="b" * 64
        ),
        tile_artifacts=[
            ImageTileArtifact(
                level=0,
                x=0,
                y=0,
                artifact=Artifact(
                    key=prefix + "tiles/0/0/0.webp",
                    byte_size=12,
                    sha256="d" * 64,
                ),
            )
        ],
        tile_count=1,
        retained_bytes=33,
    )
    return {
        "claim_image_media": ClaimImageMediaRequest(worker_id=uuid4()),
        "heartbeat_image_media": HeartbeatImageMediaRequest(
            job_id=job_id, generation=1, lease_token=lease_token
        ),
        "finish_image_media": FinishImageMediaRequest(
            job_id=job_id,
            generation=1,
            lease_token=lease_token,
            result=pyramid,
        ),
        "fail_image_media": FailImageMediaRequest(
            job_id=job_id,
            generation=1,
            lease_token=lease_token,
            error_code="decode_failed",
            error_detail="decode_failed",
        ),
        "advance_image_backfill": AdvanceImageBackfillRequest(
            job_id=job_id, generation=1, lease_token=lease_token
        ),
        "reconcile_image_media": ReconcileImageMediaRequest(),
    }


def test_worker_login_has_only_the_six_frozen_execution_operations():
    names = {
        name for name, operation in OPERATIONS.items() if operation.role == "worker"
    }
    assert names == {
        "claim_image_media",
        "heartbeat_image_media",
        "finish_image_media",
        "fail_image_media",
        "advance_image_backfill",
        "reconcile_image_media",
    }
    assert len(names) == 6
    requests = _worker_requests()
    for name, request in requests.items():
        statement, parameters = prepare_native_call(
            name, request, allowed_roles={"worker"}
        )
        operation = OPERATIONS[name]
        assert f"aap_assets_v1.{name}(" in statement
        assert all(
            f"${position}::{sql_type}" in statement
            for position, sql_type in enumerate(operation.sql_types, start=1)
        )
        assert len(parameters) == len(operation.arguments)


def test_finish_binding_preserves_frozen_manifest_wire_aliases():
    request = _worker_requests()["finish_image_media"]
    _statement, parameters = prepare_native_call(
        "finish_image_media", request, allowed_roles={"worker"}
    )
    serialized = json.loads(parameters[3])
    manifest = serialized["manifest"]
    assert manifest["schema"] == "aap-image-pyramid/v1"
    assert manifest["sourceFingerprint"] == "sha256:" + "a" * 64
    assert manifest["overview"]["contentDigest"] == "sha256:" + "b" * 64
    assert "schema_" not in manifest
    assert "source_fingerprint" not in manifest


def test_native_binding_enforces_per_page_record_budget(monkeypatch):
    import aap_assets_images_runtime.database as database_module

    request = AdvanceImageBackfillRequest(
        job_id=uuid4(), generation=1, lease_token=uuid4(), batch_size=1
    )
    result = {
        "job_id": request.job_id,
        "generation": request.generation,
        "lease_token": request.lease_token,
        "scheduled": 0,
        "skipped": 0,
        "complete": True,
        "next_owner_kind": None,
        "next_owner_id": None,
    }
    monkeypatch.setattr(database_module, "PAGE_BYTES", 1)

    with pytest.raises(ImageFailure, match="resource_budget_exceeded"):
        validate_native_result("advance_image_backfill", request, _ok(result))


@pytest.mark.asyncio
async def test_capability_preflight_requires_the_exact_six_worker_oids():
    signatures = {
        "aap_assets_v1." + name + "(" + ", ".join(operation.sql_types) + ")": index
        for index, (name, operation) in enumerate(
            (
                (name, operation)
                for name, operation in OPERATIONS.items()
                if operation.role == "worker"
            ),
            start=100,
        )
    }
    rows = [{"oid": oid, "prosecdef": True} for oid in signatures.values()]
    connection = _HealthConnection(
        identity_ok=True,
        signatures=signatures,
        rows=rows,
    )

    assert await check_capabilities(_HealthPool(connection), role="worker")
    connection.rows = [*rows, {"oid": 999, "prosecdef": True}]
    assert not await check_capabilities(_HealthPool(connection), role="worker")


@pytest.mark.asyncio
async def test_shared_control_preflight_includes_video_and_image_owner_capabilities():
    operations = [VIDEO_OPERATIONS, OPERATIONS]
    control_signatures = {
        "aap_assets_v1." + name + "(" + ", ".join(operation.sql_types) + ")": oid
        for oid, (name, operation) in enumerate(
            (
                (name, operation)
                for operation_set in operations
                for name, operation in operation_set.items()
                if operation.role in {"control", "system"}
            ),
            start=100,
        )
    }
    assert len(control_signatures) == 32
    rows = [{"oid": oid, "prosecdef": True} for oid in control_signatures.values()]
    connection = _HealthConnection(
        identity_ok=True,
        signatures=control_signatures,
        rows=rows,
    )

    assert await check_capabilities(_HealthPool(connection), role="control")
    connection.rows = rows[:-1]
    assert not await check_capabilities(_HealthPool(connection), role="control")


@pytest.mark.asyncio
async def test_asyncpg_binding_commits_only_after_typed_reply_and_scope_validation():
    pool = _Pool(_ok({"claim": None}))
    database = AsyncpgImageDatabase(pool)
    request = ClaimImageMediaRequest(worker_id=uuid4())

    result = await database.call("claim_image_media", request)

    statement, parameters = pool.connection.calls[0]
    assert "aap_assets_v1.claim_image_media(" in statement
    assert parameters[0] == request.worker_id
    assert isinstance(result, ClaimedImageWork)
    assert result.claim is None
    assert pool.connection.transactions == ["commit"]


@pytest.mark.asyncio
async def test_finish_binding_accepts_pyramid_generation_independent_of_job_generation():
    request = _worker_requests()["finish_image_media"]
    assert request.generation == 1
    assert request.result.manifest.generation == 17
    pool = _Pool(
        _ok(
            {
                "job_id": request.job_id,
                "generation": request.generation,
                "outcome": "published",
            }
        )
    )

    result = await AsyncpgImageDatabase(pool).call("finish_image_media", request)

    assert result.job_id == request.job_id
    assert pool.connection.transactions == ["commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", ["wrong_generation", "wrong_lease"])
async def test_finish_binding_rejects_pyramid_prefix_outside_manifest_and_lease(
    bad_path,
):
    request = _worker_requests()["finish_image_media"]
    result = request.result
    if bad_path == "wrong_generation":
        bad_prefix = result.manifest_artifact.key.replace("/g17/", "/g1/")
    else:
        bad_prefix = result.manifest_artifact.key.replace(
            f"/a{request.lease_token}/", f"/a{uuid4()}/"
        )
    bad_result = result.model_copy(
        update={
            "manifest_artifact": result.manifest_artifact.model_copy(
                update={"key": bad_prefix}
            )
        }
    )
    request = request.model_copy(update={"result": bad_result})
    pool = _Pool(_ok({}))

    with pytest.raises(ImageFailure, match="invalid_request"):
        await AsyncpgImageDatabase(pool).call("finish_image_media", request)

    assert pool.acquisitions == 0


@pytest.mark.asyncio
async def test_invalid_reply_rolls_back_before_transaction_commit():
    pool = _Pool({"schema": "wrong/v1", "ok": True, "data": {"claim": None}})
    database = AsyncpgImageDatabase(pool)

    with pytest.raises(ImageFailure, match="invalid_response"):
        await database.call(
            "claim_image_media", ClaimImageMediaRequest(worker_id=uuid4())
        )

    assert pool.connection.transactions == ["rollback"]


@pytest.mark.asyncio
async def test_nonworker_operation_is_rejected_before_acquiring_connection():
    pool = _Pool(_ok({}))
    database = AsyncpgImageDatabase(pool, allowed_roles={"worker"})

    with pytest.raises(PermissionError):
        await database.call(
            "read_image_source",
            # The operation/request pair itself is valid; only its role is wrong.
            ReadImageSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4()),
        )

    assert pool.acquisitions == 0
