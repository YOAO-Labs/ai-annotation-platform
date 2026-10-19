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


@dataclass(frozen=True, slots=True)
class IdentityActorFacts:
    """Current account facts, separate from project-scoped authorization."""

    id: uuid.UUID
    name: str
    email: str
    role: str
    is_active: bool
