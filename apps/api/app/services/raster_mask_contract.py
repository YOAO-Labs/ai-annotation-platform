"""Persistence-free rejection shared by Mask content and native write gates."""

from typing import Any


class RasterMaskContractError(ValueError):
    """Stable API-facing rejection raised at the Mask persistence boundary."""

    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        **detail: Any,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"reason": reason, "message": message, **detail}
