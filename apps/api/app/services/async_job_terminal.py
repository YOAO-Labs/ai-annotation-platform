"""Transport-independent terminal facts consumed by domain ledger adapters."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class JobTerminal:
    job_id: UUID
    kind: str
    cancelled: bool
    error: str | None = None
