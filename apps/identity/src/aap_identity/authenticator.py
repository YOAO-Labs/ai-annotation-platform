"""One JWT/API-key admission implementation for embedded and remote consumers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from jose import JWTError

from .contract import (
    CandidatesPage,
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    _reject_protocol_alias,
    decode_principal_reply,
    require_jwt_scope,
)
from .security import decode_access_token, verify_password

_REFRESH_GRACE = timedelta(seconds=604800)


class IdentityData(Protocol):
    async def read_jwt_principal(
        self,
        actor_id: uuid.UUID,
        jti: uuid.UUID,
        generation: int,
        expires_at: datetime,
        for_refresh: bool = False,
    ) -> dict: ...
    async def read_api_key_candidates(
        self, prefix: str, after_id: uuid.UUID | None, limit: int
    ) -> dict: ...
    async def confirm_api_key_principal(
        self, key_id: uuid.UUID, expected_hash: str
    ) -> dict: ...


@dataclass(frozen=True, slots=True)
class VerifiedCredential:
    principal: Principal
    key_hash: str | None = field(repr=False)


class CredentialDecodeFailure(IdentityFailure):
    """Credential failure with safe provenance for session error mapping."""

    def __init__(self, reason: str):
        if reason not in {"invalid_token", "malformed_token"}:
            raise ValueError("Unsupported credential decode failure")
        self.reason = reason
        super().__init__("credential_invalid")


class Authenticator:
    def __init__(self, data: IdentityData, secret: str):
        self.data = data
        self.secret = secret

    async def authenticate(self, token: str) -> Principal:
        return (await self.authenticate_bound(token)).principal

    async def authenticate_bound(
        self, token: str, *, for_refresh: bool = False
    ) -> VerifiedCredential:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise IdentityFailure("credential_invalid")
        if token.startswith("ak_"):
            if for_refresh:
                raise CredentialDecodeFailure("invalid_token")
            return await self._api_key_bound(token)
        try:
            claims = decode_access_token(token, secret=self.secret, verify_exp=False)
        except (JWTError, ValueError, TypeError, UnicodeError):
            raise CredentialDecodeFailure("invalid_token") from None
        try:
            actor_id = uuid.UUID(claims["sub"])
            jti = uuid.UUID(claims["jti"])
            generation = claims.get("gen", 0)
            expiration = claims["exp"]
            if (
                type(generation) is not int
                or not 0 <= generation < 2**63
                or type(expiration) not in (int, float)
                or not math.isfinite(expiration)
                or int(expiration) != expiration
            ):
                raise ValueError("Invalid claims")
            expires_at = datetime.fromtimestamp(expiration, UTC)
        except (
            KeyError,
            ValueError,
            TypeError,
            OverflowError,
            OSError,
            AttributeError,
        ):
            raise CredentialDecodeFailure("malformed_token") from None
        now = datetime.now(UTC)
        if not for_refresh and expires_at <= now:
            raise IdentityFailure("token_expired")
        if for_refresh and now > expires_at + _REFRESH_GRACE:
            raise IdentityFailure("token_expired")
        principal = decode_principal_reply(
            await self.data.read_jwt_principal(
                actor_id,
                jti,
                generation,
                expires_at,
                for_refresh=for_refresh,
            )
        )
        require_jwt_scope(principal, actor_id, jti, generation, expires_at)
        return VerifiedCredential(principal, None)

    async def _api_key_bound(self, token: str) -> VerifiedCredential:
        if not re.fullmatch(r"ak_[A-Za-z0-9_-]{9}", token[:12]):
            raise IdentityFailure("credential_invalid")
        after: uuid.UUID | None = None
        while True:
            try:
                raw = await self.data.read_api_key_candidates(token[:12], after, 100)
                _reject_protocol_alias(raw)
                page = CandidatesPage.model_validate_json(json.dumps(raw))
                ids = [candidate.id for candidate in page.items]
                if (
                    len(ids) > 100
                    or ids != sorted(set(ids))
                    or any(after is not None and value <= after for value in ids)
                ):
                    raise ValueError("Candidate order changed")
                if page.next_after is not None and (
                    not ids or page.next_after != ids[-1]
                ):
                    raise ValueError("Invalid candidate continuation")
            except (ValueError, TypeError):
                raise IdentityUnavailable() from None
            for candidate in page.items:
                if await asyncio.to_thread(verify_password, token, candidate.key_hash):
                    principal = decode_principal_reply(
                        await self.data.confirm_api_key_principal(
                            candidate.id,
                            candidate.key_hash,
                        )
                    )
                    if (
                        principal.credential_type != "api_key"
                        or principal.credential_id != candidate.id
                    ):
                        raise IdentityUnavailable()
                    return VerifiedCredential(principal, candidate.key_hash)
            if page.next_after is None:
                raise IdentityFailure("credential_invalid")
            after = page.next_after
