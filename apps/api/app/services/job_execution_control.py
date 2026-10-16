"""Execution cancellation requests without coupling use cases to a broker."""

from typing import Protocol


class JobExecutionControl(Protocol):
    def request_cancel(self, execution_id: str) -> None:
        """Request cooperative execution cancellation; this does not prove it stopped."""
        ...
