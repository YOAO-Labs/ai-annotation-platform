"""Preserve request authority across every asynchronous export's output I/O.

Reference-media proofs remain independent. This receipt adds no API Key scope;
it binds the credential actually authenticated for this request and never renews
that proof when an existing job is republished.
"""

import uuid

from fastapi import HTTPException

from app.core.security import verify_password

_FIELDS = {"schema", "actor_id", "project_id", "api_key_id", "key_proof"}


class ExportOutputAuthorityError(ValueError):
    def __init__(self, code="export_output_authority_changed"):
        super().__init__("导出授权已变化或缺少原请求证明，请重新发起导出")
        self.code = code


def _uuid(value):
    try:
        parsed = uuid.UUID(str(value))
        if not isinstance(value, (str, uuid.UUID)) or str(parsed) != str(value):
            raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ExportOutputAuthorityError() from None


async def _key(db, key_id, actor_id, *, lock):
    from app.services.identity_account_lock_data import IdentityAccountLockData

    facts = await IdentityAccountLockData(db).read_accepted_api_key_facts(
        actor_id, key_id
    )
    if facts is None:
        raise ExportOutputAuthorityError()
    return {
        # Non-secret digest revision: rotation still changes the receipt.
        "hash": facts.revision,
        "scopes": sorted(facts.scopes),
        "expires_at": facts.expires_at.isoformat() if facts.expires_at else None,
    }


async def capture(db, project, actor, request):
    """Capture only for a new job, checking the original authenticated token."""
    try:
        actor_id, project_id = _uuid(actor.id), _uuid(project.id)
        raw_key_id = getattr(request.state, "api_key_id", None)
        key_id = _uuid(raw_key_id) if raw_key_id is not None else None
        proof = await _key(db, key_id, actor_id, lock=True) if key_id else None
        if proof is not None:
            scheme, _, plaintext = request.headers.get("authorization", "").partition(
                " "
            )
            original_scopes = getattr(request.state, "api_key_scopes", None)
            try:
                valid = (
                    scheme.lower() == "bearer"
                    and verify_password(plaintext, proof["hash"])
                    and isinstance(original_scopes, list)
                    and sorted(original_scopes) == proof["scopes"]
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ExportOutputAuthorityError()
        elif getattr(request.state, "api_key_scopes", None) is not None:
            raise ExportOutputAuthorityError()
        return {
            "schema": 1,
            "actor_id": str(actor_id),
            "project_id": str(project_id),
            "api_key_id": str(key_id) if key_id else None,
            "key_proof": proof,
        }
    except ExportOutputAuthorityError as exc:
        raise HTTPException(403, {"code": exc.code}) from None


async def validate(db, job, *, lock=False):
    """The worker separately fences the current account, project and task scope."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    proof = payload.get("output_authority")
    if proof is None:
        raise ExportOutputAuthorityError("export_output_authority_missing")
    if (
        not isinstance(proof, dict)
        or set(proof) != _FIELDS
        or type(proof["schema"]) is not int
        or proof["schema"] != 1
        or proof["actor_id"] != str(_uuid(job.user_id))
        or proof["project_id"] != str(_uuid(job.project_id))
    ):
        raise ExportOutputAuthorityError()
    if proof["api_key_id"] is None:
        if proof["key_proof"] is not None:
            raise ExportOutputAuthorityError()
        return
    key_id = _uuid(proof["api_key_id"])
    if await _key(db, key_id, _uuid(job.user_id), lock=lock) != proof["key_proof"]:
        raise ExportOutputAuthorityError()
