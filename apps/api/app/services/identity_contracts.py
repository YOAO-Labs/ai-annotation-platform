"""Pure Identity facts and failures shared by admission and account workflows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


class IdentityCredentialRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IdentityGatewayUnavailable(Exception):
    """Identity could not return a trustworthy admission result."""


class IdentityBriefsUnavailable(IdentityGatewayUnavailable):
    """The selected profile owner could not return a complete trusted response."""


@dataclass(frozen=True, slots=True)
class IdentityActorFacts:
    """Current account facts, separate from project-scoped authorization."""

    id: uuid.UUID
    name: str
    email: str
    role: str
    is_active: bool
    weekly_target_default: int | None = None
