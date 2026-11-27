"""Issue and recheck a reference-input receipt in the caller's transaction."""

from dataclasses import dataclass
import json
import uuid

from app.services.content_snapshot import ContentError
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.reference_bindings import (
    normalize_binding,
    reference_binding,
    task_binding,
)
from app.services.reference_keys import is_reference


_ISSUER = object()


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    session: object
    transaction: object
    task_id: uuid.UUID
    binding: str
    evidence: str


async def validate_receipt(db, task_id, project_id, receipt):
    if (
        type(receipt) is not _Receipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
    ):
        raise ContentError("prediction_reference_receipt_invalid")
    task = await get_core_row(db, task_row, task_id, populate_existing=True)
    if (
        task is None
        or task.project_id != project_id
        or not is_reference(task.file_path)
    ):
        raise ContentError("prediction_reference_receipt_invalid")
    binding = {**task_binding(task), "reference": await reference_binding(db, task)}
    if binding != normalize_binding(json.loads(receipt.binding)):
        raise ContentError("content_binding_changed")
    return json.loads(receipt.evidence)


def issue_receipt(db, task_id, binding, evidence):
    return _Receipt(
        _ISSUER,
        db.sync_session,
        db.sync_session.get_transaction(),
        task_id,
        json.dumps(binding, sort_keys=True),
        json.dumps(evidence, sort_keys=True),
    )
