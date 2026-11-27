"""Canonical object key for a standalone task original."""

import uuid

from app.services.content_errors import ContentError


def _reject(code="invalid_receipt", status=409):
    raise ContentError("task_original_" + code, status)


def key_for(task_id, original_id):
    if not isinstance(task_id, uuid.UUID) or not isinstance(original_id, uuid.UUID):
        _reject()
    return f"__aap_task_originals__/{task_id}/{original_id}/payload"
