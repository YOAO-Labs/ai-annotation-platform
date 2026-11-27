"""Terminal facts passed from job workflows to domain reconciliers."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class JobTerminal:
    """Transport-independent terminal facts consumed by domain adapters."""

    job_id: UUID
    kind: str
    cancelled: bool
    error: str | None = None
