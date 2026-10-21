"""Observe through Inference while retaining the original caller transaction."""

from __future__ import annotations

import json
import uuid
import httpx

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_inference.health_observation_manager import BackendHealthObservationManager
from aap_inference.health_observation_postgres import (
    BackendHealthObservationPostgresData,
)
from aap_inference_contract.client import BackendHealthObservationHttpClient
from aap_inference_contract.health_observation import (
    BackendHealthObservationInvalidRequest,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    health_observation_policy_ready,
    make_health_observation_request,
)
from aap_inference_contract.health_observation_outcome import (
    decode_health_observation_outcome,
)
from aap_workspace.config import secret_source
from app.config import settings

_POLICY = text("SELECT aap_inference_health_observation_v1.health_observation_policy()")
_CAPTURE = text(
    "SELECT aap_inference_health_observation_v1.capture_health_observation_subject("
    "CAST(:registry_id AS uuid),CAST(:request_id AS uuid),CAST(:backend_json AS text),"
    "CAST(:challenge AS text),CAST(:timeout AS double precision))"
)
_OPEN = text(
    "SELECT aap_inference_health_observation_v1.open_health_observation_subject("
    "CAST(:subject AS text),CAST(:registry_id AS uuid),CAST(:request_id AS uuid))"
)
_SEAL = text(
    "SELECT aap_inference_health_observation_v1.seal_health_observation("
    "CAST(:subject AS text),CAST(:registry_id AS uuid),CAST(:request_id AS uuid),"
    "CAST(:observation AS text))"
)
_VERIFY = text(
    "SELECT aap_inference_health_observation_v1.verify_health_observation("
    "CAST(:subject AS text),CAST(:registry_id AS uuid),CAST(:request_id AS uuid),"
    "CAST(:evidence AS text))"
)


async def _scalar(db, statement, arguments=None):
    try:
        # These capabilities must not flush pending caller work during backend I/O.
        with db.no_autoflush:
            return await db.scalar(statement, arguments or {})
    except SQLAlchemyError:
        raise BackendHealthObservationUnavailable() from None


class BorrowedHealthObservationData:
    def __init__(self, db):
        self.db = db

    async def readiness(self):
        try:
            return health_observation_policy_ready(await _scalar(self.db, _POLICY))
        except BackendHealthObservationUnavailable:
            return False

    async def open_subject(self, subject, registry_id, request_id):
        value = await _scalar(
            self.db,
            _OPEN,
            {"subject": subject, "registry_id": registry_id, "request_id": request_id},
        )
        if type(value) is not str:
            raise BackendHealthObservationUnavailable()
        return value

    async def seal_observation(self, subject, registry_id, request_id, raw_json):
        value = await _scalar(
            self.db,
            _SEAL,
            {
                "subject": subject,
                "registry_id": registry_id,
                "request_id": request_id,
                "observation": raw_json,
            },
        )
        if type(value) is not str:
            raise BackendHealthObservationUnavailable()
        return value


def _verified_outcome(raw):
    outcome = decode_health_observation_outcome(raw)
    if outcome["kind"] in ("legacy_constructor_error", "legacy_health_error"):
        exceptions = {
            "ValueError": ValueError,
            "TypeError": TypeError,
            "AttributeError": AttributeError,
            "OverflowError": OverflowError,
            "InvalidURL": httpx.InvalidURL,
            "RuntimeError": RuntimeError,
        }
        raise exceptions[outcome["error_type"]](outcome["error_message"])
    return outcome


class _SetupFailed(Exception):
    pass


class HealthObservationClient:
    def __init__(self, db, backend, local_client):
        self.db = db
        # PostgreSQL drivers may return a UUID subclass after an ORM refresh.
        self.registry_id = uuid.UUID(str(backend.id))
        # Preserve constructor errors locally; health/setup do not use extra
        # parameters or concurrency gates, so omit that unrelated pending JSON.
        self.backend_json = json.dumps(
            {
                "id": str(backend.id),
                "url": backend.url,
                "auth_method": backend.auth_method,
                "auth_token": backend.auth_token,
                "gpu_resource_id": backend.gpu_resource_id,
                "vram_budget_mb": getattr(backend, "vram_budget_mb", None),
                "extra_params": {},
            },
            ensure_ascii=True,
            allow_nan=True,
        )
        self.timeout = settings.ml_health_timeout
        self.local_client = local_client
        self.outcome = None

    async def health_meta(self, *, gpu_health_challenge=None):
        request_id = uuid.uuid4()
        try:
            subject = await _scalar(
                self.db,
                _CAPTURE,
                {
                    "registry_id": self.registry_id,
                    "request_id": request_id,
                    "backend_json": self.backend_json,
                    "challenge": gpu_health_challenge,
                    "timeout": self.timeout,
                },
            )
            request = make_health_observation_request(
                self.registry_id, subject, request_id=request_id
            )
            try:
                dsn = secret_source(
                    settings.inference_health_observation_database_url,
                    settings.inference_health_observation_database_url_file,
                )
                url = settings.inference_health_observation_url
                if url and dsn:
                    raise BackendHealthObservationUnavailable()
                if url:
                    token = secret_source(
                        settings.inference_health_observation_token,
                        settings.inference_health_observation_token_file,
                    )
                    client = BackendHealthObservationHttpClient(
                        url,
                        token,
                        timeout_seconds=settings.inference_health_observation_timeout_seconds,
                    )
                elif dsn:
                    data = BackendHealthObservationPostgresData(dsn)
            except (ValueError, OSError):
                raise BackendHealthObservationUnavailable() from None
            if url:
                reply = await client.execute(request)
            elif dsn:
                try:
                    reply = await BackendHealthObservationManager(data).execute(request)
                finally:
                    await data.close()
            else:
                reply = await BackendHealthObservationManager(
                    BorrowedHealthObservationData(self.db),
                    client_factory=lambda backend: self.local_client,
                ).execute(request)
            reply = decode_health_observation_reply(request, reply)
            raw = await _scalar(
                self.db,
                _VERIFY,
                {
                    "subject": subject,
                    "registry_id": self.registry_id,
                    "request_id": request_id,
                    "evidence": reply.evidence,
                },
            )
            self.outcome = _verified_outcome(raw)
        except (
            BackendHealthObservationUnavailable,
            BackendHealthObservationInvalidRequest,
        ):
            raise HTTPException(
                status_code=503,
                detail={"error_code": "inference_health_observation_unavailable"},
            ) from None
        return self.outcome["healthy"], self.outcome["meta"]

    async def setup(self):
        if self.outcome is None or self.outcome["setup_failed"]:
            raise _SetupFailed()
        return self.outcome["setup"]


def health_observation_client_factory(db, *, client_factory):
    def create(backend):
        # Retain the old constructor/error phase and local instrumentation. The
        # independently deployed provider constructs its own portable transport.
        local_client = client_factory(backend)
        return HealthObservationClient(db, backend, local_client)

    return create
