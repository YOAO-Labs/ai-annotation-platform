"""Failed-prediction retry limits and explicit execution operations."""

import json
import uuid
from typing import Protocol

MAX_RETRY_COUNT = 3
MAX_REQUEST_CONTEXT_BYTES = 8 * 1024


def retryable_request_context(context: dict | None) -> dict | None:
    if context is None:
        return None
    payload = json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode()
    return context if len(payload) <= MAX_REQUEST_CONTEXT_BYTES else None


class PredictionRetryDispatcher(Protocol):
    def dispatch_prediction_retry(
        self,
        failed_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        execution_queue: str,
    ) -> None:
        """Dispatch one retry to the execution queue selected before claim commit."""
        ...

    def dispatch_job_item_retry(
        self, failed_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        """Dispatch a recorded batch failure using the existing default placement."""
        ...
