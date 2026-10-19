"""One JWT/API-key admission implementation for embedded and remote consumers."""

from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from jose import JWTError

from .contract import (
    CandidatesPage,
    IdentityFailure,
    IdentityUnavailable,
    Principal,
    decode_principal_reply,
    require_jwt_scope,
)
from .security import decode_access_token, verify_password


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
    key_hash: str | None


class Authenticator:
    def __init__(self, data: IdentityData, secret: str):
        self.data = data
        self.secret = secret

    async def authenticate(self, token: str) -> Principal:
        return (await self.authenticate_bound(token)).principal

    async def authenticate_bound(self, token: str) -> VerifiedCredential:
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise IdentityFailure("credential_invalid")
        if token.startswith("ak_"):
            return await self._api_key_bound(token)
        try:
            claims = decode_access_token(token, secret=self.secret, verify_exp=False)
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
            JWTError,
            KeyError,
            ValueError,
            TypeError,
            OverflowError,
            AttributeError,
        ):
            raise IdentityFailure("credential_invalid") from None
        if expires_at <= datetime.now(UTC):
            raise IdentityFailure("token_expired")
        principal = decode_principal_reply(
            await self.data.read_jwt_principal(
                actor_id,
                jti,
                generation,
                expires_at,
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
                page = CandidatesPage.model_validate_json(
                    json.dumps(
                        await self.data.read_api_key_candidates(token[:12], after, 100)
                    )
                )
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
