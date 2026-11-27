"""Read durable idempotency decisions through the labeling owner."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.ai_mask_accept_contract import (
    AI_MASK_ACCEPT_PROTOCOL,
    AiMaskDecisionData,
    FindAiMaskDecision,
    LockAiMaskDecision,
)

from app.db.models.user import User
from app.schemas.ai_mask import AiMaskAcceptResponse
from app.services.ai_mask_receipt import AiMaskAcceptError
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_mask_edit import ai_mask_accept_command


async def find_decision(
    db: AsyncSession, *, task_id: uuid.UUID, idempotency_key: str, lock: bool = False
) -> AiMaskDecisionData | None:
    result = await ai_mask_accept_command(
        db,
        (LockAiMaskDecision if lock else FindAiMaskDecision)(
            schema=AI_MASK_ACCEPT_PROTOCOL,
            operation="lock_decision" if lock else "find_decision",
            task_id=task_id,
            idempotency_key=idempotency_key,
        ),
    )
    decision = result.decision
    return decision if decision is not None and decision.found else None


def _replay_response(decision: AiMaskDecisionData) -> AiMaskAcceptResponse:
    snapshot = dict(decision.response_json or {})
    snapshot.pop("_reference_input", None)
    snapshot.pop("_original_input", None)
    snapshot["replayed"] = True
    return AiMaskAcceptResponse.model_validate(snapshot)


def validate_replay(
    decision: AiMaskDecisionData,
    request_digest: str,
    current_user: User | IdentityActorFacts,
    *,
    access: Any = None,
) -> AiMaskAcceptResponse:
    if decision.request_digest != request_digest:
        raise AiMaskAcceptError(
            status_code=409,
            reason="idempotency_conflict",
            message="idempotency key was already used for a different request",
        )
    if decision.expires_at is None or decision.expires_at <= datetime.now(timezone.utc):
        raise AiMaskAcceptError(
            status_code=409,
            reason="idempotency_expired",
            message="the previous idempotent result has expired",
        )
    if (
        decision.actor_id is not None
        and decision.actor_id != current_user.id
        and not (access is not None and access.is_manager)
    ):
        raise AiMaskAcceptError(
            status_code=403,
            reason="idempotency_owner_conflict",
            message="idempotency key belongs to another actor",
        )
    return _replay_response(decision)
