"""Pure identity-owned facts passed from locked account reads to workflows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdentityActorFacts:
    """Current account facts, separate from project-scoped authorization."""

    id: uuid.UUID
    name: str
    email: str
    role: str
    is_active: bool
