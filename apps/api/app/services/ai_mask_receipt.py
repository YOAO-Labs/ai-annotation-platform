from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from app.config import settings
from app.schemas.ai_mask import AiMaskAcceptRequest

RECEIPT_TTL_SECONDS = 15 * 60
_RECEIPT_VERSION = 1
_MAX_CLOCK_SKEW_SECONDS = 30
_RESERVED_CLAIMS = {"v", "iat", "exp"}


class AiMaskReceiptError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _receipt_key() -> bytes:
    return hashlib.sha256(
        f"aap:ai-mask-receipt:v{_RECEIPT_VERSION}:{settings.secret_key}".encode()
    ).digest()


def issue_ai_mask_receipt(claims: dict[str, Any], *, now: int | None = None) -> str:
    if _RESERVED_CLAIMS.intersection(claims):
        raise ValueError("receipt claims cannot override reserved fields")
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": _RECEIPT_VERSION,
        "iat": issued_at,
        "exp": issued_at + RECEIPT_TTL_SECONDS,
        **claims,
    }
    encoded = _b64_encode(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    signature = hmac.new(_receipt_key(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64_encode(signature)}"


def verify_ai_mask_receipt(token: str, *, now: int | None = None) -> dict[str, Any]:
    if len(token) > 4096:
        raise AiMaskReceiptError(
            "invalid_candidate_receipt", "candidate receipt is invalid"
        )
    try:
        encoded, signature_text = token.split(".", 1)
        signature = _b64_decode(signature_text)
        expected = hmac.new(_receipt_key(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(_b64_decode(encoded))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AiMaskReceiptError(
            "invalid_candidate_receipt", "candidate receipt is invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != _RECEIPT_VERSION:
        raise AiMaskReceiptError(
            "invalid_candidate_receipt", "candidate receipt is invalid"
        )
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    current = int(time.time() if now is None else now)
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or expires_at - issued_at != RECEIPT_TTL_SECONDS
        or issued_at > current + _MAX_CLOCK_SKEW_SECONDS
    ):
        raise AiMaskReceiptError(
            "invalid_candidate_receipt", "candidate receipt is invalid"
        )
    if expires_at <= current:
        raise AiMaskReceiptError(
            "candidate_receipt_expired", "candidate receipt has expired"
        )
    return payload


__all__ = [
    "AiMaskReceiptError",
    "RECEIPT_TTL_SECONDS",
    "issue_ai_mask_receipt",
    "verify_ai_mask_receipt",
]


class AiMaskAcceptError(ValueError):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **(extra or {})}


def ai_mask_accept_request_digest(data: AiMaskAcceptRequest) -> str:
    payload = data.model_dump(mode="json")
    payload.pop("idempotency_key", None)
    payload["candidate"].pop("receipt", None)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_acceptance_receipt(
    data: AiMaskAcceptRequest,
    *,
    task_id: uuid.UUID,
    content_digest: str,
) -> dict[str, Any]:
    try:
        claims = verify_ai_mask_receipt(data.candidate.receipt)
    except AiMaskReceiptError as exc:
        raise AiMaskAcceptError(
            status_code=409,
            reason=exc.reason,
            message=str(exc),
        ) from exc

    expected = {
        "task_id": str(task_id),
        "frame_index": data.target.frame_index,
        "candidate_id": data.candidate.candidate.candidate_id,
        "candidate_index": data.candidate.candidate_index,
        "content_digest": content_digest,
        "prompt_revision": data.candidate.prompt_revision,
        "score": data.candidate.candidate.score,
        "routing": data.routing.model_dump(mode="json"),
        "inference": data.inference.model_dump(mode="json"),
        "prompt_summary": data.prompt_summary.model_dump(mode="json"),
    }
    if any(claims.get(key) != value for key, value in expected.items()):
        raise AiMaskAcceptError(
            status_code=409,
            reason="candidate_receipt_mismatch",
            message="candidate receipt does not match the accept request",
        )
    accept_target = claims.get("accept_target")
    if accept_target is not None and accept_target != data.target.model_dump(
        mode="json"
    ):
        raise AiMaskAcceptError(
            status_code=409,
            reason="candidate_receipt_mismatch",
            message="candidate receipt does not match the accept target",
        )
    prompt_source = claims.get("prompt_source")
    if prompt_source is not None:
        expected_source = {
            "source_annotation_id": (
                str(data.target.source_annotation_id)
                if data.target.source_annotation_id is not None
                else None
            ),
            "source_version": data.target.source_version,
        }
        if (
            data.target.mode != "refine"
            or not isinstance(prompt_source, dict)
            or any(
                prompt_source.get(key) != value
                for key, value in expected_source.items()
            )
        ):
            raise AiMaskAcceptError(
                status_code=409,
                reason="candidate_receipt_mismatch",
                message="Mask prompt source does not match the accept target",
            )
    return claims
