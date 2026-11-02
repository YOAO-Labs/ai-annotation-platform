"""Bind a request's accepted API key to its current Identity-owned facts."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import storage_access
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import AcceptedApiKeyFacts, IdentityCredentialRejected
from app.services.identity_gateway import get_identity_gateway


async def capture_request_key_proof(
    db: AsyncSession, *, actor_id: uuid.UUID, request
) -> AcceptedApiKeyFacts | None:
    """Authenticate the original key and retain its current account/key fences.

    Call before this request or another owner transaction holds a conflicting
    key SHARE fence: Identity confirmation takes an UPDATE lock. The initial
    facts read is isolated and closed before authentication. This function does
    not commit or roll back the caller's transaction; the caller owns the final
    fences and must end read-only admission before long physical I/O.
    """
    raw_key_id = getattr(request.state, "api_key_id", None)
    admitted_scopes = getattr(request.state, "api_key_scopes", None)
    if raw_key_id is None:
        if admitted_scopes is not None:
            raise IdentityCredentialRejected("credential_invalid")
        return None
    try:
        key_id = uuid.UUID(str(raw_key_id))
    except (TypeError, ValueError, AttributeError):
        raise IdentityCredentialRejected("credential_invalid") from None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not isinstance(admitted_scopes, list):
        raise IdentityCredentialRejected("credential_invalid")

    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as before_db:
        before = await IdentityAccountLockData(before_db).read_accepted_api_key_facts(
            actor_id, key_id
        )
    if before is None:
        raise IdentityCredentialRejected("credential_revoked")
    principal = await get_identity_gateway().authenticate(token, db=db)
    if (
        principal.credential_type != "api_key"
        or principal.actor_id != actor_id
        or principal.credential_id != key_id
        or tuple(principal.scopes or ()) != tuple(admitted_scopes)
    ):
        raise IdentityCredentialRejected("credential_invalid")
    identity = IdentityAccountLockData(db)
    accounts = await identity.lock_accounts([actor_id], mode="share")
    actor = accounts.get(actor_id)
    if actor is None or not actor.is_active:
        raise IdentityCredentialRejected("credential_revoked")
    current = await identity.read_accepted_api_key_facts(actor_id, key_id)
    if current is None or current != before or current.scopes != tuple(admitted_scopes):
        raise IdentityCredentialRejected("credential_revoked")
    return current
