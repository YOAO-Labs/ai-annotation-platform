"""Observe through Inference while retaining the original caller transaction."""

from __future__ import annotations

import json
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_inference.backend_endpoint import (
    BackendEndpointManager,
    BackendEndpointUnavailable,
)
from aap_inference_contract.client import BackendHealthObservationHttpClient
from aap_inference_contract.health_observation import (
    BackendHealthObservationInvalidRequest,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    make_health_observation_request,
)
from aap_inference_contract.health_observation_outcome import (
    decode_health_observation_outcome,
)
from aap_workspace.config import secret_source
from app.config import settings

_ENDPOINT_CAPTURE = text(
    "SELECT aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject("
    "CAST(:registry_id AS uuid),CAST(:request_id AS uuid),CAST(:purpose AS text),"
    "CAST(:binding_json AS text))"
)
_CAPTURE = text(
    "SELECT aap_inference_health_observation_v1.capture_health_observation_subject("
    "CAST(:registry_id AS uuid),CAST(:request_id AS uuid),CAST(:backend_json AS text),"
    "CAST(:challenge AS text),CAST(:timeout AS double precision))"
)
_VERIFY = text(
    "SELECT aap_inference_health_observation_v1.verify_health_observation("
    "CAST(:subject AS text),CAST(:registry_id AS uuid),CAST(:request_id AS uuid),"
    "CAST(:evidence AS text))"
)


async def _scalar(db, statement, arguments=None):
    try:
        # Capture and evidence verification borrow the existing caller UOW.
        with db.no_autoflush:
            return await db.scalar(statement, arguments or {})
    except SQLAlchemyError:
        raise BackendHealthObservationUnavailable() from None


class _BorrowedEndpointCapture:
    """Capture a readonly registered endpoint without ever opening it in API code."""

    def __init__(self, db):
        self.db = db

    async def capture_subject(self, registry_id, request_id, purpose, binding):
        if binding is not None:
            raise BackendEndpointUnavailable()
        value = await _scalar(
            self.db,
            _ENDPOINT_CAPTURE,
            {
                "registry_id": registry_id,
                "request_id": request_id,
                "purpose": purpose,
                "binding_json": None,
            },
        )
        return value

    async def open_subject(self, subject, registry_id, request_id, purpose, authority):
        raise BackendEndpointUnavailable()

    async def readiness(self):
        return False


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
    def __init__(self, db, backend):
        self.db = db
        self.registry_id = uuid.UUID(str(backend.id))
        self.timeout = settings.ml_health_timeout
        self.outcome = None

    async def health_meta(self, *, gpu_health_challenge=None):
        request_id = uuid.uuid4()
        try:
            endpoint_subject = await BackendEndpointManager(
                _BorrowedEndpointCapture(self.db)
            ).capture(
                self.registry_id,
                request_id=request_id,
                purpose="health_observation",
            )
            backend_json = json.dumps(
                {
                    "id": str(self.registry_id),
                    "endpoint_subject": endpoint_subject.model_dump(
                        mode="json", by_alias=True
                    ),
                },
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            subject = await _scalar(
                self.db,
                _CAPTURE,
                {
                    "registry_id": self.registry_id,
                    "request_id": request_id,
                    "backend_json": backend_json,
                    "challenge": gpu_health_challenge,
                    "timeout": self.timeout,
                },
            )
            request = make_health_observation_request(
                self.registry_id, subject, request_id=request_id
            )
            url = settings.inference_health_observation_url
            if not url:
                raise BackendHealthObservationUnavailable()
            try:
                token = secret_source(
                    settings.inference_health_observation_token,
                    settings.inference_health_observation_token_file,
                )
            except (ValueError, OSError):
                raise BackendHealthObservationUnavailable() from None
            client = BackendHealthObservationHttpClient(
                url,
                token,
                timeout_seconds=settings.inference_health_observation_timeout_seconds,
            )
            reply = decode_health_observation_reply(
                request, await client.execute(request)
            )
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
            BackendEndpointUnavailable,
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


def health_observation_client_factory(db):
    def create(backend):
        return HealthObservationClient(db, backend)

    return create
